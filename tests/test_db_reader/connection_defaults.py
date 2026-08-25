"""Pure settings-dict coverage for connection-reuse defaults."""

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


@th.unit_test("conn defaults: server-backed alias gets persistent connections")
def test_defaults_applied_to_server_alias(opts):
    from mojo.db.config import apply_connection_defaults

    context = _context()
    apply_connection_defaults(context)
    default = context["DATABASES"]["default"]

    assert default["CONN_MAX_AGE"] == 300, \
        f"a server-backed alias must default to CONN_MAX_AGE=300, got {default!r}"
    assert default["CONN_HEALTH_CHECKS"] is True, \
        f"a server-backed alias must default to health checks on, got {default!r}"


@th.unit_test("conn defaults: legacy psycopg2 engine alias qualifies")
def test_legacy_engine_name_qualifies(opts):
    from mojo.db.config import apply_connection_defaults

    context = _context()
    context["DATABASES"]["default"]["ENGINE"] = (
        "django.db.backends.postgresql_psycopg2")
    apply_connection_defaults(context)

    assert context["DATABASES"]["default"]["CONN_MAX_AGE"] == 300, \
        "the postgresql_psycopg2 legacy engine name must receive the default"


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


@th.unit_test("conn defaults: project-level override keys are honored")
def test_project_override_keys(opts):
    from mojo.db.config import apply_connection_defaults

    context = _context(
        DATABASE_CONN_MAX_AGE=0,
        DATABASE_CONN_HEALTH_CHECKS=False,
    )
    apply_connection_defaults(context)
    default = context["DATABASES"]["default"]

    assert default["CONN_MAX_AGE"] == 0, \
        f"DATABASE_CONN_MAX_AGE=0 must restore per-request connections, got {default!r}"
    assert default["CONN_HEALTH_CHECKS"] is False, \
        f"DATABASE_CONN_HEALTH_CHECKS must be honored, got {default!r}"


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


@th.unit_test("conn defaults: native psycopg pool aliases are skipped")
def test_pooled_alias_skipped(opts):
    from mojo.db.config import apply_connection_defaults

    context = _context()
    context["DATABASES"]["default"]["OPTIONS"] = {"pool": True}
    apply_connection_defaults(context)
    default = context["DATABASES"]["default"]

    assert "CONN_MAX_AGE" not in default, \
        f"a pooled alias must be skipped (pooling rejects CONN_MAX_AGE), got {default!r}"


@th.unit_test("conn defaults: derived reader alias keeps its own connection age")
def test_reader_alias_keeps_reader_age(opts):
    from mojo.db.config import apply_connection_defaults, apply_reader_database

    context = _context(DATABASE_READER_HOST="reader.internal")
    context["MIDDLEWARE"] = ["project.middleware.Existing"]
    apply_reader_database(context)
    apply_connection_defaults(context)
    databases = context["DATABASES"]

    assert databases["reader"]["CONN_MAX_AGE"] == 60, \
        f"the reader's explicit 60s age must win over the general default: {databases['reader']!r}"
    assert databases["reader"]["CONN_HEALTH_CHECKS"] is True, \
        f"the reader alias must still gain health checks: {databases['reader']!r}"
    assert databases["default"]["CONN_MAX_AGE"] == 300, \
        f"the default alias must get the general default: {databases['default']!r}"
