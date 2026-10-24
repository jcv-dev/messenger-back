"""Switch the auto-reply marketing window from hours to minutes (2 minutes).

Auto-responders answer marketing templates almost immediately, so a 72-hour
window was far too broad. The guard now measures the window in minutes and the
seeded config moves to the new key while dropping the old one.
"""

from django.db import migrations

DEFAULT_WINDOW_MINUTES = 2
OLD_DEFAULT_WINDOW_HOURS = 72


def to_minutes(apps, schema_editor):
    BotConfig = apps.get_model('api', 'BotConfig')
    config = BotConfig.objects.filter(key='auto_reply_guard').first()
    if not config:
        return
    value = dict(config.value or {})
    value.pop('marketing_window_hours', None)
    value['marketing_window_minutes'] = DEFAULT_WINDOW_MINUTES
    config.value = value
    config.save(update_fields=['value'])


def to_hours(apps, schema_editor):
    BotConfig = apps.get_model('api', 'BotConfig')
    config = BotConfig.objects.filter(key='auto_reply_guard').first()
    if not config:
        return
    value = dict(config.value or {})
    value.pop('marketing_window_minutes', None)
    value['marketing_window_hours'] = OLD_DEFAULT_WINDOW_HOURS
    config.value = value
    config.save(update_fields=['value'])


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0047_auto_reply_guard'),
    ]

    operations = [
        migrations.RunPython(to_minutes, to_hours),
    ]
