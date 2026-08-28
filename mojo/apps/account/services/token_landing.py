"""
Shared machinery for the emailed-token confirmation landings (#3257).

Three flows send a person a link they are expected to click: email
verification (`ev:`), email change (`ec:`) and account deactivation (`dv:`).
Before this module, opening those URLs APPLIED the change — a mail scanner, a
link preview or a browser prefetch silently burned the single-use token and
changed the account with nobody present.

The seam this module draws:

  * the **GET** is presentation only. It reads the token out of
    ``request.DATA``, hands it straight back to the page as inert data, and
    never validates, consumes or even looks up anything. It cannot mutate
    email, session or activity, and it must never display account identity —
    nobody has proved they hold the token yet.
  * the **POST** — the button press — is the only thing that acts, and it
    keeps every contract it already had.

Routes are derived from the router's own mount prefix (``MOJO_PREFIX``, see
``mojo/urls.py``) rather than a hardcoded ``/api/``, so a deployment that
mounts the framework elsewhere gets working links automatically.
"""
from urllib.parse import urlencode

from django.http import HttpResponseRedirect
from django.shortcuts import render

from mojo.helpers import urls
from mojo.helpers.settings import settings


# Token prefix -> the landing page a link of that kind should open. These are
# the URLs the emails point at, and the targets of the /auth compatibility
# redirect. Relative to the framework mount prefix.
PREFIX_ROUTES = {
    "ev": "auth/verify/email/confirm",
    "ec": "auth/email/change/confirm",
    "dv": "account/deactivate/confirm",
}

# Token prefix -> the endpoint the landing's button POSTs to. Same path as the
# landing for `ec:` and `dv:` (a GET/POST pair). `ev:` differs deliberately:
# the landing GET keeps the historical `auth/verify/email/confirm` path, whose
# POST is the authenticated 6-digit-code handler, so the public verify-only
# token confirm lives on its own sibling route.
CONFIRM_ROUTES = {
    "ev": "auth/email/verify/confirm",
    "ec": "auth/email/change/confirm",
    "dv": "account/deactivate/confirm",
}


def api_prefix():
    """The router's mount prefix as a path segment ("/api", or "" when unset)."""
    prefix = str(settings.get_static("MOJO_PREFIX", "api") or "").strip("/")
    return f"/{prefix}" if prefix else ""


def landing_path(prefix):
    """Absolute path of the landing page for a token prefix, or "" if unmapped."""
    route = PREFIX_ROUTES.get(prefix)
    return f"{api_prefix()}/{route}" if route else ""


def confirm_path(prefix):
    """Absolute path of the confirm endpoint for a token prefix, or "" if unmapped."""
    route = CONFIRM_ROUTES.get(prefix)
    return f"{api_prefix()}/{route}" if route else ""


def token_prefix(token):
    """The `ev`/`ec`/`dv` prefix of a token, or "" when it carries none."""
    if not isinstance(token, str) or ":" not in token:
        return ""
    return token.split(":", 1)[0]


def read_token(request):
    """
    The caller-supplied token, or "" for anything that is not a non-empty str.

    `request.DATA` yields a **list** for a repeated `?token=a&token=b` and a
    **dict** for `?token.x=1`. Both are treated as absent: a landing that
    cannot name one token has nothing to offer a button.
    """
    token = request.DATA.get("token")
    if not isinstance(token, str) or not token:
        return ""
    return token


def landing_context(request, confirm_url, **extra):
    """
    Build the template context for a landing page.

    `landing_data` is the ONLY place the token appears — Django's
    `json_script` filter renders it into an `application/json` data block,
    which is neither executed nor interpolated into any raw-text element.

    `redirect_url` is scheme-guarded by `urls.safe_nav_url` before it reaches
    the template, exactly as the pages this replaces did: only http/https and
    scheme-less values survive, so a `javascript:`/`data:`/custom-app scheme
    becomes "" and the template's `{% if redirect_url %}` wrapper omits the
    anchor entirely rather than rendering it dead.
    """
    token = read_token(request)
    ctx = {
        "has_token": bool(token),
        "redirect_url": urls.safe_nav_url(request.DATA.get("redirect")),
        "landing_data": {"token": token, "confirm_url": confirm_url},
    }
    ctx.update(extra)
    return ctx


def render_landing(request, template, ctx):
    """
    Render a landing template and stamp the headers it actually owns.

    `Referrer-Policy: no-referrer` keeps the token out of the Referer header of
    anything the page later links to. There is deliberately NO `Cache-Control`
    stamp: `MojoMiddleware` overwrites that header on every response with
    `no-store, no-cache, must-revalidate, max-age=0` (see
    `mojo/middleware/mojo.py`), so a stamp here would be dead code — the same
    trap already documented in `mojo/apps/shortlink/rest/redirect.py`. No CSP
    header either: the framework CSP is scoped, by design, to the four pages
    that extend `auth_base.html` and carry a nonce.
    """
    response = render(request, f"account/{template}", ctx)
    response["Referrer-Policy"] = "no-referrer"
    # Same per-response stamp the admin portal and OAuth consent pages use:
    # a framed landing plus its token is a clickjacking primitive, and the
    # deactivation page is a one-click destructive action.
    response["X-Frame-Options"] = "DENY"
    return response


def landing_redirect(request):
    """
    A 302 to the landing this request's token belongs to, or None.

    The compatibility path for `/auth?flow=…&token=…` links: those are already
    in inboxes, and a deployment may still build them. It runs SERVER-side,
    ahead of the bouncer challenge page — the challenge stashes only `pr:`
    tokens, so a cold visitor's `ev:`/`ec:`/`dv:` token is destroyed before any
    client-side handler could see it.

    Routing keys on the token PREFIX, never on `flow=`: `flow` is read nowhere
    in the framework, uses a different vocabulary from the prefixes, and is
    attacker-supplied. Returns None — falling through unchanged — for `ml:`,
    `pr:`, `iv:`, an unmapped prefix, and a token-less request.

    No open-redirect surface: the destination is one of three fixed
    same-deployment paths, and the `redirect` passenger stays `safe_nav_url`
    sanitized.
    """
    token = read_token(request)
    prefix = token_prefix(token)
    path = landing_path(prefix)
    if not path:
        return None
    params = {"token": token}
    redirect_url = urls.safe_nav_url(request.DATA.get("redirect"))
    if redirect_url:
        params["redirect"] = redirect_url
    return HttpResponseRedirect(f"{path}?{urlencode(params)}")
