"""Settings-time database pooling and optional reader configuration."""

import copy
import os
from types import MappingProxyType

from django.core.exceptions import ImproperlyConfigured

from mojo.db.pool_identity import PoolIdentityError, application_name, validate_identity


LAST_SKIP_REASON = None
LAST_POOL_PLAN = None
LAST_POOL_DIAGNOSTIC = None

_SERVER_ENGINES = ("postgresql", "mysql", "oracle")
_POOL_OPTION_KEYS = {
    "min_size", "max_size", "timeout", "max_idle", "max_lifetime",
    "max_waiting", "reconnect_timeout",
}

CONN_MAX_AGE_DEFAULT = 0
CONN_HEALTH_CHECKS_DEFAULT = True
POOL_OPTIONS_DEFAULT = False
POOL_ERROR_MIDDLEWARE = "mojo.middleware.database_pool.DatabasePoolErrorMiddleware"


def _immutable(value):
    if isinstance(value, dict):
        return MappingProxyType({key: _immutable(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_immutable(item) for item in value)
    return value


def _positive_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _positive_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def _pool_intent(databases):
    aliases = []
    for name, alias in databases.items():
        if not isinstance(alias, dict):
            continue
        options = alias.get("OPTIONS")
        if isinstance(options, dict) and options.get("pool"):
            aliases.append(name)
    return aliases


def _strip_pools(databases):
    for alias in databases.values():
        if not isinstance(alias, dict):
            continue
        options = alias.get("OPTIONS")
        if isinstance(options, dict):
            options.pop("pool", None)


def _candidate_plan(context, databases, environ):
    pool_options = context.get("DATABASE_POOL_OPTIONS", POOL_OPTIONS_DEFAULT)
    embedded_aliases = _pool_intent(databases)
    enabled = pool_options is not False or bool(embedded_aliases)
    role = environ.get("MOJO_PROCESS_ROLE", "")
    launcher = environ.get("MOJO_PROCESS_LAUNCHER", "")
    default = databases.get("default") if isinstance(databases.get("default"), dict) else {}
    errors = []

    if enabled:
        if not isinstance(pool_options, dict):
            errors.append("DATABASE_POOL_OPTIONS must be a dictionary")
            clean_options = {}
        else:
            clean_options = copy.deepcopy(pool_options)
            unknown = sorted(set(clean_options) - _POOL_OPTION_KEYS)
            if unknown:
                errors.append("DATABASE_POOL_OPTIONS contains unsupported keys: " + ", ".join(unknown))
            if not _positive_int(clean_options.get("max_size")):
                errors.append("DATABASE_POOL_OPTIONS.max_size must be a positive integer")
            min_size = clean_options.get("min_size")
            if not isinstance(min_size, int) or isinstance(min_size, bool) or min_size < 0:
                errors.append("DATABASE_POOL_OPTIONS.min_size must be a non-negative integer")
            elif _positive_int(clean_options.get("max_size")) and min_size > clean_options["max_size"]:
                errors.append("DATABASE_POOL_OPTIONS.min_size cannot exceed max_size")
            if not _positive_number(clean_options.get("timeout")):
                errors.append("DATABASE_POOL_OPTIONS.timeout must be a positive number")
            for name in ("max_idle", "max_lifetime"):
                if name in clean_options and not _positive_number(clean_options[name]):
                    errors.append(f"DATABASE_POOL_OPTIONS.{name} must be a positive number")
            if "max_waiting" in clean_options and not _positive_int(clean_options["max_waiting"]):
                errors.append("DATABASE_POOL_OPTIONS.max_waiting must be a positive integer")
            if ("reconnect_timeout" in clean_options
                    and not _positive_number(clean_options["reconnect_timeout"])):
                errors.append("DATABASE_POOL_OPTIONS.reconnect_timeout must be a positive number")

        if context.get("DATABASE_POOL_ALIASES") != ["default"]:
            errors.append('DATABASE_POOL_ALIASES must be exactly ["default"]')
        if embedded_aliases:
            errors.append("pool intent must come from DATABASE_POOL_OPTIONS, not DATABASES aliases")
        if not default:
            errors.append("DATABASES.default must be a dictionary")
        elif "postgresql" not in (default.get("ENGINE") or ""):
            errors.append("DATABASES.default must use PostgreSQL")
        if default.get("CONN_MAX_AGE", 0) != 0:
            errors.append("DATABASES.default.CONN_MAX_AGE must be 0 when pooling")
        if context.get("DATABASE_CONN_MAX_AGE", 0) != 0:
            errors.append("DATABASE_CONN_MAX_AGE must be 0 when pooling")
        if not isinstance(context.get("MIDDLEWARE"), (list, tuple)):
            errors.append("MIDDLEWARE must be a list or tuple when pooling")
        api_workers = context.get("DATABASE_POOL_API_WORKERS")
        node_count = context.get("DATABASE_POOL_NODE_COUNT")
        observer_reserve = context.get("DATABASE_POOL_OBSERVER_RESERVE", 2)
        if not _positive_int(api_workers):
            errors.append("DATABASE_POOL_API_WORKERS must be a positive integer")
        if not _positive_int(node_count):
            errors.append("DATABASE_POOL_NODE_COUNT must be a positive integer")
        if not _positive_int(observer_reserve):
            errors.append("DATABASE_POOL_OBSERVER_RESERVE must be a positive integer")
        try:
            identity = validate_identity(context.get("DATABASE_POOL_IDENTITY"))
        except PoolIdentityError as error:
            errors.append(str(error))
            identity = {}
    else:
        clean_options = {}
        api_workers = None
        node_count = None
        observer_reserve = None
        identity = {}

    destination = {
        "engine": default.get("ENGINE", ""),
        "host": default.get("HOST", ""),
        "port": str(default.get("PORT", "") or ""),
        "name": default.get("NAME", ""),
    }
    return _immutable({
        "enabled": enabled,
        "valid": not errors,
        "errors": tuple(errors),
        "role": role,
        "launcher": launcher,
        "aliases": ("default",) if enabled else (),
        "options": clean_options,
        "api_workers": api_workers,
        "node_count": node_count,
        "observer_reserve": observer_reserve,
        "identity": identity,
        "destination": destination,
    })


def apply_connection_defaults(context, environ=None):
    """Apply safe defaults and an API-only validated psycopg pool."""
    global LAST_POOL_DIAGNOSTIC, LAST_POOL_PLAN

    databases = context.get("DATABASES")
    if not isinstance(databases, dict):
        LAST_POOL_PLAN = _immutable({
            "enabled": False, "valid": True, "errors": (), "role": "",
            "launcher": "", "aliases": (), "options": {},
            "api_workers": None, "node_count": None, "observer_reserve": None,
            "identity": {}, "destination": {},
        })
        LAST_POOL_DIAGNOSTIC = "pool disabled: DATABASES is not a dictionary"
        return

    environ = os.environ if environ is None else environ
    plan = _candidate_plan(context, databases, environ)
    LAST_POOL_PLAN = plan
    _strip_pools(databases)

    proven_api = plan["role"] == "api" and plan["launcher"] == "asgi"
    if plan["enabled"] and proven_api and not plan["valid"]:
        raise ImproperlyConfigured("; ".join(plan["errors"]))
    if plan["enabled"] and proven_api:
        from mojo.db.asgi_compat import install_thread_sensitive_error_responses

        install_thread_sensitive_error_responses()
        default = databases["default"]
        options = default.get("OPTIONS")
        if not isinstance(options, dict):
            options = {}
            default["OPTIONS"] = options
        options["pool"] = copy.deepcopy(dict(plan["options"]))
        options["application_name"] = application_name(
            dict(plan["identity"]), plan["role"], "default")
        default["ENGINE"] = "mojo.db.backends.postgresql"
        default["CONN_MAX_AGE"] = 0
        middleware = [
            item for item in context["MIDDLEWARE"]
            if item != POOL_ERROR_MIDDLEWARE
        ]
        cors = "mojo.middleware.cors.CORSMiddleware"
        insert_at = middleware.index(cors) + 1 if cors in middleware else 0
        middleware.insert(insert_at, POOL_ERROR_MIDDLEWARE)
        context["MIDDLEWARE"] = middleware
        LAST_POOL_DIAGNOSTIC = "pool enabled for role=api launcher=asgi alias=default"
    elif plan["enabled"]:
        reason = "invalid candidate" if not plan["valid"] else "process is not api/asgi"
        LAST_POOL_DIAGNOSTIC = (
            f"pool suppressed for role={plan['role'] or 'missing'} "
            f"launcher={plan['launcher'] or 'missing'}: {reason}"
        )[:240]
    else:
        LAST_POOL_DIAGNOSTIC = "pool disabled by DATABASE_POOL_OPTIONS=False"

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
        has_pool = isinstance(options, dict) and bool(options.get("pool"))
        if has_pool:
            alias["CONN_MAX_AGE"] = 0
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
