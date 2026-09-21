from django.core.management.base import BaseCommand
from django.conf import settings
from django.utils import timezone
from datetime import timedelta
import os

from api.models import Call, Message


def _delete_media_file(url, media_root):
    """Delete the local file referenced by a MEDIA_URL path. Returns True if removed."""
    if not url or not url.startswith(settings.MEDIA_URL):
        return False
    rel = url[len(settings.MEDIA_URL):]
    file_path = os.path.join(media_root, rel)
    if os.path.isfile(file_path):
        try:
            os.remove(file_path)
            return True
        except OSError:
            pass
    return False


class Command(BaseCommand):
    help = (
        'Delete messages, calls and their media/recordings older than '
        'MESSAGE_RETENTION_MINUTES'
    )

    def handle(self, *args, **options):
        retention = settings.MESSAGE_RETENTION_MINUTES
        if not retention or retention <= 0:
            self.stdout.write(self.style.WARNING('MESSAGE_RETENTION_MINUTES is 0 or unset - nothing to do'))
            return

        cutoff = timezone.now() - timedelta(minutes=retention)
        media_root = str(settings.MEDIA_ROOT)

        expired_messages = Message.objects.filter(created_at__lt=cutoff)
        media_deleted = 0
        messages_deleted = 0
        if expired_messages.exists():
            for msg in expired_messages.iterator():
                if _delete_media_file(msg.media_url, media_root):
                    media_deleted += 1
            messages_deleted, _ = expired_messages.delete()

        expired_calls = Call.objects.filter(updated_at__lt=cutoff)
        recordings_deleted = 0
        calls_deleted = 0
        if expired_calls.exists():
            for call in expired_calls.iterator():
                if _delete_media_file(call.recording_local_path, media_root):
                    recordings_deleted += 1
            calls_deleted, _ = expired_calls.delete()

        if not messages_deleted and not calls_deleted:
            self.stdout.write(self.style.SUCCESS('No expired messages or calls found'))
            return

        self.stdout.write(self.style.SUCCESS(
            f'Cleaned up {messages_deleted} messages, {media_deleted} media files, '
            f'{calls_deleted} calls, {recordings_deleted} recording files '
            f'(retention: {retention}m, cutoff: {cutoff})'
        ))
