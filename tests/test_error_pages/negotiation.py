"""
Content negotiation for the framework error pages (maestro #2262).

The load-bearing property is NOT the pages — it is that every caller which is
not a browser keeps receiving byte-for-byte the JSON it received before these
pages existed. Two tests here compare the negotiated 404 and 403 bodies
against a JsonResponse built from the same payload, in the same process, so
"byte-for-byte" means exactly that and not "same fields".

Everything in this file runs in-process against `dispatch_error_handler`
directly. That is deliberate: it is the only way to control the Accept header,
the MOJO_APP_STATUS_200_ON_ERROR shim and the incident id in one place, and to
assert what the 500 page does NOT contain. Wire-level coverage of the same
seams is in live.py.
"""
from unittest.mock import patch

from objict import objict
from testit import helpers as th


# Accept headers, and whether each one is a request for a page.
# `*/*` is the one that matters most: curl, requests, and most monitors send
# it, and every one of them must keep getting JSON.
ACCEPT_CASES = [
    ("text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8", True, "a real browser"),
    ("text/html", True, "text/html alone"),
    ("text/html;q=0.9,*/*;q=0.8", True, "text/html outranking the wildcard"),
    ("*/*", False, "curl and most HTTP libraries"),
    ("", False, "no Accept header at all"),
    ("application/json", False, "an API client naming JSON"),
    ("application/json, text/plain, */*", False, "the axios/testit default"),
    ("text/html,application/json", False, "JSON named alongside HTML is decisive for JSON"),
    ("application/vnd.mojo+json", False, "a vendor JSON media type"),
    ("text/html;q=0,*/*", False, "text/html explicitly refused at q=0"),
    ("text/html,*/*", False, "no preference between HTML and the wildcard"),
    ("application/xml", False, "a non-HTML, non-JSON type"),
]

BROWSER = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}


def _request(path="/api/thing", accept=None, method="GET"):
    """A request carrying only the surface the dispatcher and the incident
    reporter actually touch. Built here rather than through the test client
    because these tests need to set Accept per call and read the returned
    HttpResponse object, not a parsed body."""
    from django.test import RequestFactory
    from django.contrib.auth.models import AnonymousUser

    extra = {}
    if accept is not None:
        extra["HTTP_ACCEPT"] = accept
    request = RequestFactory().generic(method, path, **extra)
    request.user = AnonymousUser()
    request.ip = "127.0.0.1"
    request.DATA = objict()
    request.group = None
    request.bearer = None
    return request


def _raising_view(exc):
    def view(request):
        raise exc
    return view


def _dispatch(exc, request):
    from mojo.decorators.http import dispatch_error_handler
    return dispatch_error_handler(_raising_view(exc))(request)


def _body(response):
    return response.content.decode("utf-8")


def _reset_template_caches():
    """Drop any cached template lookups so a file created mid-test is seen.

    Django wraps its loaders in cached.Loader whenever debug is off, and that
    cache remembers misses as well as hits. Without this, the override test
    would pass or fail depending on which settings profile the suite ran under.
    """
    from django.template import engines
    for backend in engines.all():
        engine = getattr(backend, "engine", None)
        for loader in getattr(engine, "template_loaders", None) or []:
            if hasattr(loader, "reset"):
                loader.reset()


@th.django_unit_setup()
def setup_error_pages(opts):
    import os
    from mojo.helpers import error_pages

    # The per-project override fixture. Written into an installed app's
    # templates dir so the test exercises Django's REAL resolution order
    # (errors/404.html ahead of the framework's mojo/errors/404.html) rather
    # than a rearranged settings dict. Removed here before it is created, per
    # the long-lived-database rule, and again in the test's finally block.
    opts.override_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(error_pages.__file__))),
        "apps", "account", "templates", "errors")
    opts.override_path = os.path.join(opts.override_dir, "404.html")
    if os.path.exists(opts.override_path):
        os.remove(opts.override_path)
    _reset_template_caches()


# ---------------------------------------------------------------------------
# The rule itself
# ---------------------------------------------------------------------------

