"""Create an integration API key for ops → Messager pushes.

Example::

    python manage.py create_integration_key \
        --name "Domiitulua ops" --scopes exemptions:write,orders:write
"""

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError

from api.integrations.auth import ALL_SCOPES, generate_api_key
from api.models import IntegrationApiKey


class Command(BaseCommand):
    help = 'Create an integration API key (raw key is shown only once)'

    def add_arguments(self, parser):
        parser.add_argument('--name', required=True, help='Descriptive name')
        parser.add_argument(
            '--scopes', required=True,
            help=f'Comma-separated scopes. Valid: {", ".join(ALL_SCOPES)}',
        )
        parser.add_argument('--created-by', default='', help='Username of the creator (optional)')
        parser.add_argument(
            '--print', action='store_true', dest='print_only',
            help='Print only the raw key (for scripts)',
        )

    def handle(self, *args, **options):
        name = (options['name'] or '').strip()
        if not name:
            raise CommandError('El nombre es requerido.')

        scopes = [s.strip() for s in (options['scopes'] or '').split(',') if s.strip()]
        if not scopes:
            raise CommandError('Debes indicar al menos un scope.')
        invalid = [s for s in scopes if s not in ALL_SCOPES]
        if invalid:
            raise CommandError(
                f'Scopes inválidos: {", ".join(invalid)}. Válidos: {", ".join(ALL_SCOPES)}'
            )

        created_by = None
        username = (options['created_by'] or '').strip()
        if username:
            try:
                created_by = User.objects.get(username=username)
            except User.DoesNotExist:
                raise CommandError(f'Usuario no encontrado: {username}')

        raw, key_hash, prefix = generate_api_key()
        IntegrationApiKey.objects.create(
            name=name,
            key_hash=key_hash,
            prefix=prefix,
            scopes=scopes,
            created_by=created_by,
        )

        if options['print_only']:
            self.stdout.write(raw)
            return

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS('Integration API key creada'))
        self.stdout.write(f'  Nombre:  {name}')
        self.stdout.write(f'  Scopes:  {", ".join(scopes)}')
        self.stdout.write(f'  Prefix:  {prefix}')
        self.stdout.write('')
        self.stdout.write(self.style.WARNING(f'  Key: {raw}'))
        self.stdout.write(self.style.ERROR('  Guarda esta key — no se puede recuperar.'))
        self.stdout.write('')
