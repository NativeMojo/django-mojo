"""
Helpers for resolving the URL an emailed token link should point at.

Two destinations live here, and which one a flow gets is the whole point:

* **The frontend webapp** — `password_reset`, `magic_login`, `invite`. Their
  consumers are pages the deployment's own SPA implements, so the link must
  reach the frontend origin. Multi-tenant deployments have several frontends
  and HTTP_ORIGIN reflects the admin portal making the request rather than the
  tenant's webapp, hence the lookup chain in `get_webapp_base_url`.

* **The framework's own confirmation landing** — `email_verify`,
  `email_change`, `account_deactivate` (#3257). Those three pages are served by
  django-mojo itself, on the API origin, so `WEBAPP_BASE_URL` is the wrong
  base: it is the FRONTEND origin, and on any deployment whose frontend is a
  separate SPA the landing does not exist there. A custom `WEBAPP_AUTH_PATH`
  could even land the link on the bouncer decoy page. These links therefore
  target `BASE_URL` plus the landing route.

A deployment that wants its own SPA to own those three confirmations overrides
`WEBAPP_BASE_URL`/`WEBAPP_AUTH_PATH` as before and forwards the token to the
API — see docs/web_developer/account/email_verification.md.
"""
from urllib.parse import quote

from mojo.apps.account.services import token_landing
from mojo.helpers.settings import settings


# Flow name -> token prefix, for the flows whose link opens a framework-served
# confirmation landing instead of the frontend auth page.
LANDING_FLOW_PREFIXES = {
    "email_verify": "ev",
    "email_change": "ec",
    "account_deactivate": "dv",
}


def get_webapp_base_url(request=None, user=None, group=None):
    """
    Resolve the frontend webapp base URL.

    Lookup order (first non-empty value wins):
    1. request.DATA["webapp_base_url"]            — explicit per-request override
    2. group.get_metadata_value(...)              — tenant group config (traverses parents)
    3. user.org.get_metadata_value(...)           — user's primary org
    4. settings.WEBAPP_BASE_URL                  — project-wide default
    5. user.metadata["protected"]["orig_webapp_url"] — recorded at first login
    6. request HTTP_ORIGIN                        — last-resort request context
    7. settings.BASE_URL                          — final fallback
    """
    if request is not None:
        val = request.DATA.get("webapp_base_url")
        if val:
            return val.rstrip("/")
    if group is not None:
        val = group.get_metadata_value("webapp_base_url")
        if val:
            return val.rstrip("/")
    if user is not None:
        org = getattr(user, "org", None)
        if org is not None:
            val = org.get_metadata_value("webapp_base_url")
            if val:
                return val.rstrip("/")
    val = settings.get("WEBAPP_BASE_URL") or ""
    if val:
        return val.rstrip("/")
    if user is not None:
        val = user.get_protected_metadata("orig_webapp_url") or ""
        if val:
            return val.rstrip("/")
    if request is not None:
        val = request.META.get("HTTP_ORIGIN") or ""
        if val:
            return val.rstrip("/")
    return settings.get("BASE_URL", "/").rstrip("/")


def get_webapp_auth_path(group=None):
    """
    Resolve the frontend auth path (e.g. "/auth" or "/login").

    Lookup order:
    1. group.get_metadata_value("webapp_auth_path")  — per-tenant override
    2. settings.WEBAPP_AUTH_PATH                     — project-wide default
    3. "/auth"                                       — built-in default
    """
    if group is not None:
        val = group.get_metadata_value("webapp_auth_path")
        if val:
            return val.rstrip("/")
    return settings.get("WEBAPP_AUTH_PATH", "/auth")


def get_api_base_url(request=None):
    """
    Resolve the origin this deployment's own API — and its landing pages — are
    served from.

    Lookup order:
    1. settings.BASE_URL            — the platform's public address
    2. the request's own origin     — correct by construction when we have one
    3. ""                           — a root-relative link, which still works
                                      when opened but is useless in an email;
                                      that is a BASE_URL misconfiguration, and
                                      readiness already reports it.
    """
    val = settings.get("BASE_URL", "") or ""
    if val:
        return val.rstrip("/")
    if request is not None:
        try:
            return request.build_absolute_uri("/").rstrip("/")
        except Exception:
            pass
    return ""


def build_token_url(flow, token, request=None, user=None, group=None):
    """
    Build the full URL an emailed token link should point at.

    For `email_verify`, `email_change` and `account_deactivate`:
        {api_base}{landing_path}?token={token}
    For every other flow (password_reset, magic_login, invite):
        {webapp_base}{auth_path}?flow={flow}&token={token}

    See the module docstring for why the two differ. The landing path is
    derived from the router's own mount prefix, so a deployment that mounts the
    framework somewhere other than /api gets working links automatically.

    The token is percent-encoded on the landing branch (the colon is kept, it
    is legal and readable): a signature can contain `+`, which a query string
    decodes back as a space.
    """
    prefix = LANDING_FLOW_PREFIXES.get(flow)
    if prefix:
        api_base = get_api_base_url(request=request)
        return f"{api_base}{token_landing.landing_path(prefix)}?token={quote(str(token), safe=':')}"
    base_url = get_webapp_base_url(request=request, user=user, group=group)
    auth_path = get_webapp_auth_path(group=group)
    return f"{base_url}{auth_path}?flow={flow}&token={token}"
