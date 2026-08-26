"""The sole async ORM boundary for realtime WebSocket processing."""

from django.apps import apps
from django.db.models import Model, QuerySet

from mojo.helpers.async_db import DatabaseExecutor
from mojo.helpers.settings import settings


DEFAULT_DATABASE_WORKERS = 4
MIN_DATABASE_WORKERS = 1
MAX_DATABASE_WORKERS = 32
IDENTITY_HOOKS = (
    "on_realtime_connection",
    "on_realtime_connected",
    "on_realtime_disconnected",
    "on_realtime_can_subscribe",
    "on_realtime_message",
)


def resolve_database_workers(value):
    """Coerce and clamp the file-only realtime DB worker count."""
    if value is None:
        return DEFAULT_DATABASE_WORKERS
    try:
        workers = int(value)
    except (TypeError, ValueError):
        return DEFAULT_DATABASE_WORKERS
    return max(MIN_DATABASE_WORKERS, min(MAX_DATABASE_WORKERS, workers))


def _configured_database_workers():
    try:
        value = settings.get_static(
            "WS_DATABASE_WORKERS", DEFAULT_DATABASE_WORKERS, kind="int")
    except Exception:
        value = DEFAULT_DATABASE_WORKERS
    return resolve_database_workers(value)


database_executor = DatabaseExecutor(_configured_database_workers())


async def run_database(func, *args, **kwargs):
    """Run one fully bounded synchronous database unit."""
    return await database_executor.run(func, *args, **kwargs)


def serialize_identity(instance):
    """Return the plain, reloadable descriptor for a saved Django identity."""
    if not isinstance(instance, Model):
        raise ValueError("realtime identities must be saved Django model instances")
    if instance.pk is None or instance._state.adding:
        raise ValueError("realtime identities must be saved before authentication")
    wire_id = instance.pk
    if not isinstance(wire_id, (bool, int, float, str)):
        wire_id = str(wire_id)
    return {
        "model": instance._meta.label_lower,
        "pk": str(instance.pk),
        "id": wire_id,
        "hooks": [
            name for name in IDENTITY_HOOKS
            if callable(getattr(instance, name, None))
        ],
    }


def identity_pk(identity):
    if not isinstance(identity, dict):
        return None
    return identity.get("id", identity.get("pk"))


def identity_has_hook(identity, name):
    if not isinstance(identity, dict):
        return False
    return name in (identity.get("hooks") or [])


def _load_identity(identity):
    if not isinstance(identity, dict):
        raise ValueError("invalid realtime identity descriptor")
    label = identity.get("model")
    raw_pk = identity.get("pk")
    if not isinstance(label, str) or not label or raw_pk is None:
        raise ValueError("invalid realtime identity descriptor")
    model = apps.get_model(label)
    if model is None:
        raise ValueError(f"unknown realtime identity model: {label}")
    pk = model._meta.pk.to_python(raw_pk)
    return model._default_manager.get(pk=pk)


def _plain_result(value):
    """Materialize a hook result and reject database-bound/lazy objects."""
    if isinstance(value, (Model, QuerySet)):
        raise TypeError("realtime database units may return plain data only")
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {
            _plain_result(key): _plain_result(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_plain_result(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_plain_result(item) for item in value)
    raise TypeError(
        f"realtime database units may not return {type(value).__name__}")


def _call_identity_hook(identity, name, args, kwargs):
    instance = _load_identity(identity)
    hook = getattr(instance, name, None)
    if not callable(hook):
        return None
    return _plain_result(hook(*args, **kwargs))


async def run_identity_hook(identity, name, *args, **kwargs):
    """Reload an identity, call one hook, and return only materialized data."""
    return await run_database(_call_identity_hook, identity, name, args, kwargs)


async def get_database_setting(name, default=None, kind=None):
    """Resolve a potentially DB-backed runtime setting inside the DB unit."""
    return await run_database(settings.get, name, default, kind=kind)