@th.django_unit_test("negotiation: only an explicit text/html preference asks for a page")
def test_prefers_html_matrix(opts):
    from mojo.helpers import error_pages

    for accept, expected, description in ACCEPT_CASES:
        got = error_pages.prefers_html(_request(accept=accept))
        assert got is expected, \
            f"Accept {accept!r} ({description}) should prefer {'HTML' if expected else 'JSON'}, got {'HTML' if got else 'JSON'}"


@th.django_unit_test("negotiation: a request with no Accept header at all gets JSON")
def test_missing_accept_header_is_json(opts):
    from mojo.helpers import error_pages

    assert error_pages.prefers_html(_request(accept=None)) is False, \
        "a request that sends no Accept header must not be served an HTML page"


# ---------------------------------------------------------------------------
# The API contract — unchanged bytes
# ---------------------------------------------------------------------------

@th.tier("core")
@th.django_unit_test("JSON: the unknown-endpoint 404 body is byte-for-byte what it was")
def test_json_404_bytes_unchanged(opts):
    from mojo.helpers import error_pages
    from mojo.helpers.response import JsonResponse

    payload = {"error": "Endpoint not found", "code": 404}
    expected = JsonResponse(dict(payload), status=404)
    got = error_pages.error_response(_request(accept="*/*"), dict(payload), 404)

    assert got.content == expected.content, \
        f"404 JSON body changed: {got.content!r} != {expected.content!r}"
    assert got.status_code == 404, f"404 JSON status changed, got {got.status_code}"
    assert got["Content-Type"] == expected["Content-Type"], \
        f"404 JSON content type changed, got {got['Content-Type']!r}"


@th.tier("core")
@th.django_unit_test("JSON: the 403 body is byte-for-byte what it was")
def test_json_403_bytes_unchanged(opts):
    import mojo.errors
    from mojo.helpers.response import JsonResponse

    err = mojo.errors.PermissionDeniedException("Permission Denied", 403, 403)
    payload = {"error": err.reason, "code": err.code, "status": False}
    expected = JsonResponse(dict(payload), status=403)
    got = _dispatch(err, _request(accept="*/*"))

    assert got.content == expected.content, \
        f"403 JSON body changed: {got.content!r} != {expected.content!r}"
    assert got.status_code == 403, f"403 JSON status changed, got {got.status_code}"


@th.tier("core")
@th.django_unit_test("JSON: a 500 still carries its error envelope for API callers")
def test_json_500_envelope_unchanged(opts):
    got = _dispatch(RuntimeError("kaboom-2262-json"), _request(accept="application/json"))

    assert got.status_code == 500, f"API caller must still get 500, got {got.status_code}"
    assert "application/json" in got["Content-Type"], \
        f"API caller must still get JSON, got {got['Content-Type']!r}"
    assert '"code": 500' in _body(got) or '"code":500' in _body(got), \
        f"the JSON envelope must still carry code 500, got {_body(got)!r}"


# ---------------------------------------------------------------------------
# The pages
# ---------------------------------------------------------------------------

@th.django_unit_test("HTML: a browser gets the 404 page at a true 404")
def test_html_404_page(opts):
    from mojo.helpers import error_pages

    resp = error_pages.error_response(
        _request(accept=BROWSER["Accept"]), {"error": "Endpoint not found", "code": 404}, 404)
    body = _body(resp)

    assert resp.status_code == 404, f"the HTML 404 must carry status 404, got {resp.status_code}"
    assert "text/html" in resp["Content-Type"], \
        f"a browser must get text/html, got {resp['Content-Type']!r}"
    assert "That page doesn&rsquo;t exist" in body, \
        "the 404 page must carry the approved headline"
    assert 'href="/"' in body, "the 404 page must offer a link back to the root"


