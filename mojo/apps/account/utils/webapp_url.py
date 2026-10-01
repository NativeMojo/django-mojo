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
import re
from urllib.parse import quote, urlsplit

from mojo.apps.account.services import redirect_allowlist, token_landing
from mojo.helpers import logit
from mojo.helpers.settings import settings

ALLOWED_ORIGINS_SETTING = "WEBAPP_ALLOWED_ORIGINS"
CATEGORY_BASE_REFUSED = "auth:webapp_base_url_refused"
DEFAULT_AUTH_PATH = "/auth"

_REFUSED_WINDOW = 3600
# Distinct refused hosts reported per window: the send endpoints are public,
# so a caller must not be able to mint one incident per made-up host.
_REFUSED_BUDGET = 50
_DEFAULT_PORTS = {"http": 80, "https": 443}
_HOST_CHARS = re.compile(r"^[a-z0-9._-]+$")
# Default for the keyword-only seams: read the value from settings.
_FROM_SETTINGS = object()


# Flow name -> token prefix, for the flows whose link opens a framework-served
# confirmation landing instead of the frontend auth page.
LANDING_FLOW_PREFIXES = {
    "email_verify": "ev",
    "email_change": "ec",
    "account_deactivate": "dv",
}


def allowed_origins(value=_FROM_SETTINGS):
    """Return the validated WEBAPP_ALLOWED_ORIGINS entries as a tuple.

    The operator's own extra frontends: token links may point at these as well
    as at WEBAPP_BASE_URL. Each entry is a bare http(s) origin with no path;
    `https://*.example.com` covers `example.com` and one label under it.
    Raises ImproperlyConfigured naming the setting and the bad entry.
    """
    from django.core.exceptions import ImproperlyConfigured

    if value is _FROM_SETTINGS:
        value = settings.get_static(ALLOWED_ORIGINS_SETTING, [])
    if not isinstance(value, (list, tuple)):
        raise ImproperlyConfigured(
            f"{ALLOWED_ORIGINS_SETTING} must be a list or tuple of http(s) "
            f"origins, not {type(value).__name__}")
    origins = []
    for entry in value:
        parsed = _parse(entry, allow_wildcard=True)
        if parsed is None or parsed[3] not in ("", "/"):
            raise ImproperlyConfigured(
                f"{ALLOWED_ORIGINS_SETTING} entry {entry!r} is invalid: it must "
                f"be an http(s) origin such as 'https://app.example.com' or "
                f"'https://*.example.com', with no path, query or credentials")
        origins.append(entry.rstrip("/"))
    return tuple(origins)


def clean_webapp_origin(value):
    """Return `value` when it is a well-formed http(s) URL, else None.

    Shape only: this does not say the origin is trusted. It keeps `null`,
    non-strings and script or credential-bearing URLs out of stored metadata.
    """
    return value if _parse(value) is not None else None


def _parse(value, allow_wildcard=False):
    """Return (scheme, host, port, path) for a plain http(s) URL, else None.

    Refuses anything a browser and urlsplit could read differently, or that
    could carry the link somewhere else: a non-string, whitespace or control
    characters, a backslash, userinfo, a query or a fragment.
    """
    if not value or not isinstance(value, str):
        return None
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7f for ch in value):
        return None
    if "\\" in value or "?" in value or "#" in value:
        return None
    try:
        parts = urlsplit(value)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None
    scheme = (parts.scheme or "").lower()
    if scheme not in _DEFAULT_PORTS or not host or "@" in parts.netloc:
        return None
    host = host.lower()
    bare = host[2:] if (allow_wildcard and host.startswith("*.")) else host
    if not bare or not _HOST_CHARS.match(bare):
        return None
    return scheme, host, port or _DEFAULT_PORTS[scheme], parts.path


def _origin(parsed):
    scheme, host, port, _ = parsed
    if port == _DEFAULT_PORTS[scheme]:
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def _operator_origins(operator_origins=_FROM_SETTINGS):
    """The operator's frontends: WEBAPP_BASE_URL, an absolute BASE_URL and
    WEBAPP_ALLOWED_ORIGINS. A relative or unset value is left out, so it never
    reaches the matcher as an unusable entry."""
    if operator_origins is _FROM_SETTINGS:
        entries = [settings.get("WEBAPP_BASE_URL"), settings.get("BASE_URL")]
        try:
            entries.extend(allowed_origins())
        except Exception as err:
            # ready() refuses to start on a bad value. A process that runs with
            # one anyway trusts nothing from it.
            logit.error(f"{ALLOWED_ORIGINS_SETTING} is invalid; ignoring it: {err}")
    else:
        entries = list(operator_origins or [])
    return [entry for entry in entries if _parse(entry, allow_wildcard=True)]


