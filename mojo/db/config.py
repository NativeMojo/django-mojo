"""Settings-time configuration for the databases: connection-reuse defaults
for every server-backed alias, plus the optional database reader."""

import copy


LAST_SKIP_REASON = None

# Engines that hold a real server connection worth keeping alive. Substring
# match so legacy aliases ("postgresql_psycopg2") qualify too; sqlite and
# other file-backed engines are left untouched.
_SERVER_ENGINES = ("postgresql", "mysql", "oracle")

CONN_MAX_AGE_DEFAULT = 300
CONN_HEALTH_CHECKS_DEFAULT = True


def apply_connection_defaults(context):
    """Default ``CONN_MAX_AGE`` / ``CONN_HEALTH_CHECKS`` on server-backed aliases.

    Django's own default is ``CONN_MAX_AGE = 0`` — a fresh database connection
    (TCP + auth + optional TLS) on every request, a fixed per-request tax that
    projects have historically forgotten to configure away. Any alias that does
    not set the keys itself gets ``CONN_MAX_AGE`` from
    ``DATABASE_CONN_MAX_AGE`` (default 300) and ``CONN_HEALTH_CHECKS`` from
    ``DATABASE_CONN_HEALTH_CHECKS`` (default True, so a connection killed by an
    idle timeout or a database restart is replaced instead of surfacing as a
    500). An explicit per-alias value always wins.

    Set ``DATABASE_CONN_MAX_AGE = 0`` (settings profile or django.conf) to
    restore per-request connections — required when the alias points at a
    transaction-mode pooler such as pgbouncer. An alias using Django's native
    psycopg pool (``OPTIONS["pool"]``) is skipped entirely: pooling rejects
    persistent connections at startup.
    """
    databases = context.get("DATABASES")
    if not isinstance(databases, dict):
        return
    max_age = context.get("DATABASE_CONN_MAX_AGE", CONN_MAX_AGE_DEFAULT)
    health_checks = context.get(
        "DATABASE_CONN_HEALTH_CHECKS", CONN_HEALTH_CHECKS_DEFAULT)
    for alias in databases.values():
        if not isinstance(alias, dict):
            continue
        engine = alias.get("ENGINE") or ""
        if not any(name in engine for name in _SERVER_ENGINES):
            continue
        options = alias.get("OPTIONS")
        if isinstance(options, dict) and options.get("pool"):
            continue
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
