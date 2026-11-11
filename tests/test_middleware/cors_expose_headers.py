"""
Regression test for #10669 — CORSMiddleware must expose Retry-After.

The rate-limit decorators answer 429 with a `Retry-After` header, but a
cross-origin browser script can only read response headers that are listed in
`Access-Control-Expose-Headers`. Without Retry-After in that list a browser
client (e.g. a kiosk) cannot see how long to wait before retrying.

The test calls the middleware in-process with a fake request and a 429
response, on a non-bouncer path so no settings are read.
"""
from testit import helpers as th
from objict import objict

TESTIT_TIER = "bug"


def _exposed(status, headers=None):
    """Run CORSMiddleware over a response with the given status and headers;
    return the exposed header names, lower-cased."""
    from django.http import HttpResponse
    from mojo.middleware.cors import CORSMiddleware

    def get_response(request):
        resp = HttpResponse(status=status)
        for name, value in (headers or {}).items():
            resp[name] = value
        return resp

    req = objict(method="GET", path="/api/version", META={"HTTP_ORIGIN": "https://kiosk.example"})
    resp = CORSMiddleware(get_response)(req)
    raw = resp.get("Access-Control-Expose-Headers", "")
    return [part.strip().lower() for part in raw.split(",") if part.strip()]


@th.django_unit_test("cors: a 429's Retry-After is exposed to cross-origin scripts (#10669)")
def test_retry_after_exposed(opts):
    exposed = _exposed(429, {"Retry-After": "30"})
    assert "retry-after" in exposed, \
        f"Retry-After must be in Access-Control-Expose-Headers so browsers can read a 429's delay, got {exposed}"


@th.django_unit_test("cors: previously exposed headers stay exposed (#10669)")
def test_existing_exposed_headers_kept(opts):
    exposed = _exposed(200)
    for name in ("content-disposition", "x-total-count", "x-bouncer-muid",
                 "x-bouncer-reason", "www-authenticate"):
        assert name in exposed, \
            f"{name} must stay in Access-Control-Expose-Headers, got {exposed}"
