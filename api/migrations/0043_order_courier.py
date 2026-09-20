"""Phase 8 — driver selector: the assigned courier on the local mirror.

``Order`` gains the ops courier id plus a name/code snapshot so the order card
and the read-only sheet can show who is delivering without another ops call.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0042_order_status_template_params'),
    ]

    operations = [
        migrations.AddField(
            model_name='order',
            name='ops_courier_user_id',
            field=models.IntegerField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name='order',
            name='courier_name',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
        migrations.AddField(
            model_name='order',
            name='courier_code',
            field=models.CharField(blank=True, default='', max_length=12),
        ),
    ]
