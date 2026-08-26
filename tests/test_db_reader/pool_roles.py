"""Process-role and strict pool-candidate contracts."""

from testit import helpers as th


def _context(**overrides):
    context = {
        "DATABASES": {
            "default": {
                "ENGINE": "django.db.backends.postgresql",
                "NAME": "mojoland",
                "HOST": "writer.internal",
                "PORT": "5432",
                "OPTIONS": {"sslmode": "require"},
            },
        },
        "DATABASE_POOL_OPTIONS": {"min_size": 1, "max_size": 4, "timeout": 5},
        "DATABASE_POOL_ALIASES": ["default"],
        "DATABASE_POOL_API_WORKERS": 4,
        "DATABASE_POOL_NODE_COUNT": 2,
        "MIDDLEWARE": [
            "mojo.middleware.cors.CORSMiddleware",
            "mojo.middleware.auth.AuthenticationMiddleware",
            "mojo.middleware.logging.LoggerMiddleware",
        ],
        "DATABASE_POOL_IDENTITY": {
            "project": "mojoland",
            "node": "node-a",
            "application": "abc123",
            "deployment": "lab-1",
        },
    }
    context.update(overrides)
    return context


def _expect_invalid(context, message):
    from django.core.exceptions import ImproperlyConfigured
    from mojo.db.config import apply_connection_defaults

    try:
        apply_connection_defaults(
            context, {"MOJO_PROCESS_ROLE": "api", "MOJO_PROCESS_LAUNCHER": "asgi"})
    except ImproperlyConfigured:
        return
    raise AssertionError(message)


@th.unit_test("pool roles: only exact api/asgi injects default pool")
def test_only_exact_api_asgi_injects(opts):
    from mojo.db.config import apply_connection_defaults

    context = _context()
    apply_connection_defaults(
        context, {"MOJO_PROCESS_ROLE": "api", "MOJO_PROCESS_LAUNCHER": "asgi"})
    default = context["DATABASES"]["default"]
    assert default["OPTIONS"]["pool"] == {"min_size": 1, "max_size": 4, "timeout": 5}, \
        f"the proven API launcher must receive the strict pool, got {default!r}"
    assert default["ENGINE"] == "mojo.db.backends.postgresql", \
        f"the enabled alias must use the observed Django backend, got {default!r}"
    assert default["OPTIONS"]["application_name"].startswith("mojo|mojoland|node-a|api|default|"), \
        f"the pooled session must carry bounded operator identity, got {default!r}"
    assert default["CONN_MAX_AGE"] == 0, \
        f"the pooled alias must force CONN_MAX_AGE=0, got {default!r}"
    boundary = "mojo.middleware.database_pool.DatabasePoolErrorMiddleware"
    assert context["MIDDLEWARE"].count(boundary) == 1, \
        f"pool activation must inject exactly one outer HTTP boundary, got {context['MIDDLEWARE']!r}"
    assert context["MIDDLEWARE"].index(boundary) < context["MIDDLEWARE"].index(
        "mojo.middleware.auth.AuthenticationMiddleware"), \
        "the pool boundary must catch authentication-time acquisitions"

    for role, launcher in (
        ("", ""), ("api", ""), ("", "asgi"), ("jobs", "jobman"),
        ("cron", "cron"), ("management", "manage"), ("test", "testit"),
        ("preflight", "preflight"), ("api", "jobman"), ("jobs", "asgi"),
    ):
        context = _context()
        apply_connection_defaults(
            context, {"MOJO_PROCESS_ROLE": role, "MOJO_PROCESS_LAUNCHER": launcher})
        default = context["DATABASES"]["default"]
        assert "pool" not in default["OPTIONS"], (
            f"role={role!r} launcher={launcher!r} must retain an ordinary connection: {default!r}"
        )


@th.unit_test("pool roles: disabled mode needs no topology and stays ordinary")
def test_disabled_mode_needs_no_counts(opts):
    from mojo.db import config

    context = {
        "DATABASES": {
            "default": {"ENGINE": "django.db.backends.postgresql", "HOST": "writer.internal"},
        },
        "DATABASE_POOL_OPTIONS": False,
    }
    config.apply_connection_defaults(
        context, {"MOJO_PROCESS_ROLE": "api", "MOJO_PROCESS_LAUNCHER": "asgi"})
    default = context["DATABASES"]["default"]
    assert "pool" not in default.get("OPTIONS", {}), \
        f"disabled config must never create a pool, got {default!r}"
    assert config.LAST_POOL_PLAN["enabled"] is False, \
        f"disabled config must capture a disabled plan, got {config.LAST_POOL_PLAN!r}"


