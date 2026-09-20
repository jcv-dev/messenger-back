"""Backfill conversation ↔ ops client links by contact phone (plan Phase 2).

Example::

    python manage.py link_ops_clients --dry-run
    python manage.py link_ops_clients --batch-size 100
    python manage.py link_ops_clients --limit 50 --sleep 0.1
"""

import time

from django.core.management.base import BaseCommand, CommandError

from api.integrations import ops
from api.integrations.clients import fetch_client_by_phone, link_conversation
from api.models import Conversation


class Command(BaseCommand):
    help = 'Link conversations to ops clients by phone (backfill)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report what would be linked without writing.',
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
            help='Seconds to wait between ops calls (politeness).',
        )

    def handle(self, *args, **options):
        if not ops.is_configured():
            raise CommandError('Ops API no configurada (OPS_API_URL/OPS_API_KEY).')

        dry_run = options['dry_run']
        batch_size = max(1, options['batch_size'])
        limit = max(0, options['limit'])
        sleep_seconds = max(0.0, options['sleep'])

        qs = (
            Conversation.objects
            .filter(ops_client_user_id__isnull=True)
            .exclude(contact_phone__isnull=True)
            .exclude(contact_phone='')
            .order_by('id')
        )

        scanned = linked = skipped = 0
        for conversation in qs.iterator(chunk_size=batch_size):
            if limit and scanned >= limit:
                break
            scanned += 1

            client = fetch_client_by_phone(conversation.contact_phone)
            if not client:
                skipped += 1
            else:
                linked += 1
                action = 'se vincularía' if dry_run else 'vinculada'
                self.stdout.write(
                    f'  Conversación {conversation.id} → cliente {client["id"]} '
                    f'({client["name"] or client["phone"]}) — {action}'
                )
                if not dry_run:
                    link_conversation(
                        conversation, client['id'], snapshot=client,
                        source='auto_phone',
                    )

            if sleep_seconds:
                time.sleep(sleep_seconds)

        prefix = 'Simulación: ' if dry_run else ''
        self.stdout.write(self.style.SUCCESS(
            f'{prefix}{scanned} conversaciones revisadas, {linked} vinculadas, '
            f'{skipped} sin cliente en ops.'
        ))
