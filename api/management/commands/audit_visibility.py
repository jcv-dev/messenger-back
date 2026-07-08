from django.core.management.base import BaseCommand
from django.utils import timezone
from django.contrib.auth import get_user_model
from django.db.models import Count, Q, Exists, OuterRef

from api.models import (
    Conversation,
    ConversationTake,
    ConversationUserPin,
    CityGroup,
)

User = get_user_model()
BOT_USERNAME = "bot"

SECONDS_IN_HOUR = 3600


class Command(BaseCommand):
    help = "Audit conversation visibility for non-staff users"

    def handle(self, *args, **options):
        now = timezone.now()

        self.stdout.write("=" * 65)
        self.stdout.write("DOMI MESSENGER — VISIBILITY AUDIT")
        self.stdout.write(f"Timestamp: {now.isoformat()}")
        self.stdout.write("=" * 65)

        self._check_bot_user()
        self._check_groups()
        self._check_users()
        self._audit_active_takes(now)
        self._audit_null_group_conversations(now)
        self._audit_visibility(now)
        self._check_group_assignment_issues()

    def _style_bool(self, val):
        return self.style.SUCCESS("YES") if val else self.style.ERROR("NO")

    # ── 1. System checks ──────────────────────────────────────────

    def _check_bot_user(self):
        self.stdout.write("\n── Bot User ──")
        try:
            bot = User.objects.get(username=BOT_USERNAME)
            self.stdout.write(f"  Found: {bot.username} (id={bot.id}, is_staff={bot.is_staff}, is_active={bot.is_active})")
        except User.DoesNotExist:
            self.stdout.write(self.style.ERROR(f"  Bot user '{BOT_USERNAME}' DOES NOT EXIST"))
            self.stdout.write(self.style.WARNING(
                "  → All conversations with a bot-generated ConversationTake will be invisible\n"
                "    to non-staff users, because the exclude(created_by__username='bot') in\n"
                "    get_queryset() will silently fail (no bot user → no takes by bot → takes\n"
                "    by unknown users counted as 'other human')."
            ))

    def _check_groups(self):
        self.stdout.write("\n── City Groups ──")
        groups = CityGroup.objects.annotate(
            member_count=Count("members"),
            conversation_count=Count("conversations"),
        ).order_by("name")
        if not groups:
            self.stdout.write(self.style.ERROR("  No city groups defined!"))
            return
        for g in groups:
            self.stdout.write(f"  {g.name} (slug={g.slug}, id={g.id}) — {g.member_count} members, {g.conversation_count} conversations")

    def _check_users(self):
        self.stdout.write("\n── Non-staff Users ──")
        users = User.objects.filter(
            is_staff=False, is_active=True
        ).select_related("profile__group").order_by("username")
        total = 0
        unassigned = 0
        for u in users:
            total += 1
            group_name = u.profile.group.name if (hasattr(u, "profile") and u.profile and u.profile.group) else None
            if group_name:
                self.stdout.write(f"  {u.username:20s} → {group_name}")
            else:
                unassigned += 1
                self.stdout.write(self.style.WARNING(f"  {u.username:20s} → NO GROUP ASSIGNED (profile.group is None)"))
        if total == 0:
            self.stdout.write(self.style.WARNING("  No non-staff active users found"))
        else:
            self.stdout.write(f"\n  Total: {total} active non-staff users, {unassigned} without group assignment")

    # ── 2. Active Takes Audit ─────────────────────────────────────

    def _audit_active_takes(self, now):
        self.stdout.write("\n── Active Takes Audit (expires_at > now) ──")

        active_takes = ConversationTake.objects.filter(
            expires_at__gt=now
        ).select_related("created_by", "conversation").order_by("-created_at")

        total = active_takes.count()
        self.stdout.write(f"  Total active takes: {total}")

        if total == 0:
            self.stdout.write("  (no active takes — visibility issue is likely not take-related)")
            return

        orphaned = active_takes.filter(created_by__isnull=True)
        if orphaned.exists():
            self.stdout.write(self.style.ERROR(
                f"\n  ⚠  ORPHANED TAKES (created_by IS NULL): {orphaned.count()}"
            ))
            for t in orphaned:
                self.stdout.write(self.style.ERROR(
                    f"     take id={t.id}, conversation={t.conversation.id} ({t.conversation.contact_name}), "
                    f"expires_at={t.expires_at.isoformat()}"
                ))
            self.stdout.write(self.style.WARNING(
                "  → These takes ARE correctly excluded from _has_other_human_take\n"
                "    by .exclude(created_by__isnull=True). No visibility impact."
            ))

        bot_active = active_takes.filter(created_by__username=BOT_USERNAME)
        self.stdout.write(f"\n  Bot takes: {bot_active.count()}")

        non_bot_takes = active_takes.exclude(created_by__username=BOT_USERNAME).exclude(created_by__isnull=True)
        self.stdout.write(f"  Non-bot, non-null active takes: {non_bot_takes.count()}")

        # Group by user
        user_counts = (
            non_bot_takes.values("created_by__id", "created_by__username")
            .annotate(cnt=Count("id"))
            .order_by("-cnt")
        )
        if user_counts:
            self.stdout.write("\n  Active takes by user:")
            for uc in user_counts:
                self.stdout.write(
                    f"    {uc['created_by__username'] or '???'} (id={uc['created_by__id']}): "
                    f"{uc['cnt']} active takes"
                )

        # Check for deactivated users with active takes
        deactivated = non_bot_takes.filter(created_by__is_active=False)
        if deactivated.exists():
            self.stdout.write(self.style.ERROR(
                f"\n  ⚠  TAKES BY DEACTIVATED USERS: {deactivated.count()}"
            ))
            self.stdout.write(self.style.WARNING(
                "  → These takes ARE counted as 'other human' in _has_other_human_take!\n"
                "    Conversations held by deactivated users will be invisible to all other\n"
                "    non-staff users until the take expires or is cleaned up."
            ))
            for t in deactivated.select_related("created_by", "conversation__group")[:10]:
                self.stdout.write(self.style.WARNING(
                    f"     take id={t.id} user={t.created_by.username} (active={t.created_by.is_active}) "
                    f"conv id={t.conversation.id} group={t.conversation.group_id} "
                    f"expires_at={t.expires_at.isoformat()}"
                ))
            remaining = deactivated.count() - 10
            if remaining > 0:
                self.stdout.write(self.style.WARNING(f"     ... and {remaining} more"))

        # Long-running takes
        long_takes = non_bot_takes.filter(
            expires_at__gt=now + timezone.timedelta(hours=1)
        )
        if long_takes.exists():
            self.stdout.write(f"\n  Long-running takes (>1h from now): {long_takes.count()}")
            for t in long_takes.select_related("created_by", "conversation").order_by("-expires_at")[:5]:
                remaining = (t.expires_at - now).total_seconds() / 60
                self.stdout.write(
                    f"    take id={t.id} by {t.created_by.username}, "
                    f"conv id={t.conversation.id}, "
                    f"remaining={remaining:.0f}min, duration={t.duration_minutes}min"
                )

        # Duplicate takes on same conversation
        dupes = (
            non_bot_takes.values("conversation_id")
            .annotate(cnt=Count("id"))
            .filter(cnt__gt=1)
        )
        if dupes.exists():
            self.stdout.write(self.style.ERROR(
                f"\n  ⚠  CONVERSATIONS WITH MULTIPLE ACTIVE TAKES: {dupes.count()}"
            ))
            self.stdout.write(self.style.WARNING(
                "  → This should never happen! take_conversation() deletes all existing takes\n"
                "    before creating a new one. Something may be wrong with the transaction logic."
            ))
            for d in dupes[:10]:
                conv_takes = ConversationTake.objects.filter(
                    conversation_id=d["conversation_id"], expires_at__gt=now
                ).select_related("created_by")
                take_list = "; ".join(
                    f"id={t.id} by={t.created_by.username if t.created_by else 'NULL'}"
                    for t in conv_takes
                )
                self.stdout.write(self.style.ERROR(
                    f"    conversation id={d['conversation_id']} — {d['cnt']} takes: [{take_list}]"
                ))

    # ── 3. Null-group Conversations ───────────────────────────────

    def _audit_null_group_conversations(self, now):
        self.stdout.write("\n── Null-Group Active Conversations ──")
        null_group = Conversation.objects.filter(
            group__isnull=True,
            status="active",
        ).annotate(msg_count=Count("messages")).filter(msg_count__gt=0)

        count = null_group.count()
        if count == 0:
            self.stdout.write("  None — all active conversations have a group assigned")
        else:
            self.stdout.write(self.style.ERROR(
                f"  ⚠  {count} active conversations have group=NULL"
            ))
            self.stdout.write(self.style.WARNING(
                "  → These conversations are INVISIBLE to ALL non-staff users\n"
                "    because get_queryset() filters by group_id=user.profile.group_id.\n"
                "    Staff can see them (staff bypasses group filter)."
            ))
            for c in null_group.select_related("group").order_by("-last_message_at")[:10]:
                self.stdout.write(self.style.WARNING(
                    f"    conv id={c.id} name={c.contact_name} phone={c.contact_phone} "
                    f"last_msg={c.last_message_at.isoformat() if c.last_message_at else 'never'}"
                ))
            remaining = count - 10
            if remaining > 0:
                self.stdout.write(self.style.WARNING(f"    ... and {remaining} more"))

    # ── 4. Visibility analysis per user ────────────────────────────

    def _audit_visibility(self, now):
        self.stdout.write("\n── Visibility Analysis (per non-staff user) ──")
        users = User.objects.filter(
            is_staff=False, is_active=True
        ).select_related("profile__group")

        shown = False
        for u in users:
            group_id = u.profile.group_id if (hasattr(u, "profile") and u.profile) else None
            if not group_id:
                continue

            # Total active conversations in this user's group
            group_convos = Conversation.objects.filter(
                group_id=group_id,
                status="active",
            ).annotate(msg_count=Count("messages")).filter(msg_count__gt=0)
            total_in_group = group_convos.count()

            # Conversations with other human take (non-null, not self, not bot)
            other_human_takes = ConversationTake.objects.filter(
                conversation=OuterRef("pk"),
                expires_at__gt=now,
            ).exclude(created_by__isnull=True).exclude(created_by=u).exclude(created_by__username="bot")

            hidden_by_other_take = group_convos.annotate(
                _has_other_human_take=Exists(other_human_takes)
            ).filter(_has_other_human_take=True)

            # Count how many of those hidden have a personal pin from this user
            hidden_but_pinned = hidden_by_other_take.filter(
                user_pins__user=u
            ).distinct().count()

            hidden_by_other_take_count = hidden_by_other_take.count()
            visible = total_in_group - hidden_by_other_take_count

            self.stdout.write(f"\n  User: {u.username}")
            self.stdout.write(f"    Group: {u.profile.group.name if u.profile.group else 'None'}")
            self.stdout.write(
                f"    Conversations in group: {total_in_group} "
                f"→ visible: {visible}, hidden by other-human-take: {hidden_by_other_take_count}"
            )
            if hidden_but_pinned > 0:
                self.stdout.write(self.style.ERROR(
                    f"    ⚠  {hidden_but_pinned} conversation(s) hidden by other-human-take that user HAS PERSONALLY PINNED"
                ))
                self.stdout.write(self.style.WARNING(
                    "    → This is the group-pin + personal-pin exception bug (step 3 in the plan).\n"
                    "      The ~Q(is_pinned=True, _has_other_human_take=True) filter has no personal-pin exception."
                ))
            if hidden_by_other_take_count > 0:
                self.stdout.write(
                    f"    Hidden details (up to 5):"
                )
                for c in hidden_by_other_take.select_related("group").order_by("-last_message_at")[:5]:
                    group_label = c.group.name if c.group else "NULL"
                    pinned_label = "✓" if c.is_pinned else "✗"
                    self.stdout.write(
                        f"      conv id={c.id} name={c.contact_name} "
                        f"group={group_label} is_pinned={pinned_label}"
                    )

            shown = True

        if not shown:
            self.stdout.write("  No non-staff users with group assignment to analyze")

    # ── 5. Group assignment issues ────────────────────────────────

    def _check_group_assignment_issues(self):
        self.stdout.write("\n── Group Assignment Issues ──")

        unassigned_convos = Conversation.objects.filter(
            group__isnull=True,
        ).count()
        unassigned_users = User.objects.filter(
            is_staff=False, is_active=True, profile__group__isnull=True
        ).count()

        if unassigned_convos > 0:
            self.stdout.write(self.style.WARNING(
                f"  {unassigned_convos} conversations have no group (NULL)"
            ))
        else:
            self.stdout.write("  All conversations have a group assigned.")

        if unassigned_users > 0:
            self.stdout.write(self.style.WARNING(
                f"  {unassigned_users} active non-staff users have no group assigned"
            ))
            for u in User.objects.filter(is_staff=False, is_active=True, profile__group__isnull=True):
                self.stdout.write(self.style.WARNING(
                    f"    user id={u.id} username={u.username}"
                ))
        else:
            self.stdout.write("  All active non-staff users have a group assigned.")

        self.stdout.write("")
