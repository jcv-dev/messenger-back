"""Backfill automated business replies captured before the guard existed.

Example::

    python manage.py classify_auto_replies --days 30
    python manage.py classify_auto_replies --days 30 --dry-run
    python manage.py classify_auto_replies --days 30 --no-mark-read

The command uses the same rules as the live webhook: configured phrases plus a
recent MARKETING template inside the configured window. It evaluates the window
against each message's own timestamp, so older campaigns are classified
correctly.
"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from api.auto_replies import apply_guard, classify_message
from api.models import Message


class Command(BaseCommand):
    help = 'Detect and flag historical automated business replies'

    def add_arguments(self, parser):
        parser.add_argument(
            '--days', type=int, default=30,
            help='Look back this many days (default: 30)',
        )
        parser.add_argument(
            '--limit', type=int, default=5000,
            help='Maximum messages to scan (default: 5000)',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report matches without changing anything',
        )
        parser.add_argument(
            '--no-mark-read', action='store_true',
            help='Flag matches as automated but leave them unread',
        )

    def handle(self, *args, **options):
        days = max(1, options['days'])
        limit = max(1, options['limit'])
        dry_run = options['dry_run']
        mark_read = None if not options['no_mark_read'] else False

        since = timezone.now() - timedelta(days=days)
        candidates = (
            Message.objects
            .filter(
                Q(is_automated=False) | Q(is_automated=True, is_read=False),
                direction='inbound',
                created_at__gte=since,
            )
            .exclude(message_type__in=('reaction', 'edit'))
            .select_related('conversation')
            .order_by('-created_at')[:limit]
        )

        scanned = detected = marked_read = 0
        samples = []
        for message in candidates:
            scanned += 1
            match = classify_message(
                message.conversation, message.message_type,
                message.content, at=message.created_at,
            )
            if not match:
                continue
            detected += 1
            was_read = message.is_read
            if not dry_run:
                apply_guard(message, match, mark_read=mark_read)
            will_mark = (mark_read is not False) and bool(match.get('mark_read', True))
            if not was_read and will_mark:
                marked_read += 1
            if len(samples) < 10:
                samples.append((
                    message.id,
                    message.conversation_id,
                    match.get('matched_pattern'),
                    (message.content or '')[:80],
                ))

        if dry_run:
            self.stdout.write(self.style.WARNING(
                f'DRY RUN — scanned {scanned}, detected {detected}, '
                f'would mark read {marked_read}'
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f'Scanned {scanned}, detected {detected}, marked read {marked_read}'
            ))
        for sample in samples:
            self.stdout.write(
                f'  msg={sample[0]} conv={sample[1]} pattern={sample[2]!r} '
                f'text={sample[3]!r}'
            )
