"""Pure settings-dict coverage for database reader injection."""

import copy

from testit import helpers as th


def _context(**overrides):
    context = {
        "DATABASES": {
            "default": {
                "ENGINE": "django.db.backends.postgresql",
                "HOST": "primary.internal",
                "PORT": "5432",
                "OPTIONS": {"sslmode": "require"},
            },
        },
        "MIDDLEWARE": ["project.middleware.Existing"],
    }
    context.update(overrides)
    return context


@th.unit_test("reader config: absent host is completely inert")
def test_absent_host_leaves_context_untouched(opts):
    from mojo.db.config import apply_reader_database

    context = _context()
    baseline = copy.deepcopy(context)
    apply_reader_database(context)
    assert context == baseline, \
        f"absent DATABASE_READER_HOST must not mutate settings, got {context!r}"


@th.unit_test("reader config: host derives an independent reader alias")
def test_host_derives_reader_alias(opts):
    from mojo.db.config import apply_reader_database

    context = _context(DATABASE_READER_HOST="reader.internal")
    apply_reader_database(context)
    reader = context["DATABASES"]["reader"]

    assert reader["HOST"] == "reader.internal", \
        f"the reader host override was not applied: {reader!r}"
    assert reader["CONN_MAX_AGE"] == 60, \
        f"the default reader connection age must be 60 seconds: {reader!r}"
    assert reader["TEST"]["MIRROR"] == "default", \
        f"reader tests must mirror the default database: {reader!r}"
    reader["OPTIONS"]["sslmode"] = "changed"
    assert context["DATABASES"]["default"]["OPTIONS"]["sslmode"] == "require", \
        "the derived reader must be a deep copy of the default alias"


@th.unit_test("reader config: port and connection-age overrides are honored")
def test_reader_overrides_are_honored(opts):
    from mojo.db.config import apply_reader_database

    context = _context(
        DATABASE_READER_HOST="reader.internal",
        DATABASE_READER_PORT=6432,
        DATABASE_READER_CONN_MAX_AGE=15,
    )
    apply_reader_database(context)
    reader = context["DATABASES"]["reader"]

    assert reader["PORT"] == 6432, \
        f"DATABASE_READER_PORT must replace the copied port, got {reader['PORT']!r}"
    assert reader["CONN_MAX_AGE"] == 15, \
        f"DATABASE_READER_CONN_MAX_AGE must be honored, got {reader['CONN_MAX_AGE']!r}"


@th.unit_test("reader config: malformed copied TEST config is replaced safely")
def test_malformed_test_config_is_replaced(opts):
    from mojo.db.config import apply_reader_database

    context = _context(DATABASE_READER_HOST="reader.internal")
    context["DATABASES"]["default"]["TEST"] = None
    apply_reader_database(context)

    test_config = context["DATABASES"]["reader"]["TEST"]
    assert test_config == {"MIRROR": "default"}, \
        f"a malformed copied TEST value must not break startup, got {test_config!r}"


@th.unit_test("reader config: existing routers retain priority")
def test_router_is_appended_once(opts):
    from mojo.db.config import apply_reader_database

    existing = "project.db.ExistingRouter"
    context = _context(
        DATABASE_READER_HOST="reader.internal",
        DATABASE_ROUTERS=[existing],
    )
    apply_reader_database(context)
    apply_reader_database(context)

    assert context["DATABASE_ROUTERS"] == [
        existing, "mojo.db.router.ReaderRouter",
    ], f"existing routers must retain priority and mojo must appear once: {context['DATABASE_ROUTERS']!r}"


@th.unit_test("reader config: routing middleware is outermost and unique")
def test_middleware_is_prepended_once(opts):
    from mojo.db.config import apply_reader_database

    context = _context(DATABASE_READER_HOST="reader.internal")
    apply_reader_database(context)
    apply_reader_database(context)

    expected = "mojo.middleware.db_reader.ReaderPinMiddleware"
    assert context["MIDDLEWARE"][0] == expected, \
        f"reader scoping middleware must be outermost, got {context['MIDDLEWARE']!r}"
    assert context["MIDDLEWARE"].count(expected) == 1, \
        f"reader scoping middleware must be injected exactly once: {context['MIDDLEWARE']!r}"


@th.unit_test("reader config: tuple settings are rebuilt as lists")
def test_tuple_router_and_middleware_settings_are_supported(opts):
    from mojo.db.config import apply_reader_database

    context = _context(
        DATABASE_READER_HOST="reader.internal",
        DATABASE_ROUTERS=("project.db.ExistingRouter",),
        MIDDLEWARE=("project.middleware.Existing",),
    )
    apply_reader_database(context)

    assert isinstance(context["DATABASE_ROUTERS"], list), \
        f"tuple DATABASE_ROUTERS must be rebuilt as a list, got {type(context['DATABASE_ROUTERS']).__name__}"
    assert isinstance(context["MIDDLEWARE"], list), \
        f"tuple MIDDLEWARE must be rebuilt as a list, got {type(context['MIDDLEWARE']).__name__}"


@th.unit_test("reader config: an explicit reader alias is preserved")
def test_explicit_reader_alias_wins(opts):
    from mojo.db.config import apply_reader_database

    explicit = {"ENGINE": "custom", "HOST": "handwritten.internal"}
    context = _context(
        DATABASE_READER_HOST="ignored.internal",
        DATABASES={"default": {"HOST": "primary.internal"}, "reader": explicit},
    )
    apply_reader_database(context)

    assert context["DATABASES"]["reader"] is explicit, \
        "a hand-written reader alias must not be replaced or copied"
    assert context["DATABASES"]["reader"]["HOST"] == "handwritten.internal", \
        f"the explicit reader host must win, got {context['DATABASES']['reader']!r}"


@th.unit_test("reader config: malformed databases skip safely with diagnostics")
def test_missing_or_malformed_databases_skip(opts):
    from mojo.db import config

    for databases in (None, [], {}, {"analytics": {}}, {"default": None}):
        context = {
            "DATABASE_READER_HOST": "reader.internal",
            "DATABASES": databases,
            "MIDDLEWARE": ["project.middleware.Existing"],
        }
        baseline = copy.deepcopy(context)
        config.apply_reader_database(context)
        assert context == baseline, \
            f"malformed DATABASES must be left untouched, got {context!r}"
        assert config.LAST_SKIP_REASON, \
            f"malformed DATABASES must leave a diagnostic reason for {databases!r}"


@th.unit_test("reader config: missing middleware skips the whole feature safely")
def test_missing_middleware_skips(opts):
    from mojo.db import config

    context = _context(DATABASE_READER_HOST="reader.internal")
    del context["MIDDLEWARE"]
    baseline = copy.deepcopy(context)
    config.apply_reader_database(context)

    assert context == baseline, \
        f"routing must stay inert without a request-scope middleware list, got {context!r}"
    assert "MIDDLEWARE" in config.LAST_SKIP_REASON, \
        f"the skip reason must name the missing MIDDLEWARE setting, got {config.LAST_SKIP_REASON!r}"
