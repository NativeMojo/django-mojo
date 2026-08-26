"""Pure connection-default and explicit pool opt-in coverage."""

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
        f"health checks must still default on, got {default!r}"


@th.unit_test("conn defaults: explicit DATABASE_CONN_MAX_AGE opts persistence in")
def test_explicit_config_opts_in(opts):
    from mojo.db.config import apply_connection_defaults

    context = _context(DATABASE_CONN_MAX_AGE=300)
    apply_connection_defaults(context)
    default = context["DATABASES"]["default"]

    assert default["CONN_MAX_AGE"] == 300, \
        f"a configured DATABASE_CONN_MAX_AGE must be applied, got {default!r}"


@th.unit_test("conn defaults: configured zero is applied, not treated as absent")
def test_explicit_zero_applied(opts):
    from mojo.db.config import apply_connection_defaults

    context = _context(DATABASE_CONN_MAX_AGE=0)
    apply_connection_defaults(context)
    default = context["DATABASES"]["default"]

    assert default["CONN_MAX_AGE"] == 0, (
        f"DATABASE_CONN_MAX_AGE=0 must be written through (pgbouncer "
        f"transaction mode relies on it), got {default!r}"
    )


@th.unit_test("conn defaults: legacy psycopg2 engine alias gets health checks")
def test_legacy_engine_name_qualifies(opts):
    from mojo.db.config import apply_connection_defaults

    context = _context()
    context["DATABASES"]["default"]["ENGINE"] = (
        "django.db.backends.postgresql_psycopg2")
    apply_connection_defaults(context)
    default = context["DATABASES"]["default"]

    assert default["CONN_MAX_AGE"] == 0, \
        f"the legacy engine alias must retain per-request connections, got {default!r}"
    assert "pool" not in default.get("OPTIONS", {}), \
        f"the legacy engine alias must not receive an implicit pool, got {default!r}"
    assert default["CONN_HEALTH_CHECKS"] is True, \
        f"the legacy engine alias must receive health checks, got {default!r}"


@th.unit_test("conn defaults: explicit per-alias values are never overridden")
def test_explicit_values_win(opts):
    from mojo.db.config import apply_connection_defaults

    context = _context(DATABASE_CONN_MAX_AGE=300)
    context["DATABASES"]["default"]["CONN_MAX_AGE"] = 0
    context["DATABASES"]["default"]["CONN_HEALTH_CHECKS"] = False
    apply_connection_defaults(context)
    default = context["DATABASES"]["default"]

    assert default["CONN_MAX_AGE"] == 0, \
        f"an explicit per-alias CONN_MAX_AGE must survive, got {default!r}"
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

    context = _context(DATABASE_CONN_MAX_AGE=300)
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


@th.unit_test("conn defaults: PostgreSQL pooling requires explicit opt-in")
def test_postgresql_pooling_requires_explicit_opt_in(opts):
    from mojo.db.config import apply_connection_defaults

    context = _context()
    apply_connection_defaults(context)
    default = context["DATABASES"]["default"]

    assert default["CONN_MAX_AGE"] == 0, \
        f"PostgreSQL must retain per-request connections by default, got {default!r}"
    assert "pool" not in default.get("OPTIONS", {}), \
        f"native pooling must require an explicit project opt-in, got {default!r}"


@th.unit_test("conn defaults: project can explicitly disable pooling")
def test_project_can_disable_pool(opts):
    from mojo.db.config import apply_connection_defaults

    disabled = _context(DATABASE_POOL_OPTIONS=False)
    apply_connection_defaults(disabled)
    default = disabled["DATABASES"]["default"]
    assert default["CONN_MAX_AGE"] == 0, \
        f"disabling the pool must retain per-request connections, got {default!r}"
    assert "pool" not in default.get("OPTIONS", {}), \
        f"a false DATABASE_POOL_OPTIONS must disable automatic pooling, got {default!r}"
