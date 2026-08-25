"""Settings-time configuration for the databases: connection-reuse defaults
for every server-backed alias, plus the optional database reader."""

import copy


LAST_SKIP_REASON = None

# Engines that hold a real server connection worth keeping alive. Substring
# match so legacy aliases ("postgresql_psycopg2") qualify too; sqlite and
# other file-backed engines are left untouched.
_SERVER_ENGINES = ("postgresql", "mysql", "oracle")

CONN_HEALTH_CHECKS_DEFAULT = True


def apply_connection_defaults(context):
    """Settings-driven ``CONN_MAX_AGE`` / ``CONN_HEALTH_CHECKS`` on server aliases.

    ``CONN_MAX_AGE`` is applied ONLY when ``DATABASE_CONN_MAX_AGE`` is
    explicitly configured — there is deliberately no framework default.
    Django's guidance is that persistent connections must stay disabled
    under ASGI: the ASGI handler runs each request's ORM work in a
    per-request ``ThreadSensitiveContext`` thread, a persistent
    connection survives the request, the thread exits, and the
    connection orphans until GC — leaking server slots under load.
    django-mojo projects serve HTTP through Django's ASGI application
    (``mojo.apps.realtime.routing``), so the safe default is Django's
    own (0, per-request connections). Opt in via
    ``DATABASE_CONN_MAX_AGE`` only for WSGI-style deployments.

    Connection REUSE for ASGI deployments belongs at the driver or
    pooler layer instead: Django 5.1+'s native psycopg 3 pool
    (``OPTIONS = {"pool": ...}`` — aliases using it are skipped here,
    since pooling rejects persistent connections at startup) or an
    external pooler such as pgbouncer (transaction mode requires
    ``CONN_MAX_AGE`` unset/0 as well).

    ``CONN_HEALTH_CHECKS`` still defaults True (override with
    ``DATABASE_CONN_HEALTH_CHECKS``): it is a no-op at age 0 and the
    correct pairing wherever persistence is explicitly enabled.
    An explicit per-alias value always wins over both keys.
    """
    databases = context.get("DATABASES")
    if not isinstance(databases, dict):
        return
    max_age = context.get("DATABASE_CONN_MAX_AGE")
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
        if max_age is not None:
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