def _is_home_group(group, user):
    """True when `group` is in the tenant tree that created `user`'s account.

    Membership is deliberately not the test: add_member makes an active member
    with no consent from the user, so any group manager could claim it.
    """
    from mojo.apps.account.models import Group

    org = getattr(user, "org", None) if user is not None else None
    if org is None or not isinstance(group, Group) or not group.is_active:
        return False
    return group.top_most_parent.pk == org.top_most_parent.pk


def _metadata_str(holder, key):
    getter = getattr(holder, "get_metadata_value", None)
    if not callable(getter):
        return None
    val = getter(key)
    return val if val and isinstance(val, str) else None


def _select_trusted(candidate, operator, home_values):
    """Return the configured value `candidate` selects, or None.

    The return is always the configured value, never `candidate` itself, so a
    caller cannot choose a path on a trusted host. A wildcard entry has no
    single value to return, so it yields the candidate's bare origin.
    """
    parsed = _parse(candidate)
    if parsed is None:
        return None
    for entry in operator:
        if redirect_allowlist.matches_allowlist(
                candidate, [entry], source=ALLOWED_ORIGINS_SETTING, allow_wildcard=True):
            if _parse(entry, allow_wildcard=True)[1].startswith("*."):
                return _origin(parsed)
            return entry.rstrip("/")
    key = parsed[:3] + (parsed[3].rstrip("/"),)
    for home in home_values:
        home_parsed = _parse(home)
        if home_parsed and home_parsed[:3] + (home_parsed[3].rstrip("/"),) == key:
            return home.rstrip("/")
    return None


def _report_refused(source, value, request=None):
    """File one suppressed incident for an untrusted base. Never raises.

    Keyed by host, at most once an hour, budgeted and fail-closed: the callers
    are public endpoints, so a raw log line or event would be free
    amplification (see redirect_allowlist.report_refused_redirect_uri).
    """
    try:
        from mojo.apps import incident

        host = "unparsable"
        try:
            parsed_host = (urlsplit(value).hostname or "").lower()
            if _HOST_CHARS.match(parsed_host):
                host = parsed_host
        except (ValueError, TypeError, AttributeError):
            pass
        incident.report_event_suppressed(
            f"A token link (magic login, password reset or invite) was not sent "
            f"to {value!r:.200}, taken from {source}: it is not WEBAPP_BASE_URL, "
            f"not on {ALLOWED_ORIGINS_SETTING}, and not the frontend of the tenant "
            f"that created the account. The link went to the configured frontend "
            f"instead. If this host is one of your frontends, add it to "
            f"{ALLOWED_ORIGINS_SETTING}; otherwise it is a probe.",
            key=host,
            title=f"Refused token link host: {host}",
            category=CATEGORY_BASE_REFUSED,
            level=3,
            request=request,
            window=_REFUSED_WINDOW,
            budget=_REFUSED_BUDGET,
            fail_open=False,
            refused_source=source,
            refused_host=host)
    except Exception as err:
        logit.error(f"failed to report a refused webapp base url: {err}")


def _iter_webapp_bases(request, user, group, operator_origins):
    """Yield each usable base URL in lookup order. See get_webapp_base_url."""
    operator = _operator_origins(operator_origins)
    org = getattr(user, "org", None) if user is not None else None
    org_val = _metadata_str(org, "webapp_base_url") if org is not None else None
    group_val = _metadata_str(group, "webapp_base_url") if group is not None else None
    home_group = group_val is not None and _is_home_group(group, user)
    home_values = [val for val in (group_val if home_group else None, org_val) if val]

    def select(source, val):
        chosen = _select_trusted(val, operator, home_values)
        if chosen is None:
            _report_refused(source, val, request=request)
        return chosen

    if request is not None:
        val = request.DATA.get("webapp_base_url")
        if val:
            chosen = select("the request's webapp_base_url", val)
            if chosen is not None:
                yield chosen
    if group_val:
        if home_group:
            yield group_val.rstrip("/")
        else:
            chosen = select("a group outside the account's tenant", group_val)
            if chosen is not None:
                yield chosen
    if org_val:
        yield org_val.rstrip("/")
    val = settings.get("WEBAPP_BASE_URL") or ""
    if val:
        yield val.rstrip("/")
    if user is not None:
        val = user.get_protected_metadata("orig_webapp_url") or ""
        if val:
            chosen = select("the account's first-login origin", val)
            if chosen is not None:
                yield chosen
    if request is not None:
        val = request.META.get("HTTP_ORIGIN") or ""
        if val:
            chosen = select("the Origin header", val)
            if chosen is not None:
                yield chosen
    yield settings.get("BASE_URL", "/").rstrip("/")


