"""Adopt the active ops orders of linked conversations that are missing locally.

Batches created before a conversation was linked to its ops client never matched
an event, so they stayed invisible in ``Pedidos activos`` until their next
status change (and courier releases do not push events at all). The link
endpoint/backfill now covers new links; this command heals conversations that
were linked before that fix.

Idempotent: it only creates mirrors that do not exist yet and never notifies the
client about transitions that already happened.

Example::

    python manage.py sync_active_orders --conversation 123
    python manage.py sync_active_orders --limit 50 --sleep 0.1
"""

import time

from django.core.management.base import BaseCommand, CommandError

from api.integrations import ops
from api.integrations.adoption import sync_active_orders
from api.models import Conversation


class Command(BaseCommand):
    help = 'Adopt the active ops orders of linked conversations missing locally'

    def add_arguments(self, parser):
        parser.add_argument(
            '--conversation', type=int, default=0,
            help='Only this conversation id (0 = all linked conversations).',
        )
        parser.add_argument(
            '--batch-size', type=int, default=200,
            help='Rows fetched per DB batch (default: 200).',
        )
        parser.add_argument(
            '--limit', type=int, default=0,
            help='Stop after N conversations (0 = no limit).',
        )
        parser.add_argument(
            '--sleep', type=float, default=0.0,
            help='Seconds to wait between conversations (politeness).',
        )

    def handle(self, *args, **options):
        if not ops.is_configured():
            raise CommandError('Ops API no configurada (OPS_API_URL/OPS_API_KEY).')

        batch_size = max(1, options['batch_size'])
        limit = max(0, options['limit'])
        sleep_seconds = max(0.0, options['sleep'])

        qs = (
            Conversation.objects
            .filter(ops_client_user_id__isnull=False)
            .order_by('id')
        )
        if options['conversation']:
            qs = qs.filter(id=options['conversation'])

        scanned = adopted = 0
        for conversation in qs.iterator(chunk_size=batch_size):
            if limit and scanned >= limit:
                break
            scanned += 1

            count = sync_active_orders(conversation)
            adopted += count
            if count:
                self.stdout.write(
                    f'  Conversación {conversation.id} → cliente '
                    f'{conversation.ops_client_user_id}: {count} pedido(s) adoptado(s)'
                )
            if sleep_seconds:
                time.sleep(sleep_seconds)

        self.stdout.write(self.style.SUCCESS(
            f'{scanned} conversaciones revisadas, {adopted} pedido(s) adoptado(s).'
        ))
