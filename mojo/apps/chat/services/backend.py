"""File-only read-state selection; never read deployment controls from DB."""
from django.core.exceptions import ImproperlyConfigured
from mojo.helpers.settings import settings


def read_backend(value=None):
    value = settings.get_static("CHAT_READ_STATE_BACKEND", "legacy") if value is None else value
    if value not in ("legacy", "cursor"):
        raise ImproperlyConfigured("CHAT_READ_STATE_BACKEND must be legacy or cursor")
    return value


def ordinary_save_fields(instance, kwargs, protected):
    """Exclude service-owned facts from the actual UPDATE, not just validation.

    A stale ordinary model save can race a sender/read/maintenance transaction.
    Refreshing fields before a full UPDATE still loses that race. Keep all
    loaded ordinary concrete fields (including auto_now and trusted joined_at)
    while leaving protected facts entirely out of the database statement.
    """
    fields = kwargs.get("update_fields")
    if fields is not None:
        if set(fields).intersection(protected):
            raise ValueError("Service-owned chat fields require chat services")
        return
    kwargs["update_fields"] = [
        field.name for field in instance._meta.concrete_fields
        if not field.primary_key and not field.generated
        and field.name not in protected and field.attname not in protected
        and field.attname in instance.__dict__]
