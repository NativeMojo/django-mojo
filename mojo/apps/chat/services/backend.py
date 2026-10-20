"""File-only read-state selection; never read deployment controls from DB."""
from django.core.exceptions import ImproperlyConfigured
from mojo.helpers.settings import settings


def read_backend(value=None):
    value = settings.get_static("CHAT_READ_STATE_BACKEND", "legacy") if value is None else value
    if value not in ("legacy", "cursor"):
        raise ImproperlyConfigured("CHAT_READ_STATE_BACKEND must be legacy or cursor")
    return value
