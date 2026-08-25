"""Settings-time database pooling and optional reader configuration."""

import copy


LAST_SKIP_REASON = None

# Engines that hold a real server connection. Substring matching keeps legacy
# aliases ("postgresql_psycopg2") working; file-backed engines stay untouched.
_SERVER_ENGINES = ("postgresql", "mysql", "oracle")

CONN_MAX_AGE_DEFAULT = 0
CONN_HEALTH_CHECKS_DEFAULT = True
POOL_OPTIONS_DEFAULT = False


def apply_connection_defaults(context):
    """Apply explicit psycopg pools and safe connection defaults.

    Django advises disabling persistent connections under ASGI. PostgreSQL
    aliases therefore retain per-request connections and ``CONN_MAX_AGE = 0``
    by default. ``DATABASE_POOL_OPTIONS`` explicitly enables a native psycopg
    pool for otherwise unconfigured PostgreSQL aliases.

    Explicit alias ``CONN_MAX_AGE`` / ``OPTIONS["pool"]`` values always win.
    Setting the legacy ``DATABASE_CONN_MAX_AGE`` key selects persistent
    connections instead. MySQL and Oracle retain Django's per-request default
    because Django's native pool is PostgreSQL-only.
    """
    databases = context.get("DATABASES")
    if not isinstance(databases, dict):
        return
    max_age = context.get("DATABASE_CONN_MAX_AGE", CONN_MAX_AGE_DEFAULT)
    max_age_configured = "DATABASE_CONN_MAX_AGE" in context
    health_checks = context.get(
        "DATABASE_CONN_HEALTH_CHECKS", CONN_HEALTH_CHECKS_DEFAULT)
    pool_options = context.get("DATABASE_POOL_OPTIONS", POOL_OPTIONS_DEFAULT)
    for alias in databases.values():
        if not isinstance(alias, dict):
            continue
        engine = alias.get("ENGINE") or ""
        if not any(name in engine for name in _SERVER_ENGINES):
            continue
        options = alias.get("OPTIONS")
        explicit_pool = isinstance(options, dict) and "pool" in options
        explicit_max_age = "CONN_MAX_AGE" in alias
        use_default_pool = (
            "postgresql" in engine
            and pool_options
            and not explicit_pool
            and not explicit_max_age
            and not max_age_configured
        )

        if use_default_pool:
            if not isinstance(options, dict):
                options = {}
                alias["OPTIONS"] = options
            options["pool"] = copy.deepcopy(pool_options)
            alias["CONN_MAX_AGE"] = 0
        elif explicit_pool and options.get("pool"):
            # Django rejects a native pool combined with persistent connections.
            alias.setdefault("CONN_MAX_AGE", 0)
        else:
            alias.setdefault("CONN_MAX_AGE", max_age)
        alias.setdefault("CONN_HEALTH_CHECKS", health_checks)

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
        if "DATABASE_READER_CONN_MAX_AGE" in context:
            # This legacy override explicitly selects persistent connections
            # for the reader, so it cannot also inherit a native psycopg pool.
            reader["CONN_MAX_AGE"] = context["DATABASE_READER_CONN_MAX_AGE"]
            options = reader.get("OPTIONS")
            if isinstance(options, dict):
                options.pop("pool", None)
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
