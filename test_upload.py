import os
import django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()
from django.conf import settings
from api.models import Message
import urllib.parse
import mimetypes
import uuid
import urllib.request
import json

def upload_media_to_whatsapp(file_path, phone_number_id, token):
    url = f"https://graph.facebook.com/v20.0/{phone_number_id}/media"
    boundary = uuid.uuid4().hex
    
    mime_type, _ = mimetypes.guess_type(file_path)
    if not mime_type:
        mime_type = 'application/octet-stream'
        
    with open(file_path, 'rb') as f:
        file_data = f.read()
        
    filename = os.path.basename(file_path)
    
    body = (
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"messaging_product\"\r\n\r\n"
        f"whatsapp\r\n"
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
        f"Content-Type: {mime_type}\r\n\r\n"
    ).encode('utf-8') + file_data + f"\r\n--{boundary}--\r\n".encode('utf-8')
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": f"multipart/form-data; boundary={boundary}"
    }
    
    req = urllib.request.Request(url, data=body, headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req) as response:
            res_data = json.loads(response.read().decode())
            return res_data.get('id')
    except urllib.error.HTTPError as e:
        print(f"HTTPError: {e.read().decode()}")
        raise e

msg = Message.objects.last()
content_str = msg.content

parsed = urllib.parse.urlparse(content_str)
media_url = getattr(settings, 'MEDIA_URL', '/media/')
if parsed.path.startswith(media_url):
    relative_path = parsed.path[len(media_url):]
    # Remove leading slashes to prevent os.path.join from treating it as absolute
    relative_path = relative_path.lstrip('/')
    file_path = os.path.join(settings.MEDIA_ROOT, relative_path)
    print(f"File path: {file_path}")
    if os.path.exists(file_path):
        phone_number_id = settings.WHATSAPP_PHONE_NUMBER_ID
        token = settings.WHATSAPP_API_TOKEN
        media_id = upload_media_to_whatsapp(file_path, phone_number_id, token)
        print(f"Success! Uploaded media ID: {media_id}")
    else:
        print("File does not exist")
else:
    print("Not a media URL")