@th.django_unit_test("HTML: a browser gets the 403 page, and it names nothing")
def test_html_403_page(opts):
    import mojo.errors

    resp = _dispatch(
        mojo.errors.PermissionDeniedException("Permission Denied", 403, 403),
        _request(path="/api/account/group/91919", accept=BROWSER["Accept"]))
    body = _body(resp)

    assert resp.status_code == 403, f"the HTML 403 must carry status 403, got {resp.status_code}"
    assert "You don&rsquo;t have access to this" in body, \
        "the 403 page must carry the approved headline"
    # A 403 that says more than a 404 is free reconnaissance: it must not
    # confirm that anything exists at the address, nor name the address.
    assert "/api/account/group/91919" not in body, \
        "the 403 page must not echo the request path"
    assert "91919" not in body, \
        "the 403 page must not echo any identifier from the request"
    assert "Permission Denied" not in body, \
        "the 403 page must not echo the internal denial reason"


@th.django_unit_test("HTML: pages never mention or link the admin portal")
def test_html_pages_never_mention_admin(opts):
    from mojo.helpers import error_pages

    for status in sorted(error_pages.PAGES):
        body = _body(error_pages.render_error_page(_request(), status, reference=4242))
        assert "/admin" not in body, f"the {status} page must not link /admin/"
        assert "admin" not in body.lower(), f"the {status} page must not mention admin at all"

    root = _body(error_pages.render_root_page(_request(accept=BROWSER["Accept"])))
    assert "admin" not in root.lower(), "the unconfigured-root page must not mention admin"


@th.django_unit_test("HTML: every shipped page renders light/dark and is self-contained")
def test_pages_are_self_contained(opts):
    from mojo.helpers import error_pages

    names = list(error_pages.PAGES.values()) + [error_pages.ROOT_PAGE]
    for name in names:
        body = error_pages._render_html(name, {"brand_name": "TEST BRAND", "reference": None})
        assert "prefers-color-scheme: dark" in body, \
            f"{name} must follow the visitor's system light/dark preference"
        assert "<link" not in body, \
            f"{name} must not link an external stylesheet — it has to render when things are broken"
        assert "<script" not in body, f"{name} must not load a script"
        assert "TEST BRAND" in body, f"{name} must render the wordmark it was given"


@th.django_unit_test("HTML: an unmapped status has no page and falls through to JSON")
def test_unmapped_status_falls_through_to_json(opts):
    import mojo.errors

    # 440 (step-up re-auth) deliberately has no page: inventing one would put
    # the wrong words in front of the user.
    resp = _dispatch(mojo.errors.ReauthRequiredException(),
                     _request(accept=BROWSER["Accept"]))

    assert resp.status_code == 440, f"the true status must survive, got {resp.status_code}"
    assert "application/json" in resp["Content-Type"], \
        f"a status with no page must fall through to JSON, got {resp['Content-Type']!r}"


# ---------------------------------------------------------------------------
# The 200-on-error shim is an independent axis
# ---------------------------------------------------------------------------

@th.django_unit_test("MOJO_APP_STATUS_200_ON_ERROR: API callers get 200, browsers get the true code")
def test_status_200_shim_does_not_reach_html(opts):
    import mojo.errors

    err = mojo.errors.PermissionDeniedException("Permission Denied", 403, 403)
    with patch("mojo.decorators.http._status_200_on_error", return_value=True):
        api = _dispatch(err, _request(accept="*/*"))
        page = _dispatch(err, _request(accept=BROWSER["Accept"]))

    assert api.status_code == 200, \
        f"with the shim on, an API caller must still get HTTP 200, got {api.status_code}"
    assert '"code": 403' in _body(api) or '"code":403' in _body(api), \
        f"the shim must keep the real code in the JSON body, got {_body(api)!r}"
    assert page.status_code == 403, \
        f"the shim is an API-client knob — the HTML page must carry the true 403, got {page.status_code}"
    assert "text/html" in page["Content-Type"], \
        "the browser must still get the page when the shim is on"


# ---------------------------------------------------------------------------
# The 500 page: the reference, and nothing else
# ---------------------------------------------------------------------------