def get_webapp_base_url(request=None, user=None, group=None, *,
                        operator_origins=_FROM_SETTINGS):
    """
    Resolve the frontend webapp base URL.

    Lookup order (first usable value wins):
    1. request.DATA["webapp_base_url"]            — selects a trusted frontend
    2. group.get_metadata_value(...)              — tenant group config (traverses parents)
    3. user.org.get_metadata_value(...)           — user's primary org
    4. settings.WEBAPP_BASE_URL                  — project-wide default
    5. user.metadata["protected"]["orig_webapp_url"] — recorded at first login
    6. request HTTP_ORIGIN                        — selects a trusted frontend
    7. settings.BASE_URL                          — final fallback

    The link built on this base carries a sign-in token, so only two kinds of
    value are trusted (#6225):

    * an operator origin — WEBAPP_BASE_URL, an absolute BASE_URL, or an entry
      of WEBAPP_ALLOWED_ORIGINS;
    * a home-tenant value — the `webapp_base_url` metadata of `user.org`, or of
      `group` when it is in the tenant tree that created the account.

    Steps 3, 4 and 7 are configured values and are used as they are. Steps 1,
    5 and 6, and step 2 for a group outside the account's tenant, can only
    SELECT a trusted value: the configured value that matched is returned,
    never the caller's string. Anything else is ignored and reported once per
    host per hour as `auth:webapp_base_url_refused`.

    `operator_origins` is a keyword-only test seam: the operator origins to
    match against, in place of the ones read from settings.
    """
    for base in _iter_webapp_bases(request, user, group, operator_origins):
        return base


def _valid_auth_path(val):
    """An auth path is appended to the base, so it must not be able to move the
    host: `@evil.tld/a` and `.evil.tld/a` both would."""
    if not isinstance(val, str) or not val.startswith("/") or "//" in val:
        return False
    return not any(ch in "@\\?#" or ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7f
                   for ch in val)


def get_webapp_auth_path(group=None, user=None):
    """
    Resolve the frontend auth path (e.g. "/auth" or "/login").

    Lookup order:
    1. group.get_metadata_value("webapp_auth_path")  — per-tenant override, only
                                                       for a group in the tenant
                                                       tree that created `user`
    2. settings.WEBAPP_AUTH_PATH                     — project-wide default
    3. "/auth"                                       — built-in default

    A value that is not a plain path (one leading "/", no "//", "@", "\\", "?",
    "#", whitespace or control character) is skipped.
    """
    if group is not None and _is_home_group(group, user):
        val = _metadata_str(group, "webapp_auth_path")
        if val and _valid_auth_path(val):
            return val.rstrip("/")
    val = settings.get("WEBAPP_AUTH_PATH", DEFAULT_AUTH_PATH)
    if val == "" or _valid_auth_path(val):
        return val
    return DEFAULT_AUTH_PATH


def get_api_base_url(request=None):
    """
    Resolve the origin this deployment's own API — and its landing pages — are
    served from.

    Lookup order:
    1. settings.BASE_URL            — the platform's public address
    2. ""                           — a root-relative link, which still works
                                      when opened but is useless in an email;
                                      that is a BASE_URL misconfiguration, and
                                      readiness already reports it.

    Deliberately NEVER derived from the request: under a permissive
    ALLOWED_HOSTS a poisoned Host header on a send endpoint would otherwise
    become the origin of an emailed single-use token link.
    """
    val = settings.get("BASE_URL", "") or ""
    if val:
        return val.rstrip("/")
    return ""


def build_token_url(flow, token, request=None, user=None, group=None, *,
                    operator_origins=_FROM_SETTINGS):
    """
    Build the full URL an emailed token link should point at.

    For `email_verify`, `email_change` and `account_deactivate`:
        {api_base}{landing_path}?token={token}
    For every other flow (password_reset, magic_login, invite):
        {webapp_base}{auth_path}?flow={flow}&token={token}

    See the module docstring for why the two differ. The landing path is
    derived from the router's own mount prefix, so a deployment that mounts the
    framework somewhere other than /api gets working links automatically.

    The frontend base is resolved by `get_webapp_base_url`, which only trusts a
    configured frontend; `operator_origins` is its test seam.

    The token is percent-encoded on the landing branch (the colon is kept, it
    is legal and readable): a signature can contain `+`, which a query string
    decodes back as a space.
    """
    prefix = LANDING_FLOW_PREFIXES.get(flow)
    if prefix:
        api_base = get_api_base_url(request=request)
        return f"{api_base}{token_landing.landing_path(prefix)}?token={quote(str(token), safe=':')}"
    auth_path = get_webapp_auth_path(group=group, user=user)
    for base_url in _iter_webapp_bases(request, user, group, operator_origins):
        url = f"{base_url}{auth_path}?flow={flow}&token={token}"
        # Re-read the finished URL: the base and path are both checked, and
        # this catches whatever a future source lets through.
        if _same_origin(base_url, url):
            return url
    return f"{DEFAULT_AUTH_PATH}?flow={flow}&token={token}"


def _same_origin(base_url, url):
    try:
        base, full = urlsplit(base_url), urlsplit(url)
        return (base.scheme.lower(), base.hostname, base.port) == \
               (full.scheme.lower(), full.hostname, full.port)
    except ValueError:
        return False
