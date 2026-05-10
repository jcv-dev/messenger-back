import os
import django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()
from django.conf import settings
from api.models import Message
import urllib.parse

msg = Message.objects.last()
content_str = msg.content

# Check if content_str is a local URL and try to replace it with public URL
parsed = urllib.parse.urlparse(content_str)
if parsed.hostname in ['localhost', '127.0.0.1']:
    # find the first non-local allowed host
    public_host = next((h for h in settings.ALLOWED_HOSTS if h not in ['localhost', '127.0.0.1', '*']), None)
    if public_host:
        content_str = urllib.parse.urlunparse(('https', public_host, parsed.path, parsed.params, parsed.query, parsed.fragment))

print(f"Original: {msg.content}")
print(f"Public: {content_str}")
