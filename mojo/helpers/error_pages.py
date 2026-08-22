"""
Error pages — a styled HTML page for a browser, byte-identical JSON for
everything else.

Six states ship with the framework: 400, 403, 404, 500, 503, and the
unconfigured root (200). They live in ``mojo/templates/mojo/errors/`` and are
rendered through one seam so the negotiation rule is written down exactly
once.

The negotiation rule
--------------------
``prefers_html`` is deliberately strict. An API client that has been getting
JSON from this deployment for years must keep getting JSON, on the same
status code, with the same bytes — that is the load-bearing property of this
whole feature, not the pages. So:

1. No ``Accept`` header at all -> JSON.
2. Any JSON media type at q>0 -> JSON, decisively, even alongside text/html.
3. ``text/html`` at a quality **strictly greater** than the one offered for
   ``*/*`` -> HTML.
4. Anything else -> JSON.

``Accept: */*`` — curl, requests, most HTTP libraries, most monitors —
therefore stays on JSON. A wildcard is not a preference. A browser sends
``text/html,application/xhtml+xml,...,*/*;q=0.8``, where text/html at 1.0
beats the wildcard's 0.8, and gets the page.

Status codes
------------
``MOJO_APP_STATUS_200_ON_ERROR`` is an API-client compatibility shim. It
folds the JSON status to 200 and nothing else: the HTML branch always carries
the TRUE status, because a browser, a crawler and an uptime monitor all need
the real code. The two are independent axes — see ``error_response``.

Overriding a page in a project
------------------------------
Resolution order for state ``<name>``:

1. ``errors/<name>`` through Django's normal template loaders — drop
   ``templates/errors/404.html`` into any installed app and it wins.
2. ``mojo/errors/<name>`` through the normal loaders — a second override
   hook, and the path to use when a project also puts ``TEMPLATE_DIR`` on
   ``TEMPLATES[0]["DIRS"]`` and wants to ``{% extends %}`` the shipped shell.
3. The shipped page, loaded off disk through a private engine.

Step 3 is why a project that touches nothing gets the full set on upgrade:
the framework's own copy needs neither ``APP_DIRS`` nor an entry in
``INSTALLED_APPS`` nor a healthy app registry to render.
"""
import os

from django.http import HttpResponse
from django.template import Context, Engine, TemplateDoesNotExist, loader

from mojo.helpers import logit
from mojo.helpers.response import JsonResponse

logger = logit.get_logger("error", "error.log")


# The framework's own template root, shipped inside the package.
TEMPLATE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")

# The states that have a page. A status not listed here has no HTML rendering
# and always falls through to JSON — deliberately conservative: inventing a
# page for, say, 440 (step-up re-auth) would put the wrong words on screen.
PAGES = {
    400: "400.html",
    403: "403.html",
    404: "404.html",
    500: "500.html",
    503: "503.html",
}

ROOT_PAGE = "root.html"

_ENGINE = None


def _framework_engine():
    """A private filesystem-only engine over the shipped templates.

    Built lazily and cached. Independent of the project's TEMPLATES setting on
    purpose — the last-resort renderer must not depend on the configuration
    that may itself be what broke.
    """
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = Engine(dirs=[TEMPLATE_DIR], debug=False)
    return _ENGINE


def parse_accept(header):
    """Return [(media_type, quality)] parsed from an Accept header.

    Unparseable parts are dropped, and an unparseable ``q`` counts as 0 —
    garbage never argues its way into an HTML response.
    """
    parsed = []
    for part in (header or "").split(","):
        bits = part.split(";")
        media = bits[0].strip().lower()
        if not media:
            continue
        quality = 1.0
        for param in bits[1:]:
            key, _, value = param.partition("=")
            if key.strip().lower() != "q":
                continue
            try:
                quality = float(value.strip())
            except (TypeError, ValueError):
                quality = 0.0
        parsed.append((media, quality))
    return parsed


def _is_json_media(media):
    return media == "application/json" or media == "text/json" or media.endswith("+json")


def prefers_html(request):
    """True only when the caller asked for HTML specifically. See module docs."""
    header = ""
    meta = getattr(request, "META", None)
    if isinstance(meta, dict):
        header = meta.get("HTTP_ACCEPT") or ""
    if not header:
        return False

    html_quality = 0.0
    wildcard_quality = 0.0
    for media, quality in parse_accept(header):
        if quality <= 0:
            continue
        if _is_json_media(media):
            return False
        if media == "text/html" or media == "application/xhtml+xml":
            html_quality = max(html_quality, quality)
        elif media == "*/*":
            wildcard_quality = max(wildcard_quality, quality)

    return html_quality > wildcard_quality


def brand_name():
    """The wordmark, read from the same place the hosted auth pages read it:
    ``AUTH_CONFIG.theme.app_title`` (mojo.apps.account.services.auth_config).

    Never raises and never blocks on a broken dependency: an error page that
    dies reading configuration is worse than one with no wordmark.
    """
    try:
        from mojo.apps.account.services import auth_config
        cfg = auth_config.resolve_auth_config()
        return (cfg.theme.app_title or "").strip() or None
    except Exception:
        return None


def _render_html(name, context, select_template=None):
    """Render one page by template basename, honoring project overrides.

    select_template is a keyword test seam (item #2558) defaulting to
    Django's loader.select_template; production behavior is unchanged.
    """
    if select_template is None:
        select_template = loader.select_template
    try:
        template = select_template([f"errors/{name}", f"mojo/errors/{name}"])
        return template.render(context)
    except TemplateDoesNotExist:
        pass
    except Exception as err:
        # A broken TEMPLATES setting must not cost us the page.
        logger.error(f"error page: project loader failed for {name}: {err}")
    return _framework_engine().get_template(f"mojo/errors/{name}").render(Context(context))


def render_error_page(request, status, reference=None):
    """An HttpResponse carrying the page for `status`, at that TRUE status.

    Returns None when `status` has no page — the caller falls back to JSON.
    """
    name = PAGES.get(status)
    if name is None:
        return None
    html = _render_html(name, {"brand_name": brand_name(), "reference": reference})
    return HttpResponse(html, content_type="text/html; charset=utf-8", status=status)


def error_response(request, payload, json_status, page_status=None, reference=None):
    """The one seam. HTML page for an HTML-preferring caller, else `payload`.

    `payload` and `json_status` are exactly what the JSON caller received
    before this function existed — pass them through unchanged and the API
    contract is unchanged with them.

    `page_status` is the TRUE status. Pass it when `json_status` has been
    folded to 200 by MOJO_APP_STATUS_200_ON_ERROR; the HTML branch uses it
    regardless of that shim.

    `reference` is the incident id, rendered by the 500 page only.
    """
    page_status = json_status if page_status is None else page_status
    if prefers_html(request):
        try:
            response = render_error_page(request, page_status, reference=reference)
            if response is not None:
                return response
        except Exception as err:
            # Falling back to JSON is strictly better than a bare Django 500
            # on top of the error we were already reporting.
            logger.error(f"error page: render failed for {page_status}: {err}")
    return JsonResponse(payload, status=json_status)


def render_root_page(request):
    """The unconfigured-root response (HTTP 200) for a project with no site at "/"."""
    if prefers_html(request):
        try:
            html = _render_html(ROOT_PAGE, {"brand_name": brand_name(), "reference": None})
            return HttpResponse(html, content_type="text/html; charset=utf-8", status=200)
        except Exception as err:
            logger.error(f"error page: render failed for root: {err}")
    return JsonResponse(
        {"status": True, "code": 200, "message": "Nothing is published here yet"},
        status=200)
