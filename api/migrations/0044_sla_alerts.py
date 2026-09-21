"""SLA reminder notifications to couriers (WhatsApp via the Messager).

Seeds the ``BotConfig`` keys the Messager reads to send the courier reminder
with the approved ``aviso_sla`` utility template:

- ``sla_notifications_enabled`` — master switch (on by default).
- ``sla_alert_template`` — template name/language and the mapping between the
  template body placeholders (``{{order_code}}``, ``{{time}}``,
  ``{{order_status}}``) and the values ops sends. Value keys are fixed; only
  the placeholder names are editable.

Seeds never overwrite an existing value, so later customizations survive.
"""

from django.db import migrations

DEFAULT_TEMPLATE = {
    'template': 'aviso_sla',
    'language': 'es',
    'params': [
        {'name': 'order_code', 'value': 'order_code'},
        {'name': 'time', 'value': 'time'},
        {'name': 'order_status', 'value': 'order_status'},
    ],
}


def seed_sla_alerts(apps, schema_editor):
    BotConfig = apps.get_model('api', 'BotConfig')
    BotConfig.objects.get_or_create(
        key='sla_notifications_enabled',
        defaults={
            'value': True,
            'description': (
                'Activa el recordatorio por WhatsApp al domiciliario cuando un pedido '
                'supera su umbral SLA (asignado, confirmado o en ruta).'
            ),
        },
    )
    BotConfig.objects.get_or_create(
        key='sla_alert_template',
        defaults={
            'value': DEFAULT_TEMPLATE,
            'description': (
                'Plantilla de utilidad aprobada por Meta para el recordatorio SLA al '
                'domiciliario. params: {name} es la variable del cuerpo de la plantilla '
                'y {value} la clave del valor enviado por ops.'
            ),
        },
    )


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0043_order_courier'),
    ]

    operations = [
        migrations.RunPython(seed_sla_alerts, migrations.RunPython.noop),
    ]
