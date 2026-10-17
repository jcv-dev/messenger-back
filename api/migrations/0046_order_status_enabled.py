"""Order status cost control (2026-10-16).

WhatsApp now bills service-window messages, so only the transitions the client
actually needs are sent by default: ``entregado`` and ``cancelado``. The
creation confirmation remains the "recibido" message. ``asignado`` and
``en_ruta`` stay configurable but off; disabling a transition consumes it
silently so it can never be replayed later.

Also updates the default order-creation confirmation only when it still matches
the previous default, so customized texts are never overwritten.
"""

from django.db import migrations

DEFAULT_ORDER_STATUS_ENABLED = {
    'asignado': False,
    'en_ruta': False,
    'entregado': True,
    'cancelado': True,
}

OLD_ORDER_CREATED_MESSAGE = (
    '✅ Pedido {order_numbers} recibido. '
    'Un domiciliario lo aceptará pronto.'
)

NEW_ORDER_CREATED_MESSAGE = (
    '✅ Pedido {order_numbers} recibido. '
    'Te avisaremos cuando sea entregado.'
)


def apply_cost_control(apps, schema_editor):
    BotConfig = apps.get_model('api', 'BotConfig')
    BotConfig.objects.get_or_create(
        key='order_status_enabled',
        defaults={
            'value': DEFAULT_ORDER_STATUS_ENABLED,
            'description': (
                'Avisos de estado que se envían al cliente. Cada aviso es un mensaje '
                'de WhatsApp facturable. Por defecto solo entregado y cancelado; '
                'recibido es el mensaje de confirmación al crear el pedido.'
            ),
        },
    )
    BotConfig.objects.filter(
        key='order_created_message', value=OLD_ORDER_CREATED_MESSAGE,
    ).update(value=NEW_ORDER_CREATED_MESSAGE)


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0045_order_scheduled'),
    ]

    operations = [
        migrations.RunPython(apply_cost_control, migrations.RunPython.noop),
    ]
