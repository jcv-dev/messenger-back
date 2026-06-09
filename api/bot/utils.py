"""Shared utilities for the bot module."""

import logging
from asgiref.sync import sync_to_async

from django.contrib.auth.models import User

logger = logging.getLogger("api.bot")


def get_bot_user():
    return User.objects.filter(username="bot").first()


get_bot_user_async = sync_to_async(get_bot_user)
