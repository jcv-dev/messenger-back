"""Phase 5 — aggregate order status notification configuration.

Seeds the three ``BotConfig`` keys the Messager reads to notify clients on
batch transitions (``asignado``, ``en_ruta``, ``entregado``, ``cancelado``):

- ``order_status_notifications_enabled`` — master switch (on by default; the
  feature can be killed at any time without a deploy).
- ``order_status_messages`` — free-text message per status (used inside the
  WhatsApp 24 h window; placeholders ``{order_numbers}``, ``{order_number}``,
  ``{total}``, ``{count}``, ``{origin}``, ``{status}``, ``{status_label}``).
- ``order_status_templates`` — approved Meta template per status, used when
  Meta rejects the free-text message because the window is closed.
  ``params`` lists the values the template body expects, in order.

Seeds never overwrite an existing value, so later customizations survive.
"""

from django.db import migrations

DEFAULT_MESSAGES = {
    'asignado': '🛵 Pedido {order_numbers} asignado. Un domiciliario va en camino a recogerlo.',
    'en_ruta': '🛵 Pedido {order_numbers} en camino.',
    'entregado': '✅ Pedido {order_numbers} entregado. ¡Gracias por preferirnos!',
    'cancelado': '❌ Pedido {order_numbers} cancelado. Si necesitas ayuda, escríbenos.',
}

DEFAULT_TEMPLATES = {
    'asignado': {'template': 'aviso_asignado', 'language': 'es', 'params': ['order_number']},
    'en_ruta': {'template': 'aviso_en_ruta', 'language': 'es', 'params': ['order_number']},
    'entregado': {'template': 'aviso_entregado', 'language': 'es', 'params': ['order_number']},
    'cancelado': {'template': 'aviso_cancelado', 'language': 'es', 'params': ['order_number']},
}


def seed_order_status_notifications(apps, schema_editor):
    BotConfig = apps.get_model('api', 'BotConfig')
    BotConfig.objects.get_or_create(
        key='order_status_notifications_enabled',
        defaults={
            'value': True,
            'description': (
                'Activa las notificaciones automáticas al cliente cuando el pedido '
                'pasa a asignado, en ruta, entregado o cancelado.'
            ),
        },
    )
    BotConfig.objects.get_or_create(
        key='order_status_messages',
        defaults={
            'value': DEFAULT_MESSAGES,
            'description': (
                'Mensajes de estado por transición del pedido (texto libre, ventana de 24 h). '
                'Placeholders: {order_numbers}, {order_number}, {total}, {count}, {origin}, '
                '{status}, {status_label}.'
            ),
        },
    )
    BotConfig.objects.get_or_create(
        key='order_status_templates',
        defaults={
            'value': DEFAULT_TEMPLATES,
            'description': (
                'Plantillas aprobadas por Meta usadas cuando la ventana de 24 h está '
                'cerrada. params: valores del cuerpo en orden.'
            ),
        },
    )


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0040_cancel_followups'),
    ]

    operations = [
        migrations.RunPython(
            seed_order_status_notifications, migrations.RunPython.noop,
        ),
    ]
