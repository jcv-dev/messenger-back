from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0007_add_perf_indexes'),
    ]

    operations = [
        migrations.AddField(
            model_name='message',
            name='context_message',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='replies', to='api.message'),
        ),
    ]