@th.unit_test("pool roles: candidate snapshot survives suppression and is immutable")
def test_candidate_snapshot_survives_suppression(opts):
    from mojo.db import config

    context = _context()
    config.apply_connection_defaults(
        context, {"MOJO_PROCESS_ROLE": "preflight", "MOJO_PROCESS_LAUNCHER": "preflight"})
    plan = config.LAST_POOL_PLAN
    assert plan["enabled"] is True and plan["valid"] is True, \
        f"preflight must retain the valid candidate before suppression, got {plan!r}"
    assert plan["options"]["max_size"] == 4, \
        f"the immutable plan must retain nonsecret sizing, got {plan!r}"
    assert "pool" not in context["DATABASES"]["default"]["OPTIONS"], \
        f"the preflight process itself must remain ordinary, got {context!r}"
    try:
        plan["options"]["max_size"] = 99
    except TypeError:
        pass
    else:
        raise AssertionError("LAST_POOL_PLAN must reject nested mutation")


@th.unit_test("pool roles: malformed or ambiguous candidates fail API startup")
def test_invalid_candidates_fail_closed(opts):
    invalid = []
    invalid.append(_context(DATABASE_POOL_OPTIONS=True))
    invalid.append(_context(DATABASE_POOL_OPTIONS={"min_size": 1, "max_size": 4}))
    invalid.append(_context(DATABASE_POOL_OPTIONS={"min_size": True, "max_size": 4, "timeout": 5}))
    invalid.append(_context(DATABASE_POOL_OPTIONS={"min_size": 5, "max_size": 4, "timeout": 5}))
    invalid.append(_context(DATABASE_POOL_OPTIONS={"min_size": 1, "max_size": 4, "timeout": 0}))
    invalid.append(_context(DATABASE_POOL_ALIASES=["default", "reader"]))
    invalid.append(_context(DATABASE_POOL_API_WORKERS=True))
    invalid.append(_context(DATABASE_POOL_NODE_COUNT=0))
    invalid.append(_context(MIDDLEWARE=None))
    invalid.append(_context(DATABASE_POOL_IDENTITY=None))
    invalid.append(_context(DATABASE_POOL_IDENTITY={
        "project": "mojoland", "node": "node a", "application": "abc", "deployment": "lab"}))
    invalid.append(_context(DATABASE_POOL_OPTIONS={
        "min_size": 1, "max_size": 4, "timeout": 5, "max_waiting": 0}))
    nonzero_age = _context()
    nonzero_age["DATABASES"]["default"]["CONN_MAX_AGE"] = 30
    invalid.append(nonzero_age)
    invalid.append(_context(DATABASE_CONN_MAX_AGE=30))
    non_postgres = _context()
    non_postgres["DATABASES"]["default"]["ENGINE"] = "django.db.backends.mysql"
    invalid.append(non_postgres)
    embedded = _context()
    embedded["DATABASES"]["reader"] = {
        "ENGINE": "django.db.backends.postgresql",
        "OPTIONS": {"pool": {"min_size": 1, "max_size": 1, "timeout": 1}},
    }
    invalid.append(embedded)

    for index, context in enumerate(invalid):
        _expect_invalid(context, f"invalid pool candidate {index} unexpectedly started the API")


@th.unit_test("pool roles: invalid non-api candidates are suppressed for recovery")
def test_invalid_candidate_is_suppressed_outside_api(opts):
    from mojo.db import config

    context = _context(DATABASE_POOL_OPTIONS=True)
    config.apply_connection_defaults(
        context, {"MOJO_PROCESS_ROLE": "jobs", "MOJO_PROCESS_LAUNCHER": "jobman"})
    assert "pool" not in context["DATABASES"]["default"]["OPTIONS"], \
        f"jobs must stay ordinary even when inherited config is invalid, got {context!r}"
    assert config.LAST_POOL_PLAN["valid"] is False, \
        f"the suppressed candidate must remain visible to diagnostics, got {config.LAST_POOL_PLAN!r}"
    assert len(config.LAST_POOL_DIAGNOSTIC) <= 240, \
        f"the nonsecret diagnostic must stay bounded, got {config.LAST_POOL_DIAGNOSTIC!r}"
