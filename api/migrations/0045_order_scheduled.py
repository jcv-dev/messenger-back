"""Scheduled orders (pedidos programados).

``Order`` mirrors the ops schedule: ``scheduled_for`` (aware) plus
``scheduled_released`` for the case where a pre-assigned courier was released
to libre at activation. ``programado`` is added to the status choices.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0044_sla_alerts'),
    ]

    operations = [
        migrations.AddField(
            model_name='order',
            name='scheduled_for',
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name='order',
            name='scheduled_released',
            field=models.BooleanField(default=False),
        ),
        migrations.AlterField(
            model_name='order',
            name='status',
            field=models.CharField(
                choices=[
                    ('draft', 'Borrador'),
                    ('pending', 'Enviando'),
                    ('failed', 'Fallido'),
                    ('programado', 'Programado'),
                    ('disponible', 'Buscando domiciliario'),
                    ('asignado', 'Asignado'),
                    ('confirmado', 'Confirmado'),
                    ('en_ruta', 'En camino'),
                    ('entregado', 'Entregado'),
                    ('cancelado', 'Cancelado'),
                ],
                db_index=True,
                default='draft',
                max_length=20,
            ),
        ),
    ]
