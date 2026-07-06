"""
App configuration for API
"""
from django.apps import AppConfig


class ApiConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'api'

    def ready(self):
        from api.views import _start_sweeper
        try:
            _start_sweeper()
        except Exception:
            pass
