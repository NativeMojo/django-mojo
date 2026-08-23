"""The project handler404, end to end through the real server (maestro #2262).

Django routes to handler404 only when DEBUG is off, so this test flips DEBUG on
the server with th.server_settings() — a reload. Moved to the serial sibling
(maestro #2791); the rest of the live error-page coverage stays in the parallel
test_error_pages package.
"""
from testit import helpers as th
from testit.helpers import assert_eq, assert_true


BROWSER = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"}
CURL = {"Accept": "*/*"}

# A path nothing resolves at all — Django's own handler404, which the project
# URLconf owns. Django only calls it when DEBUG is off, so the test flips DEBUG.
UNRESOLVED_PATH = "/api/2262/definitely-not-a-route"


@th.django_unit_test("live: the project's handler404 negotiates for a genuinely unresolved path")
def test_project_handler404(opts):
    # Django routes to handler404 only when DEBUG is off; with DEBUG on it
    # serves its own technical page and the project handler never runs.
    with th.server_settings(DEBUG=False):
        api = opts.client.get(UNRESOLVED_PATH, headers=CURL)
        api_body = opts.client.last_response.body
        page = opts.client.get(UNRESOLVED_PATH, headers=BROWSER)
        page_body = str(page.text)

    assert_eq(api.status_code, 404, "an unresolved path must still be a JSON 404 for API clients")
    assert_eq(api.response.error, "Not found",
              f"the project handler404 JSON envelope must be unchanged, got {api_body!r}")
    assert_eq(page.status_code, 404, "the browser must get a true 404 from handler404 too")
    assert_true("That page doesn&rsquo;t exist" in page_body,
                f"handler404 must serve the styled page to a browser, got: {page_body[:300]!r}")
    assert_true("Traceback" not in page_body and "URLconf" not in page_body,
                "the styled page must replace Django's technical 404, not sit beside it")
