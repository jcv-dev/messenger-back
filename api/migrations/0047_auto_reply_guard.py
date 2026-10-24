"""Auto-reply guard: flag automated replies and seed its configuration.

``Message.is_automated`` lets the API/UI distinguish business auto-responders
from real inbound messages. The seeded ``auto_reply_guard`` BotConfig turns the
guard on with sensible defaults; the pattern list stays in
``api.auto_replies.DEFAULT_PATTERNS`` so it can evolve without a migration.
"""

from django.db import migrations, models


DEFAULT_CONFIG = {
    'enabled': True,
    'mark_read': True,
    'require_marketing': True,
    'marketing_window_hours': 72,
}


def seed_auto_reply_guard(apps, schema_editor):
    BotConfig = apps.get_model('api', 'BotConfig')
    BotConfig.objects.get_or_create(
        key='auto_reply_guard',
        defaults={
            'value': DEFAULT_CONFIG,
            'description': (
                'Guarda de respuestas automáticas: detecta auto-respuestas de '
                'negocios después de una plantilla de marketing y las marca como '
                'leídas para que no entren a la cola de mensajes sin leer.'
            ),
        },
    )


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0046_order_status_enabled'),
    ]

    operations = [
        migrations.AddField(
            model_name='message',
            name='is_automated',
            field=models.BooleanField(db_index=True, default=False),
        ),
        migrations.RunPython(seed_auto_reply_guard, migrations.RunPython.noop),
    ]
