from django.core.management.base import BaseCommand
from django.conf import settings
from django.utils import timezone
from django.db.models import Count
from datetime import timedelta
import os

from api.models import Message, Conversation


class Command(BaseCommand):
    help = 'Delete messages and media older than MESSAGE_RETENTION_MINUTES'

    def handle(self, *args, **options):
        retention = settings.MESSAGE_RETENTION_MINUTES
        if not retention or retention <= 0:
            self.stdout.write(self.style.WARNING('MESSAGE_RETENTION_MINUTES is 0 or unset - nothing to do'))
            return

        cutoff = timezone.now() - timedelta(minutes=retention)
        expired = Message.objects.filter(created_at__lt=cutoff)

        total = expired.count()
        if total == 0:
            self.stdout.write(self.style.SUCCESS('No expired messages found'))
            return

        media_root = str(settings.MEDIA_ROOT)
        media_deleted = 0

        for msg in expired.iterator():
            if msg.media_url and msg.media_url.startswith(settings.MEDIA_URL):
                rel = msg.media_url[len(settings.MEDIA_URL):]
                file_path = os.path.join(media_root, rel)
                if os.path.isfile(file_path):
                    try:
                        os.remove(file_path)
                        media_deleted += 1
                    except OSError:
                        pass

        count, _ = expired.delete()

        stale_convs = Conversation.objects.annotate(
            remaining=Count('messages')
        ).filter(
            remaining=0,
            last_message_at__lt=cutoff,
        )
        conv_count = stale_convs.count()
        stale_convs.delete()

        self.stdout.write(self.style.SUCCESS(
            f'Cleaned up {count} messages, {media_deleted} media files, {conv_count} empty conversations '
            f'(retention: {retention}m, cutoff: {cutoff})'
        ))
