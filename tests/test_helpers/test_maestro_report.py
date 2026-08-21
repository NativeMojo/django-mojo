"""Tests for the maestro test-run reporter (testit/maestro.py) — the
parallel-safe remainder.

The bulk of this module (enable-gate, discovery, payload, and push tests) moved
to tests/test_helpers_extended_serial/test_maestro_report.py: those tests write
MAESTRO_*/CI environment variables and/or patch testit.maestro module
attributes, process-global mutations that are unsafe under the parallel default
tier (maestro item #1839). What stays touches no shared state beyond a
save/restore of testit's own run flags.
"""
from testit import helpers as th


def _opts(**kwargs):
    from objict import objict
    base = dict(maestro=False, no_maestro=False, config_data={})
    base.update(kwargs)
    return objict(base)


@th.unit_test("maestro auth: a JWT credential authenticates as Bearer, not apikey")
def test_auth_scheme_by_shape(opts):
    """mojo routes on the scheme — `apikey` goes to ApiKey.validate_token and
    `Bearer` to User.validate_jwt — so the wrong one is a flat 401. maestro
    issues its long-lived `user_api_key` AS a JWT, which is the trap: the thing
    everyone calls "the api key" does not authenticate under `apikey`.

    This is not hypothetical. The first working discovery still failed to push,
    with HTTP 401 'Invalid API key', for exactly this reason.
    """
    from testit import maestro

    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJ1aWQiOjF9.c2ln"
    assert maestro.auth_scheme(jwt) == "Bearer", \
        "a JWT must authenticate as Bearer"
    assert maestro.auth_scheme("plain-opaque-token") == "apikey", \
        "an opaque token must authenticate as apikey"
    assert maestro.auth_scheme(jwt, "apikey") == "apikey", \
        "a scheme declared by the carrier must win over the shape guess"
    assert maestro.auth_scheme("a.b") == "apikey" and maestro.auth_scheme("a.b.c") == "apikey", \
        "dots alone are not a JWT — the base64url header prefix is required too"


# ---------------------------------------------------------------------------
# Partial runs
# ---------------------------------------------------------------------------




