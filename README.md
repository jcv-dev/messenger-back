# WhatsApp Business Messenger - Backend API

Django REST Framework backend for WhatsApp Business messaging platform with team collaboration features.

## Quick Start

```bash
# Create virtual environment
python -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env

# Run migrations
python manage.py migrate

# Create superuser
python manage.py createsuperuser

# Run server
python manage.py runserver
```

Server runs at `http://localhost:8000`

## Project Structure

```
backend/
├── config/           # Django project settings
│   ├── __init__.py
│   ├── settings.py   # Main settings
│   ├── urls.py       # URL routing
│   ├── wsgi.py       # WSGI config
│   └── asgi.py       # ASGI config
├── api/              # REST API application
│   ├── migrations/   # Database migrations
│   ├── models.py     # Data models
│   ├── views.py      # API views
│   ├── serializers.py # DRF serializers
│   ├── urls.py       # API routes
│   ├── admin.py      # Django admin config
│   └── apps.py       # App configuration
├── manage.py         # Django management
├── requirements.txt  # Dependencies
├── .env.example      # Environment template
└── .gitignore
```

## Database Models

### Team
- name: CharField
- description: TextField (optional)
- created_at: DateTimeField
- updated_at: DateTimeField

### TeamMember
- team: ForeignKey(Team)
- user: ForeignKey(User)
- role: Choice(admin, member)
- joined_at: DateTimeField

### Conversation
- team: ForeignKey(Team)
- whatsapp_id: CharField (unique)
- contact_name: CharField
- contact_phone: CharField
- last_message: TextField
- last_message_at: DateTimeField
- status: Choice(active, resolved, archived)
- created_at: DateTimeField
- updated_at: DateTimeField

### Message
- conversation: ForeignKey(Conversation)
- direction: Choice(inbound, outbound)
- message_type: CharField (text, image, document, etc.)
- content: TextField
- sender_name: CharField
- created_at: DateTimeField
- is_read: BooleanField

### ConversationTag
- conversation: ForeignKey(Conversation)
- assigned_to: ForeignKey(User, null=True)
- tag_name: CharField
- tag_color: CharField (Tailwind color classes)
- note: TextField
- expiry_type: Choice(1h, 5h, end_of_day, custom)
- expires_at: DateTimeField
- created_at: DateTimeField
- created_by: ForeignKey(User)
- is_active: BooleanField

## API Endpoints

### Authentication
```
POST /api-auth/
Body: { "username": "user", "password": "pass" }
Response: { "token": "xxxxx" }
```

### Teams
```
GET    /api/teams/                    # List teams
POST   /api/teams/                    # Create team
GET    /api/teams/{id}/               # Get team detail
POST   /api/teams/{id}/add_member/    # Add member
POST   /api/teams/{id}/remove_member/ # Remove member
```

### Conversations
```
GET    /api/conversations/                    # List all conversations
GET    /api/conversations/active_conversations/ # List active
GET    /api/conversations/{id}/               # Get detail
POST   /api/conversations/{id}/add_tag/       # Add tag
POST   /api/conversations/{id}/remove_tag/    # Remove tag
GET    /api/conversations/{id}/messages/      # Get messages
POST   /api/conversations/{id}/messages/      # Send message
```

### Users
```
GET    /api/users/current_user/  # Get logged-in user
GET    /api/users/team_users/    # Get team members
```

### Stickers
```
GET    /api/stickers/            # List your uploaded sticker images
POST   /api/stickers/            # Upload a sticker image (multipart/form-data)
GET    /api/stickers/{id}/       # Get sticker details
DELETE /api/stickers/{id}/       # Remove a sticker image
```

## Tag Management

### Creating Tags

```bash
curl -X POST http://localhost:8000/api/conversations/1/add_tag/ \
  -H "Authorization: Token YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "tag_name": "Billing Issue",
    "assigned_to_id": 2,
    "expiry_type": "1h",
    "tag_color": "red",
    "note": "Waiting for invoice"
  }'
```

