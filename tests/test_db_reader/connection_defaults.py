"""Pure settings-dict coverage for connection-reuse defaults.

CONN_MAX_AGE has NO framework default: persistent connections leak under
ASGI (per-request ThreadSensitiveContext threads orphan them), and
django-mojo projects serve HTTP over ASGI. The key applies only when
DATABASE_CONN_MAX_AGE is explicitly configured. CONN_HEALTH_CHECKS still
defaults True (no-op at age 0, correct pairing when persistence is opted
into).
"""

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


@th.unit_test("conn defaults: no CONN_MAX_AGE unless explicitly configured")
def test_no_conn_max_age_by_default(opts):
    from mojo.db.config import apply_connection_defaults

    context = _context()
    apply_connection_defaults(context)
    default = context["DATABASES"]["default"]

    assert "CONN_MAX_AGE" not in default, (
        f"CONN_MAX_AGE must have no framework default (ASGI leak — Django "
        f"guidance), got {default!r}"
    )
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

    assert default["CONN_HEALTH_CHECKS"] is True, \
        "the postgresql_psycopg2 legacy engine name must qualify"
    assert "CONN_MAX_AGE" not in default, \
        f"no persistence without explicit config, got {default!r}"


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


@th.unit_test("conn defaults: native psycopg pool aliases are skipped")
def test_pooled_alias_skipped(opts):
    from mojo.db.config import apply_connection_defaults

    context = _context(DATABASE_CONN_MAX_AGE=300)
    context["DATABASES"]["default"]["OPTIONS"] = {"pool": True}
    apply_connection_defaults(context)
    default = context["DATABASES"]["default"]

    assert "CONN_MAX_AGE" not in default, \
        f"a pooled alias must be skipped (pooling rejects CONN_MAX_AGE), got {default!r}"
    assert "CONN_HEALTH_CHECKS" not in default, \
        f"a pooled alias must be skipped entirely, got {default!r}"


@th.unit_test("conn defaults: derived reader alias keeps its own connection age")
def test_reader_alias_keeps_reader_age(opts):
    from mojo.db.config import apply_connection_defaults, apply_reader_database

    context = _context(DATABASE_READER_HOST="reader.internal")
    context["MIDDLEWARE"] = ["project.middleware.Existing"]
    apply_reader_database(context)
    apply_connection_defaults(context)
    databases = context["DATABASES"]

    assert databases["reader"]["CONN_MAX_AGE"] == 60, \
        f"the reader's explicit 60s age must survive: {databases['reader']!r}"
    assert databases["reader"]["CONN_HEALTH_CHECKS"] is True, \
        f"the reader alias must still gain health checks: {databases['reader']!r}"
    assert "CONN_MAX_AGE" not in databases["default"], \
        f"the default alias must stay non-persistent: {databases['default']!r}"
