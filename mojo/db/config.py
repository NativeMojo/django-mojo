"""Settings-time configuration for the optional database reader."""

import copy


LAST_SKIP_REASON = None

_ROUTER = "mojo.db.router.ReaderRouter"
_MIDDLEWARE = "mojo.middleware.db_reader.ReaderPinMiddleware"


def apply_reader_database(context):
    """Inject the reader alias, router, and request scope when configured."""
    global LAST_SKIP_REASON

    LAST_SKIP_REASON = None
    host = context.get("DATABASE_READER_HOST")
    if not host:
        return

    databases = context.get("DATABASES")
    if (not isinstance(databases, dict)
            or not isinstance(databases.get("default"), dict)):
        LAST_SKIP_REASON = "DATABASES must contain a default dictionary"
        return
    if "MIDDLEWARE" not in context:
        LAST_SKIP_REASON = "MIDDLEWARE is required for request-scoped reader routing"
        return

    if "reader" not in databases:
        reader = copy.deepcopy(databases["default"])
        reader["HOST"] = host
        if context.get("DATABASE_READER_PORT") is not None:
            reader["PORT"] = context["DATABASE_READER_PORT"]
        reader["CONN_MAX_AGE"] = context.get(
            "DATABASE_READER_CONN_MAX_AGE", 60)
        test_config = reader.get("TEST")
        if not isinstance(test_config, dict):
            test_config = {}
            reader["TEST"] = test_config
        test_config.setdefault("MIRROR", "default")
        databases["reader"] = reader

    routers = list(context.get("DATABASE_ROUTERS") or [])
    if _ROUTER not in routers:
        routers.append(_ROUTER)
    context["DATABASE_ROUTERS"] = routers

    middleware = list(context["MIDDLEWARE"] or [])
    if _MIDDLEWARE not in middleware:
        middleware.insert(0, _MIDDLEWARE)
    context["MIDDLEWARE"] = middleware
