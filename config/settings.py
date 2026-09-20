"""
Django settings for WhatsApp Messenger app
"""

from pathlib import Path
import os

try:
    from decouple import Config, RepositoryEnv

    dotenv_file = os.environ.get('DOTENV_FILE')
    if dotenv_file and os.path.isfile(dotenv_file):
        _cfg = Config(RepositoryEnv(dotenv_file))
        config = _cfg.get
    else:
        from decouple import config
except ModuleNotFoundError:
    def config(key, default=None, cast=None):
        value = os.environ.get(key, default)
        if cast is None or value is None:
            return value
        return cast(value)

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = config('SECRET_KEY')

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = config('DEBUG', default=False, cast=bool)

_raw_allowed = config('ALLOWED_HOSTS', default='')
ALLOWED_HOSTS = [h.strip() for h in _raw_allowed.split(',') if h.strip()]

# Application definition
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'rest_framework',
    'rest_framework.authtoken',
    'corsheaders',
    'django.contrib.postgres',
    'api',
]

MIGRATION_MODULES = {
    'auth': 'migration_overrides.auth',
}

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
SESSION_COOKIE_SECURE = config('SESSION_COOKIE_SECURE', default=not DEBUG, cast=bool)
CSRF_COOKIE_SECURE = config('CSRF_COOKIE_SECURE', default=not DEBUG, cast=bool)
SECURE_HSTS_SECONDS = config('SECURE_HSTS_SECONDS', default=31536000, cast=int)
SECURE_HSTS_INCLUDE_SUBDOMAINS = config('SECURE_HSTS_INCLUDE_SUBDOMAINS', default=True, cast=bool)
SECURE_SSL_REDIRECT = config('SECURE_SSL_REDIRECT', default=False, cast=bool)

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

# Database
DATABASES = {
    'default': {
        'ENGINE': config('DB_ENGINE', default='django.db.backends.sqlite3'),
        'NAME': config('DB_NAME', default=BASE_DIR / 'db.sqlite3'),
        'USER': config('DB_USER', default=''),
        'PASSWORD': config('DB_PASSWORD', default=''),
        'HOST': config('DB_HOST', default=''),
        'PORT': config('DB_PORT', default=''),
        'CONN_MAX_AGE': config('DB_CONN_MAX_AGE', default=0, cast=int),
    }
}
_db_engine = DATABASES['default']['ENGINE']
if 'postgresql' in _db_engine or 'postgis' in _db_engine:
    DATABASES['default']['OPTIONS'] = {
        'sslmode': config('DB_SSLMODE', default='prefer'),
    }

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

# Internationalization
LANGUAGE_CODE = 'es-co'
TIME_ZONE = 'America/Bogota'
USE_I18N = True
USE_TZ = True

# Static files (CSS, JavaScript, Images)
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

DATA_UPLOAD_MAX_MEMORY_SIZE = config('DATA_UPLOAD_MAX_MEMORY_SIZE', default=52428800, cast=int)
FILE_UPLOAD_MAX_MEMORY_SIZE = config('FILE_UPLOAD_MAX_MEMORY_SIZE', default=52428800, cast=int)

# Default primary key field type
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework.authentication.TokenAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_THROTTLE_CLASSES': [
        'rest_framework.throttling.UserRateThrottle',
    ],
    'DEFAULT_THROTTLE_RATES': {
        'login': '5/min',
        'user': '500/min',
        # Phase 6: one LLM call per order draft, 10 drafts/min/user
        'order_draft': '10/min',
    },
}

# CORS
_raw_cors = config('CORS_ALLOWED_ORIGINS', default='')
CORS_ALLOWED_ORIGINS = [h.strip() for h in _raw_cors.split(',') if h.strip()]
CORS_ALLOW_CREDENTIALS = False

_raw_csrf = config('CSRF_TRUSTED_ORIGINS', default='')
CSRF_TRUSTED_ORIGINS = [h.strip() for h in _raw_csrf.split(',') if h.strip()]

# WhatsApp API Configuration (placeholder)
WHATSAPP_BUSINESS_ACCOUNT_ID = config('WHATSAPP_BUSINESS_ACCOUNT_ID', default='')
WHATSAPP_APP_ID = config('WHATSAPP_APP_ID', default='')
WHATSAPP_API_TOKEN = config('WHATSAPP_API_TOKEN', default='')
WHATSAPP_PHONE_NUMBER = config('WHATSAPP_PHONE_NUMBER', default='')
WHATSAPP_PHONE_NUMBER_ID = config('WHATSAPP_PHONE_NUMBER_ID', default='')
# Base URL for outbound Graph API calls; overridable so tests/live harnesses can
# point the send path at a local mock (Phase 5 fallback verification).
WHATSAPP_GRAPH_BASE_URL = config(
    'WHATSAPP_GRAPH_BASE_URL', default='https://graph.facebook.com/v20.0',
).rstrip('/')
WEBHOOK_TOKEN = config('WEBHOOK_TOKEN', default='')
WHATSAPP_APP_SECRET = config('WHATSAPP_APP_SECRET', default='')
WA_RATE_LIMIT_THRESHOLD = config('WA_RATE_LIMIT_THRESHOLD', default=70, cast=int)

# TURN/STUN server for WebRTC calling
TURN_SERVER_URL = config('TURN_SERVER_URL', default='turn:localhost:3478')
TURN_SERVER_USERNAME = config('TURN_SERVER_USERNAME', default='domi')
TURN_SERVER_CREDENTIAL = config('TURN_SERVER_CREDENTIAL', default='')

