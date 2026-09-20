"""Phase 4 follow-up (2026-09-20, user feedback).

- Cancellation no longer creates a ``ConversationNote``: the reason lives in
  the ops ``cancel_reason`` and in the ``AuditLog``. Drops the
  ``cancel_note_expiry_minutes`` config seeded by the first iteration.
- The order confirmation default no longer shows the price. The stored value
  is only updated when it still matches the old default, so a customized
  template is never overwritten.
"""

from django.db import migrations

OLD_ORDER_CREATED_MESSAGE = (
    '✅ Pedido {order_numbers} recibido. Valor: {total}. '
    'Un domiciliario lo aceptará pronto.'
)

NEW_ORDER_CREATED_MESSAGE = (
    '✅ Pedido {order_numbers} recibido. '
    'Un domiciliario lo aceptará pronto.'
)


def apply_followups(apps, schema_editor):
    BotConfig = apps.get_model('api', 'BotConfig')
    BotConfig.objects.filter(key='cancel_note_expiry_minutes').delete()
    BotConfig.objects.filter(
        key='order_created_message', value=OLD_ORDER_CREATED_MESSAGE,
    ).update(value=NEW_ORDER_CREATED_MESSAGE)


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0039_order_cancellation'),
    ]

    operations = [
        migrations.RunPython(apply_followups, migrations.RunPython.noop),
    ]
