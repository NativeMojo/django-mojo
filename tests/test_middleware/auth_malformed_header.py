"""
Regression tests for ITEM-012 — AuthenticationMiddleware must not 500 on a
malformed `Authorization` header.

Before the fix, `prefix, token = token.split()` raised ValueError whenever the
header was not exactly two whitespace-separated parts (a bare scheme-less token,
an empty string, or 3+ parts), producing an unhandled HTTP 500.

The in-process tests call `process_request` directly (the crash site); the HTTP
test drives the real middleware chain through the test server.
"""
from testit import helpers as th
from objict import objict


def _run(auth_value):
    """Build a fake request carrying the given raw Authorization header and run it
    through AuthenticationMiddleware.process_request. Returns (result, request).

    Imports live inside the helper so the account models the middleware pulls in
    are only imported once Django is configured (testit convention)."""
    from mojo.middleware.auth import AuthenticationMiddleware
    req = objict(META={"HTTP_AUTHORIZATION": auth_value}, bearer=None)
    result = AuthenticationMiddleware(lambda request: None).process_request(req)
    return result, req


@th.django_unit_test("auth middleware: bare scheme-less token passes through, exposed as prefix 'raw'")
def test_bare_single_token(opts):
    result, req = _run("baretoken123")
    assert result is None, \
        "a bare scheme-less token must pass through (return None), not crash or reject"
    assert req.bearer is None, \
        "a bare token must NOT authenticate: request.bearer must stay None"
    assert getattr(req, "auth_token", None) is not None, \
        "a bare token must be exposed on request.auth_token for downstream validation"
    assert req.auth_token.prefix == "raw", \
        f"bare-token prefix must be 'raw', got {req.auth_token.prefix!r}"
    assert req.auth_token.token == "baretoken123", \
        f"bare-token value must be preserved intact, got {req.auth_token.token!r}"


@th.django_unit_test("auth middleware: empty Authorization header passes through without 500")
def test_empty_header(opts):
    result, req = _run("")
    assert result is None, \
        "an empty Authorization header must pass through (return None), not crash"
    assert req.bearer is None, \
        "an empty header must leave request.bearer None"
    assert getattr(req, "auth_token", None) is None, \
        "an empty header has no token to expose — request.auth_token must not be set"


@th.django_unit_test("auth middleware: 3+ part Authorization header passes through without 500")
def test_three_part_header(opts):
    result, req = _run("bearer tok extra")
    assert result is None, \
        "a 3+ part Authorization header must pass through (return None), not crash"
    assert req.bearer is None, \
        "a 3+ part header must leave request.bearer None"
    assert getattr(req, "auth_token", None) is None, \
        "a 3+ part header is malformed — request.auth_token must not be set"


@th.django_unit_test("auth middleware over HTTP: malformed header returns 200 on public endpoint, not 500")
def test_http_public_endpoint_no_500(opts):
    # AuthenticationMiddleware is installed in the test-project MIDDLEWARE, so this
    # GET actually flows through it. The headers= override wins over the client's
    # default Authorization header (RestClient merges it in last).
    resp = opts.client.get("/api/auth/config", headers={"Authorization": "baretoken123"})
    assert resp.status_code != 500, \
        f"a malformed Authorization header must not cause an HTTP 500, got {resp.status_code}"
    assert resp.status_code == 200, \
        f"the public auth/config endpoint must still succeed with a malformed header, got {resp.status_code}"
