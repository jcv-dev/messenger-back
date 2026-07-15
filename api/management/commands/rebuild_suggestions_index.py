"""Rebuild the MessageSuggestion index from existing outbound text messages."""
from collections import defaultdict

from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db.models import Q

from api.models import Message, ConversationTag


class Command(BaseCommand):
    help = 'Rebuild the autocomplete suggestion index from all existing agent text messages'

    def add_arguments(self, parser):
        parser.add_argument(
            '--days', type=int, default=None,
            help='Only index messages from the last N days (default: all)',
        )

    def handle(self, *args, **options):
        from api.suggestions import index_message

        days = options['days']

        qs = Message.objects.filter(
            direction='outbound',
            message_type='text',
        ).exclude(
            Q(content__isnull=True) | Q(content=''),
        )

        if days:
            cutoff = timezone.now() - timezone.timedelta(days=days)
            qs = qs.filter(created_at__gte=cutoff)

        total = qs.count()
        if total == 0:
            self.stdout.write(self.style.WARNING('No outbound text messages found to index'))
            return

        self.stdout.write(f'Indexing {total} messages...')

        conv_ids = set(
            qs.values_list('conversation_id', flat=True).distinct()
        )

        self.stdout.write(f'  Fetching tags for {len(conv_ids)} conversations...')
        tags_by_conv = defaultdict(set)
        for tag in ConversationTag.objects.filter(
            conversation_id__in=conv_ids,
        ).filter(
            Q(expires_at__gt=timezone.now()) | Q(expires_at__isnull=True),
        ).values('conversation_id', 'tag_name'):
            tags_by_conv[tag['conversation_id']].add(tag['tag_name'])

        indexed = 0
        batch = []
        for msg in qs.only('content', 'conversation_id', 'sender_id'):
            tags = list(tags_by_conv.get(msg.conversation_id, []))
            batch.append((msg.content, tags, msg.sender_id))
            indexed += 1

            if len(batch) >= 100:
                for content, tags, sender_id in batch:
                    index_message(content, tags, sender_id)
                batch = []
                self.stdout.write(f'  ... {indexed}/{total}')

        if batch:
            for content, tags, sender_id in batch:
                index_message(content, tags, sender_id)

        self.stdout.write(self.style.SUCCESS(
            f'Indexed {indexed} messages'
        ))
