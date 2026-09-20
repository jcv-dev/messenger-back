from django.db import migrations

DEFAULT_MESSAGE = (
    '✅ Pedido {order_numbers} recibido. '
    'Un domiciliario lo aceptará pronto.'
)


def seed_order_created_message(apps, schema_editor):
    BotConfig = apps.get_model('api', 'BotConfig')
    BotConfig.objects.get_or_create(
        key='order_created_message',
        defaults={
            'value': DEFAULT_MESSAGE,
            'description': (
                'Mensaje de confirmación al cliente cuando un agente crea un pedido '
                'desde el Messager. Placeholders: {order_numbers}, {total}, {count}, '
                '{client_name}, {origin}.'
            ),
        },
    )


class Migration(migrations.Migration):
    dependencies = [
        ('api', '0036_audit_log_client_actions'),
    ]

    operations = [
        migrations.RunPython(seed_order_created_message, migrations.RunPython.noop),
    ]