### Expiry Types
- `1h`: Expires after 1 hour
- `5h`: Expires after 5 hours
- `end_of_day`: Expires at 23:59:59 UTC
- `custom`: Use `custom_expiry_minutes` field

### Removing Tags

Tags are not deleted, they're marked as inactive:

```bash
curl -X POST http://localhost:8000/api/conversations/1/remove_tag/ \
  -H "Authorization: Token YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"tag_id": 5}'
```

## Environment Variables

```env
# Django
SECRET_KEY=your-secret-key
DEBUG=True
ALLOWED_HOSTS=localhost,127.0.0.1

# Database (SQLite for dev, PostgreSQL for prod)
DB_ENGINE=django.db.backends.sqlite3
DB_NAME=db.sqlite3

# PostgreSQL (production)
# DB_ENGINE=django.db.backends.postgresql
# DB_NAME=domi_messager
# DB_USER=postgres
# DB_PASSWORD=password
# DB_HOST=localhost
# DB_PORT=5432

# CORS
CORS_ALLOWED_ORIGINS=http://localhost:5173

# Media uploads
MEDIA_URL=/media/
MEDIA_ROOT=media

# WhatsApp API (when ready)
WHATSAPP_BUSINESS_ACCOUNT_ID=
WHATSAPP_API_TOKEN=
WHATSAPP_PHONE_NUMBER=
```

## Running Migrations

```bash
# Show migrations
python manage.py showmigrations

# Create migration
python manage.py makemigrations

# Apply migration
python manage.py migrate

# Migrate specific app
python manage.py migrate api
```

## Django Admin

Access at `http://localhost:8000/admin`

Log in with your superuser credentials created during setup.

## Sample Data Creation

```python
python manage.py shell

from django.contrib.auth.models import User
from api.models import Team, TeamMember, Conversation

# Create users
user1 = User.objects.create_user('agent1', 'agent1@test.com', 'pass123')
user2 = User.objects.create_user('agent2', 'agent2@test.com', 'pass123')

# Create team
team = Team.objects.create(name='Support Team')
TeamMember.objects.create(team=team, user=user1, role='admin')
TeamMember.objects.create(team=team, user=user2)

# Create conversation
Conversation.objects.create(
    team=team,
    whatsapp_id='1234567890',
    contact_name='John Doe',
    contact_phone='+1234567890'
)
```

## Testing

```bash
python manage.py test

# Test specific app
python manage.py test api

# Verbose output
python manage.py test -v 2
```

## Deployment

### Production Checklist
- [ ] Set `DEBUG=False` in `.env`
- [ ] Generate strong `SECRET_KEY`
- [ ] Set up PostgreSQL database
- [ ] Configure `ALLOWED_HOSTS`
- [ ] Set `CORS_ALLOWED_ORIGINS` to your frontend domain
- [ ] Use environment variables for all sensitive data
- [ ] Set up HTTPS/SSL
- [ ] Configure static files serving
- [ ] Set up error logging

### Deploy with Gunicorn

```bash
pip install gunicorn
gunicorn config.wsgi:application
```

### Deploy with Docker

Create `Dockerfile`:
```dockerfile
FROM python:3.10
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000"]
```

## Troubleshooting

### Database Issues
```bash
# Reset database (development only)
rm db.sqlite3
python manage.py migrate
python manage.py createsuperuser
```

### Migration Conflicts
```bash
python manage.py makemigrations --merge
```

### Clear cache
```bash
python manage.py clear_cache
```

## API Response Format

### Success Response (200, 201)
```json
{
  "id": 1,
  "name": "Team Name",
  ...
}
```

### Error Response (400, 404, 500)
```json
{
  "error": "Error message"
}
```

## Authentication

All endpoints require token authentication:

```bash
curl -H "Authorization: Token YOUR_TOKEN" http://localhost:8000/api/teams/
```

Get token by logging in:
```bash
curl -X POST http://localhost:8000/api-auth/ \
  -d "username=user&password=pass"
```

## More Information

- [Django Documentation](https://docs.djangoproject.com/)
- [Django REST Framework](https://www.django-rest-framework.org/)
- [PostgreSQL](https://www.postgresql.org/)
