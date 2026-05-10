import os
import django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()
from api.models import Message
msg = Message.objects.last()
print(f"ID: {msg.id}, Type: {msg.message_type}, Content: {msg.content}")
