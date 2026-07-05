# Generated manually for AgentTakeRecord model
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0028_alter_conversationtake_duration_minutes_and_more'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='AgentTakeRecord',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('taken_at', models.DateTimeField()),
                ('released_at', models.DateTimeField(null=True, blank=True)),
                ('first_response_at', models.DateTimeField(null=True, blank=True)),
                ('duration_minutes', models.PositiveIntegerField(default=10)),
                ('conversation', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='take_records', to='api.conversation')),
                ('agent', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='take_records', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'Agent Take Record',
                'verbose_name_plural': 'Agent Take Records',
                'ordering': ['-taken_at'],
                'indexes': [
                    models.Index(fields=['agent', 'taken_at'], name='takerecord_agent_taken_idx'),
                    models.Index(fields=['conversation', 'taken_at'], name='takerecord_conv_taken_idx'),
                ],
            },
        ),
    ]
