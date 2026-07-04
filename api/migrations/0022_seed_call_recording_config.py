from django.db import migrations


def seed_call_recording_config(apps, schema_editor):
    BotConfig = apps.get_model('api', 'BotConfig')
    BotConfig.objects.get_or_create(
        key='call_recording_enabled',
        defaults={
            'value': False,
            'description': 'Grabar automáticamente todas las llamadas (entrantes y salientes).',
        },
    )


class Migration(migrations.Migration):
    dependencies = [
        ('api', '0021_add_call_recording_local_path'),
    ]

    operations = [
        migrations.RunPython(seed_call_recording_config, migrations.RunPython.noop),
    ]
