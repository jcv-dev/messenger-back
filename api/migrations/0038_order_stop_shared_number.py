"""Multi-stop pedidos are ops comandas: every local stop shares one order number.

Drops the unique constraint on ``OrderStop.ops_order_number``; a comanda has a
single ``order_number`` for all its ``order_stops`` rows.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0037_seed_order_created_message'),
    ]

    operations = [
        migrations.AlterField(
            model_name='orderstop',
            name='ops_order_number',
            field=models.BigIntegerField(blank=True, db_index=True, null=True),
        ),
    ]
