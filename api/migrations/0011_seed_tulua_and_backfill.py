from django.db import migrations
from django.utils.text import slugify


def seed_tulua(apps, schema_editor):
    CityGroup = apps.get_model('api', 'CityGroup')
    Conversation = apps.get_model('api', 'Conversation')
    UserProfile = apps.get_model('api', 'UserProfile')
    User = apps.get_model('auth', 'User')

    tulua, _ = CityGroup.objects.get_or_create(
        name='Tuluá',
        defaults={'slug': slugify('Tuluá')},
    )

    Conversation.objects.filter(group__isnull=True).update(group=tulua)

    for user in User.objects.filter(profile__isnull=True):
        UserProfile.objects.get_or_create(user=user, defaults={'group': tulua})

    UserProfile.objects.filter(group__isnull=True).update(group=tulua)


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0010_citygroup_conversation_group_userprofile'),
    ]

    operations = [
        migrations.RunPython(seed_tulua, migrations.RunPython.noop),
    ]