@th.django_unit_test("500: the page carries the REAL incident id and no diagnostics at all")
def test_500_page_shows_only_the_reference(opts):
    from mojo.apps.incident.models import Event

    secret = "kaboom-2262-do-not-render-me"
    path = "/api/thing/9182736"
    before = Event.objects.order_by("-id").values_list("id", flat=True).first() or 0

    resp = _dispatch(RuntimeError(secret), _request(path=path, accept=BROWSER["Accept"]))
    body = _body(resp)

    event = Event.objects.filter(
        id__gt=before,
        category="rest_error",
        details=f"Rest Exception: {secret}",
        metadata__http_path=path,
    ).get()
    assert event is not None, "the 500 must still file an incident"
    assert resp.status_code == 500, f"the HTML 500 must carry status 500, got {resp.status_code}"
    assert "Something went wrong on our end" in body, \
        "the 500 page must carry the approved headline"
    assert f"REF &middot; {event.id}" in body, \
        f"the 500 page must show the real incident id {event.id}, got: {body[-400:]!r}"

    # Everything below already lives on the incident record, which is
    # access-controlled. None of it may reach the page.
    assert secret not in body, "the 500 page must not render the exception text"
    assert "RuntimeError" not in body, "the 500 page must not render the exception type"
    assert "Traceback" not in body, "the 500 page must not render a traceback"
    assert "mojo/decorators" not in body, "the 500 page must not render a stack frame"
    assert path not in body, "the 500 page must not render the request path"
    assert "9182736" not in body, "the 500 page must not render identifiers from the request"


@th.django_unit_test("500: no incident, no reference — the page never invents one")
def test_500_page_without_incident_has_no_reference(opts):
    with patch("mojo.decorators.http._events_on_errors", return_value=False):
        resp = _dispatch(RuntimeError("kaboom-2262-no-incident"),
                         _request(accept=BROWSER["Accept"]))
    body = _body(resp)

    assert resp.status_code == 500, f"the page must still be a 500, got {resp.status_code}"
    assert "Something went wrong on our end" in body, \
        "the page must still render when incident reporting is off"
    assert "REF" not in body, \
        "with no incident filed there is no reference — the page must show none"


# ---------------------------------------------------------------------------
# Per-project override
# ---------------------------------------------------------------------------

@th.django_unit_test("override: a project's errors/404.html wins over the framework's page")
def test_project_override_wins(opts):
    import os
    from mojo.helpers import error_pages

    marker = "OVERRIDE-2262-THIS-IS-THE-PROJECT-PAGE"
    os.makedirs(opts.override_dir, exist_ok=True)
    try:
        with open(opts.override_path, "w") as handle:
            handle.write(f"<!DOCTYPE html><html><body>{marker}</body></html>")
        _reset_template_caches()

        body = _body(error_pages.render_error_page(_request(), 404))
        assert marker in body, \
            "a template at errors/404.html must win over the framework's mojo/errors/404.html"
        assert "That page doesn&rsquo;t exist" not in body, \
            "the framework page must not also render once a project has overridden it"
    finally:
        if os.path.exists(opts.override_path):
            os.remove(opts.override_path)
        if os.path.isdir(opts.override_dir) and not os.listdir(opts.override_dir):
            os.rmdir(opts.override_dir)
        _reset_template_caches()

    restored = _body(error_pages.render_error_page(_request(), 404))
    assert "That page doesn&rsquo;t exist" in restored, \
        "removing the override must restore the framework page"


@th.django_unit_test("override: the shipped page renders with no project template config at all")
def test_framework_fallback_renders_without_loaders(opts):
    from django.template import TemplateDoesNotExist
    from mojo.helpers import error_pages

    # What a project that has not configured TEMPLATES (or has broken it) gets:
    # the private filesystem engine over the shipped templates. The failing
    # loader is injected through _render_html's select_template seam
    # (item #2558) instead of patching the shared error_pages module.
    def no_loader(names):
        raise TemplateDoesNotExist("errors/404.html")

    body = error_pages._render_html(
        "404.html", {"brand_name": None, "reference": None},
        select_template=no_loader)

    assert "That page doesn&rsquo;t exist" in body, \
        "the shipped page must render straight off disk when no loader can find it"
