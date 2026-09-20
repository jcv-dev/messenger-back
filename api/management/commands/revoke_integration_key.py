"""Revoke an integration API key.

Example::

    python manage.py revoke_integration_key --id 3
"""

from django.core.management.base import BaseCommand, CommandError

from api.models import IntegrationApiKey


class Command(BaseCommand):
    help = 'Revoke (deactivate) an integration API key'

    def add_arguments(self, parser):
        parser.add_argument('--id', type=int, default=None, help='IntegrationApiKey id')
        parser.add_argument('--prefix', default='', help='Key prefix (12 chars) as alternative to --id')

    def handle(self, *args, **options):
        key_id = options.get('id')
        prefix = (options.get('prefix') or '').strip()

        if not key_id and not prefix:
            raise CommandError('Indica --id o --prefix.')

        qs = IntegrationApiKey.objects.all()
        if key_id:
            qs = qs.filter(id=key_id)
        else:
            qs = qs.filter(prefix=prefix)

        keys = list(qs)
        if not keys:
            raise CommandError('No se encontró la key.')
        if len(keys) > 1 and prefix:
            raise CommandError('El prefix coincide con más de una key; usa --id.')

        key = keys[0]
        if not key.is_active:
            self.stdout.write(f'La key #{key.id} ({key.name}) ya estaba revocada.')
            return

        key.is_active = False
        key.save(update_fields=['is_active', 'updated_at'])
        self.stdout.write(self.style.SUCCESS(f'Key #{key.id} ({key.name}) revocada.'))
