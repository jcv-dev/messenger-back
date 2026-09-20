"""Phase 4 — order cancellation support.

Adds the ``cancel_order`` action to ``AuditLog.ACTION_CHOICES``.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0038_order_stop_shared_number'),
    ]

    operations = [
        migrations.AlterField(
            model_name='auditlog',
            name='action',
            field=models.CharField(choices=[
                ('take', 'Take'),
                ('release', 'Release'),
                ('pin', 'Pin'),
                ('unpin', 'Unpin'),
                ('send_message', 'Send Message'),
                ('delete_conversation', 'Delete Conversation'),
                ('toggle_status', 'Toggle Status'),
                ('link_client', 'Link Client'),
                ('unlink_client', 'Unlink Client'),
                ('cancel_order', 'Cancel Order'),
            ], max_length=32),
        ),
    ]
