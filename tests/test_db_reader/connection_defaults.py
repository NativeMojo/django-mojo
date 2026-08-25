"""Settings-dict and Django integration coverage for database pooling."""

from testit import helpers as th


def _context(**overrides):
    context = {
        "DATABASES": {
            "default": {
                "ENGINE": "django.db.backends.postgresql",
                "HOST": "primary.internal",
                "PORT": "5432",
            },
        },
    }
    context.update(overrides)
    return context


@th.unit_test("conn defaults: non-PostgreSQL servers stay per-request")
def test_non_postgresql_server_stays_per_request(opts):
    from mojo.db.config import apply_connection_defaults

    context = _context()
    context["DATABASES"]["default"]["ENGINE"] = "django.db.backends.mysql"
    apply_connection_defaults(context)
    default = context["DATABASES"]["default"]

    assert default["CONN_MAX_AGE"] == 0, \
        f"a non-PostgreSQL alias must retain Django's per-request default, got {default!r}"
    assert "OPTIONS" not in default, \
        f"the PostgreSQL-only pool must not be applied to MySQL, got {default!r}"
    assert default["CONN_HEALTH_CHECKS"] is True, \
        f"a server-backed alias must default to health checks on, got {default!r}"


@th.unit_test("conn defaults: legacy psycopg2 engine alias qualifies")
def test_legacy_engine_name_qualifies(opts):
    from mojo.db.config import apply_connection_defaults

    context = _context()
    context["DATABASES"]["default"]["ENGINE"] = (
        "django.db.backends.postgresql_psycopg2")
    apply_connection_defaults(context)
    default = context["DATABASES"]["default"]

    assert default["CONN_MAX_AGE"] == 0, \
        f"the legacy engine alias must use pool-compatible connection age, got {default!r}"
    assert default["OPTIONS"]["pool"]["max_size"] == 4, \
        f"the postgresql_psycopg2 alias must receive the native pool, got {default!r}"


@th.unit_test("conn defaults: explicit per-alias values are never overridden")
def test_explicit_values_win(opts):
    from mojo.db.config import apply_connection_defaults

    context = _context()
    context["DATABASES"]["default"]["CONN_MAX_AGE"] = 0
    context["DATABASES"]["default"]["CONN_HEALTH_CHECKS"] = False
    apply_connection_defaults(context)
    default = context["DATABASES"]["default"]

    assert default["CONN_MAX_AGE"] == 0, \
        f"an explicit CONN_MAX_AGE must survive the defaults pass, got {default!r}"
    assert default["CONN_HEALTH_CHECKS"] is False, \
        f"an explicit CONN_HEALTH_CHECKS must survive, got {default!r}"
    assert "pool" not in default.get("OPTIONS", {}), \
        f"an explicit CONN_MAX_AGE must opt the alias out of pooling, got {default!r}"


@th.unit_test("conn defaults: project-level override keys are honored")
def test_project_override_keys(opts):
    from mojo.db.config import apply_connection_defaults

    context = _context(
        DATABASE_CONN_MAX_AGE=45,
        DATABASE_CONN_HEALTH_CHECKS=False,
    )
    apply_connection_defaults(context)
    default = context["DATABASES"]["default"]

    assert default["CONN_MAX_AGE"] == 45, \
        f"DATABASE_CONN_MAX_AGE must retain the legacy explicit lifetime, got {default!r}"
    assert default["CONN_HEALTH_CHECKS"] is False, \
        f"DATABASE_CONN_HEALTH_CHECKS must be honored, got {default!r}"
    assert "pool" not in default.get("OPTIONS", {}), \
        f"DATABASE_CONN_MAX_AGE must opt aliases out of automatic pooling, got {default!r}"


@th.unit_test("conn defaults: sqlite aliases are left untouched")
def test_sqlite_untouched(opts):
    from mojo.db.config import apply_connection_defaults

    context = _context()
    context["DATABASES"]["local"] = {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": "/tmp/db.sqlite3",
    }
    apply_connection_defaults(context)
    local = context["DATABASES"]["local"]

    assert "CONN_MAX_AGE" not in local, \
        f"sqlite aliases must not receive connection defaults, got {local!r}"
    assert "CONN_HEALTH_CHECKS" not in local, \
        f"sqlite aliases must not receive health-check defaults, got {local!r}"


