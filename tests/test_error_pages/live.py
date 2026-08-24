"""
The same negotiation, end to end through the real server (maestro #2262).

negotiation.py proves the rule; this file proves it is actually wired into
the URLconf and the dispatcher a deployment runs — the unconfigured root, the
Django handler404, and the dispatcher's own unknown-endpoint 404.
"""

TESTIT_TIER = "extended"
from testit import helpers as th
from testit.helpers import assert_eq, assert_true


BROWSER = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"}
CURL = {"Accept": "*/*"}

# A path Django resolves but the dispatcher has no handler for: /s/<code> is
# registered GET-only, so a POST falls to the "Endpoint not found" branch in
# mojo/decorators/http.py rather than to Django's handler404.
DISPATCH_MISS = "/s/2262nope"

# The unresolved-path handler404 test needs DEBUG off (a server reload), so it
# lives in tests/test_error_pages_extended_serial/live_handler404.py
# (maestro #2791).


@th.django_unit_test("live: the unconfigured root serves the page to a browser, at 200")
def test_root_page_for_browser(opts):
    resp = opts.client.get("/", headers=BROWSER)

    assert_eq(resp.status_code, 200, "the unconfigured root is not an error — it must be 200")
    body = str(resp.text)
    assert_true("Nothing is published here yet" in body,
                f"root page must carry the approved headline, got: {body[:300]!r}")
    assert_true("Permission Denied" not in body,
                "the fresh-install root must never read as a denial (maestro #2162)")
    assert_true("admin" not in body.lower(), "the root page must not mention admin")


@th.django_unit_test("live: the unconfigured root answers a monitor with JSON")
def test_root_json_for_api_client(opts):
    resp = opts.client.get("/", headers=CURL)

    assert_eq(resp.status_code, 200, "a monitor must still get 200 from the root")
    assert_true(resp.response is not None and resp.response.code == 200,
                f"Accept: */* must get JSON, not a page, got: {opts.client.last_response.body!r}")


@th.django_unit_test("live: the dispatcher's unknown-endpoint 404 keeps its JSON for API clients")
def test_dispatcher_miss_json(opts):
    resp = opts.client.post(DISPATCH_MISS, headers=CURL)

    assert_eq(resp.status_code, 404, "an unhandled method must still be a 404")
    assert_eq(resp.response.error, "Endpoint not found",
              f"the JSON 404 envelope must be unchanged, got {opts.client.last_response.body!r}")


@th.django_unit_test("live: the dispatcher's unknown-endpoint 404 serves the page to a browser")
def test_dispatcher_miss_html(opts):
    resp = opts.client.post(DISPATCH_MISS, headers=BROWSER)
    body = str(resp.text)

    assert_eq(resp.status_code, 404, "the browser must get a true 404, not a 200")
    assert_true("That page doesn&rsquo;t exist" in body,
                f"the browser must get the styled 404 page, got: {body[:300]!r}")
    assert_true(DISPATCH_MISS not in body, "the 404 page must not echo the request path")


# test_project_handler404 moved to
# tests/test_error_pages_extended_serial/live_handler404.py (maestro #2791):
# it needs DEBUG off, which requires a server reload.