# Message retention in minutes — messages and their media files older than this are deleted
MESSAGE_RETENTION_MINUTES = config('MESSAGE_RETENTION_MINUTES', default=0, cast=int)

# Web Push (VAPID) for browser push notifications
VAPID_PRIVATE_KEY = config('VAPID_PRIVATE_KEY', default='')
VAPID_PUBLIC_KEY = config('VAPID_PUBLIC_KEY', default='')
VAPID_CLAIMS_EMAIL = config('VAPID_CLAIMS_EMAIL', default='admin@domi.app')

REDIS_URL = config('REDIS_URL', default='redis://localhost:6379/0')

if REDIS_URL:
    CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.redis.RedisCache',
            'LOCATION': REDIS_URL,
        },
        'throttle': {
            'BACKEND': 'django.core.cache.backends.redis.RedisCache',
            'LOCATION': REDIS_URL,
        },
    }
else:
    CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        },
        'throttle': {
            'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        },
    }

# Bot / Calculator / Gemini
DOMII_CALCULATOR_URL = config('DOMII_CALCULATOR_URL', default='http://calculator:8000')
DOMII_CALCULATOR_API_KEY = config('DOMII_CALCULATOR_API_KEY', default='')
GEMINI_API_KEY = config('GEMINI_API_KEY', default='')

# Ops integration (Domiitulua public API v1) — server-to-server only
OPS_API_URL = config('OPS_API_URL', default='')
OPS_API_KEY = config('OPS_API_KEY', default='')
OPS_TIMEOUT = config('OPS_TIMEOUT', default=15, cast=float)
# HMAC secret for ops → Messager webhooks (X-Signature)
INTEGRATION_WEBHOOK_SECRET = config('INTEGRATION_WEBHOOK_SECRET', default='')

# DeepSeek (OpenAI-compatible) for the button-driven order draft.
# The draft runs on V4.1 Flash with thinking disabled (plan Phase 6); the
# provider exposes it as ``deepseek-flash``.
ORDER_LLM_BASE_URL = config('ORDER_LLM_BASE_URL', default='https://api.deepseek.com/v1')
ORDER_LLM_API_KEY = config('ORDER_LLM_API_KEY', default='')
ORDER_LLM_MODEL = config('ORDER_LLM_MODEL', default='deepseek-flash')
ORDER_LLM_TIMEOUT = config('ORDER_LLM_TIMEOUT', default=30, cast=int)
ORDER_LLM_TEMPERATURE = config('ORDER_LLM_TEMPERATURE', default=0.2, cast=float)
# Send ``thinking: {"type": "disabled"}`` so Flash does not burn tokens on
# reasoning; set to false for providers that reject the parameter.
ORDER_LLM_DISABLE_THINKING = config('ORDER_LLM_DISABLE_THINKING', default=True, cast=bool)
# Order draft rounds: each draft confirms addresses through the calculator tools
ORDER_LLM_MAX_TOOL_ROUNDS = config('ORDER_LLM_MAX_TOOL_ROUNDS', default=4, cast=int)

# Bot operating hours — used in system prompt and FAQ router
BOT_OPERATING_HOURS = config(
    'BOT_OPERATING_HOURS',
    default='Lunes a sábado 8:00 AM a 8:00 PM, domingos y festivos 9:00 AM a 6:00 PM',
)

# Bot hardening
BOT_MAX_USER_MESSAGE_LENGTH = config('BOT_MAX_USER_MESSAGE_LENGTH', default=1000, cast=int)
BOT_LLM_TEMPERATURE = config('BOT_LLM_TEMPERATURE', default=0.25, cast=float)
BOT_LLM_MAX_OUTPUT_TOKENS = config('BOT_LLM_MAX_OUTPUT_TOKENS', default=1024, cast=int)
BOT_LLM_RETRY_COUNT = config('BOT_LLM_RETRY_COUNT', default=3, cast=int)
BOT_TOOLS_CACHE_TTL = config('BOT_TOOLS_CACHE_TTL', default=300, cast=int)
BOT_INBOUND_RATE_LIMIT = config('BOT_INBOUND_RATE_LIMIT', default=10, cast=int)
_raw_url_domains = config('BOT_ALLOWED_OUTPUT_URL_DOMAINS', default='')
BOT_ALLOWED_OUTPUT_URL_DOMAINS = [h.strip() for h in _raw_url_domains.split(',') if h.strip()]
BOT_TESTING_WARNING = config('BOT_TESTING_WARNING', default=False, cast=bool)
BOT_ESCALATE_ORDERS = config('BOT_ESCALATE_ORDERS', default=False, cast=bool)
BOT_ENABLED = config('BOT_ENABLED', default=True, cast=bool)

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'json': {
            '()': 'pythonjsonlogger.jsonlogger.JsonFormatter',
            'format': '%(asctime)s %(name)s %(levelname)s %(message)s',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'json',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': config('LOG_LEVEL', default='WARNING'),
    },
    'loggers': {
        'api': {'handlers': ['console'], 'level': 'INFO', 'propagate': False},
        'django': {'handlers': ['console'], 'level': 'INFO', 'propagate': False},
        'django.request': {'handlers': ['console'], 'level': 'WARNING', 'propagate': False},
        'django.server': {'handlers': ['console'], 'level': 'WARNING', 'propagate': False},
    },
}