@th.unit_test("conn defaults: explicit native psycopg pool is preserved")
def test_explicit_pool_is_preserved(opts):
    from mojo.db.config import apply_connection_defaults

    context = _context()
    context["DATABASES"]["default"]["OPTIONS"] = {"pool": True}
    apply_connection_defaults(context)
    default = context["DATABASES"]["default"]

    assert default["OPTIONS"]["pool"] is True, \
        f"an explicit pool configuration must survive unchanged, got {default!r}"
    assert default["CONN_MAX_AGE"] == 0, \
        f"an explicit pool must receive a compatible CONN_MAX_AGE, got {default!r}"
    assert default["CONN_HEALTH_CHECKS"] is True, \
        f"an explicit pool must still receive health-check defaults, got {default!r}"


@th.unit_test("conn defaults: writer and derived reader get independent pools")
def test_reader_alias_gets_pool(opts):
    from mojo.db.config import apply_connection_defaults, apply_reader_database

    context = _context(DATABASE_READER_HOST="reader.internal")
    context["MIDDLEWARE"] = ["project.middleware.Existing"]
    apply_reader_database(context)
    apply_connection_defaults(context)
    databases = context["DATABASES"]

    assert databases["reader"]["CONN_MAX_AGE"] == 0, \
        f"the reader must use a pool-compatible connection age: {databases['reader']!r}"
    assert databases["reader"]["CONN_HEALTH_CHECKS"] is True, \
        f"the reader alias must still gain health checks: {databases['reader']!r}"
    assert databases["default"]["CONN_MAX_AGE"] == 0, \
        f"the default alias must use a pool-compatible age: {databases['default']!r}"
    assert databases["reader"]["OPTIONS"]["pool"] == databases["default"]["OPTIONS"]["pool"], \
        f"writer and reader must receive the same bounded defaults: {databases!r}"
    assert databases["reader"]["OPTIONS"]["pool"] is not databases["default"]["OPTIONS"]["pool"], \
        "writer and reader pool dictionaries must be independent copies"


@th.unit_test("conn defaults: ASGI PostgreSQL uses a bounded native pool")
def test_postgresql_defaults_to_bounded_pool(opts):
    from mojo.db.config import apply_connection_defaults

    context = _context()
    apply_connection_defaults(context)
    default = context["DATABASES"]["default"]

    assert default["CONN_MAX_AGE"] == 0, \
        f"native pooling requires CONN_MAX_AGE=0, got {default!r}"
    assert default["OPTIONS"]["pool"] == {
        "min_size": 1,
        "max_size": 4,
        "timeout": 5,
        "max_idle": 300,
        "max_lifetime": 1800,
    }, f"PostgreSQL must receive the bounded ASGI pool defaults, got {default!r}"


@th.unit_test("conn defaults: project can replace or disable pool defaults")
def test_project_pool_options_are_honored(opts):
    from mojo.db.config import apply_connection_defaults

    custom = {"min_size": 0, "max_size": 2, "timeout": 3}
    context = _context(DATABASE_POOL_OPTIONS=custom)
    apply_connection_defaults(context)
    default = context["DATABASES"]["default"]

    assert default["OPTIONS"]["pool"] == custom, \
        f"DATABASE_POOL_OPTIONS must replace the defaults, got {default!r}"
    assert default["OPTIONS"]["pool"] is not custom, \
        "pool options must be copied instead of mutating the project setting"

    disabled = _context(DATABASE_POOL_OPTIONS=False)
    apply_connection_defaults(disabled)
    default = disabled["DATABASES"]["default"]
    assert default["CONN_MAX_AGE"] == 0, \
        f"disabling the pool must retain per-request connections, got {default!r}"
    assert "pool" not in default.get("OPTIONS", {}), \
        f"a false DATABASE_POOL_OPTIONS must disable automatic pooling, got {default!r}"


@th.django_unit_test("conn defaults: Django opens the configured psycopg pool")
def test_django_pool_is_available(opts):
    from django.db import connection

    pool = connection.pool
    assert pool is not None, \
        "the generated ASGI test project must expose Django's native psycopg pool"
    assert pool.min_size == 1, \
        f"the live pool must use min_size=1, got {pool.min_size!r}"
    assert pool.max_size == 4, \
        f"the live pool must use max_size=4, got {pool.max_size!r}"
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        value = cursor.fetchone()[0]
    assert value == 1, \
        f"the live pooled connection must execute a query, got {value!r}"
