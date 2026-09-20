"""Phase 5 follow-up — the approved ``aviso_*`` templates use a named parameter.

The Meta templates were created with a named body variable ``{{order_code}}``
instead of a positional ``{{1}}``. The Messager can send either form, but the
config has to say which name the template expects, otherwise a template that is
not mirrored locally would be sent positionally and Meta would reject it.

Only entries that still match the migration ``0041`` seed are upgraded, so any
customization (template name, language or params) survives untouched.
"""

from django.db import migrations

OLD_DEFAULTS = {
    'asignado': {'template': 'aviso_asignado', 'language': 'es', 'params': ['order_number']},
    'en_ruta': {'template': 'aviso_en_ruta', 'language': 'es', 'params': ['order_number']},
    'entregado': {'template': 'aviso_entregado', 'language': 'es', 'params': ['order_number']},
    'cancelado': {'template': 'aviso_cancelado', 'language': 'es', 'params': ['order_number']},
}

NEW_PARAMS = [{'name': 'order_code', 'value': 'order_number'}]


def upgrade_named_parameter(apps, schema_editor):
    BotConfig = apps.get_model('api', 'BotConfig')
    config = BotConfig.objects.filter(key='order_status_templates').first()
    if not config or not isinstance(config.value, dict):
        return

    stored = dict(config.value)
    changed = False
    for status, old in OLD_DEFAULTS.items():
        entry = stored.get(status)
        if entry == old:
            updated = dict(entry)
            updated['params'] = [dict(param) for param in NEW_PARAMS]
            stored[status] = updated
            changed = True

    if changed:
        config.value = stored
        config.save(update_fields=['value'])


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0041_order_status_notifications'),
    ]

    operations = [
        migrations.RunPython(upgrade_named_parameter, migrations.RunPython.noop),
    ]
