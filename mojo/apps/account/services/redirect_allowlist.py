"""
Auth-handoff destination allowlist. **Enforcement is OPT-IN.**

`is_allowed_destination(url, request=None)` answers exactly one question: may
this deployment mint a cross-origin auth handoff code for `url`?
`is_enforced()` answers whether that answer is binding.

The check belongs at ISSUANCE. A handoff code is a bearer credential that
`POST /api/auth/exchange` trades for an access **and** refresh token pair, and
exchange is a server-to-server call from the consuming app — an attacker who
holds the code also controls every header on that request, so re-checking
Origin/Referer there is theatre. Refusing before a code exists is the only
place the answer cannot be spoofed.

Two sources, checked in order:

  AUTH_HANDOFF_RESOLVER      Dotted path to ``fn(url, request=None) -> bool``,
                             loaded via ``mojo.helpers.modules.load_function()``
                             and cached. When set it DECIDES — the static list
                             is not consulted. This is the answer for
                             multi-tenant platforms that cannot enumerate their
                             destinations in a settings file: implement one
                             function against your own domain registry.

  AUTH_HANDOFF_ALLOWED_URLS  Static list of allowed destination URLs, matched
                             by exact host + path prefix (see below).

The setting is the switch — there is no flag day
------------------------------------------------
NEITHER configured — what every existing deployment upgrades into — is
**monitor mode**: the handoff mints exactly as it always has, and a destination
that would not have passed files an `auth:handoff_destination_unlisted`
incident naming it. The incident feed writes your allowlist for you. Nothing
breaks on upgrade.

EITHER configured (a resolver path, or a list — even an empty one) turns on
**enforcement**: an unlisted destination refuses with a 400, mints no code, and
files an `auth:handoff_destination_refused` incident; a missing `redirect_uri`
refuses too.

Both incidents are suppressed to one per destination host per hour.

This mirrors `JOBS_ALLOWED_CHANNELS`, which learned the same lesson: hard
refusal by default is a flag day for every deployment that upgrades without
reading the changelog, and the security value of the check does not pay for
that. What the check buys, when you do turn it on, is narrow but real — it
closes a one-click token leak (`?redirect=` on a link to your own auth page,
opened by an already-signed-in user) that a password manager, a passkey and a
2FA prompt do **not** close, because the victim never types anything and the
URL bar shows your real domain the whole time. Weigh that against your own
population before enabling; see `docs/django_developer/account/auth.md`.

UNLIKE ``USER_LOGIN_HANDLER``, a resolver that raises does NOT fail open. The
exception is logged and treated as "refused" — a deployment resolver is
security-critical code, and a broken one must never open the gate. The same
goes for a dotted path that fails to import.

Static matching rules
---------------------
An entry matches a destination when ALL of:

  * scheme is ``http`` or ``https`` on both, and they are the SAME scheme (an
    ``https://`` entry never admits an ``http://`` destination — the code would
    then travel in cleartext);
  * port matches (scheme default when not explicit);
  * host matches exactly, case-folded, or the entry host is ``*.example.com``,
    which admits ``example.com`` plus exactly ONE additional non-empty,
    dot-free label — so ``a.example.com`` yes, ``a.b.example.com`` no, and
    ``example.com.evil.tld`` no;
  * the destination path is the entry path or sits underneath it on a path
    SEGMENT boundary — ``/app`` admits ``/app`` and ``/app/x`` but not
    ``/application``.

Host-only matching was deliberately rejected: it would make every path on an
allowed host a token-deposit site (an open redirector, a query reflector, an
analytics beacon that ships ``location.href``).

Hostnames are additionally required to be plain ASCII host characters. That is
not cosmetic — it closes the parser-differential class where Python keeps a
character inside the host that a browser treats as an authority terminator
(``https://attacker\\.example.com/`` parses here as one host ending in
``.example.com``, but a browser navigates to ``attacker``). List IDN
destinations in their punycode form.
"""
import re
from urllib.parse import urlsplit

from mojo.helpers import logit, modules
from mojo.helpers.settings import settings


_SCHEMES = ("http", "https")
_DEFAULT_PORTS = {"http": 80, "https": 443}
_HOST_CHARS = re.compile(r"^[a-z0-9._-]+$")

# Incident suppression: one report per destination host per hour, per mode.
_NOTICE_PREFIX = "account:handoff:dest_alerted"
_RENOTIFY_SEC = 3600

# Resolver cache keyed by the dotted path, mirroring
# mojo.apps.account.services.extensions — a settings change (including a test
# server_settings override) naturally lands on a different key. A path that
# failed to import caches as None so the error is logged once, not per request.
_CACHE = {}


def is_enforced():
    """
    Return True when this deployment has opted in to handoff destination checks.

    True when AUTH_HANDOFF_RESOLVER is a non-empty dotted path, or
    AUTH_HANDOFF_ALLOWED_URLS is *set at all* — including an empty list, which
    is a deliberate "enforce, and allow nothing". Unset means monitor mode; see
    the module docstring.
    """
    if settings.get("AUTH_HANDOFF_RESOLVER", ""):
        return True
    return settings.get("AUTH_HANDOFF_ALLOWED_URLS", None) is not None


def is_allowed_destination(url, request=None):
    """
    Return True when a handoff code may be minted for `url`.

    This is the allow/deny answer only — it does NOT consider whether the
    deployment has opted in. Callers pair it with `is_enforced()`; in monitor
    mode a False here is logged, not acted on.

    Never raises. Every failure mode — no configuration, unparsable URL, broken
    resolver dotted path, resolver exception — returns False.
    """
    if not url or not isinstance(url, str):
        return False

    configured, resolver = _get_resolver()
    if configured:
        if resolver is None:
            # Dotted path did not import. Refuse everything rather than
            # silently falling through to the static list.
            return False
        try:
            return bool(resolver(url, request=request))
        except Exception as exc:
            logit.error(
                "account.redirect_allowlist",
                f"AUTH_HANDOFF_RESOLVER raised for {url!r}: {exc} — refusing")
            return False

    return _matches_static_list(url)


def _get_resolver():
    """Return (is_configured, callable_or_None).

    is_configured distinguishes "no resolver set, use the static list" from
    "a resolver is set but broken, refuse everything".
    """
    path = settings.get("AUTH_HANDOFF_RESOLVER", "")
    if not path:
        return False, None
    cached = _CACHE.get(path, Ellipsis)
    if cached is not Ellipsis:
        return True, cached
    try:
        fn = modules.load_function(path)
    except Exception as exc:
        logit.error(
            "account.redirect_allowlist",
            f"failed to load AUTH_HANDOFF_RESOLVER {path!r}: {exc} — refusing all handoffs")
        fn = None
    _CACHE[path] = fn
    return True, fn


def _matches_static_list(url):
    candidate = _split(url)
    if candidate is None:
        return False
    allowed = settings.get("AUTH_HANDOFF_ALLOWED_URLS", [], kind="list") or []
    for raw_entry in allowed:
        entry = _split(raw_entry, allow_wildcard=True)
        if entry is None:
            logit.warning(
                "account.redirect_allowlist",
                f"ignoring unusable AUTH_HANDOFF_ALLOWED_URLS entry {raw_entry!r}")
            continue
        if _entry_matches(entry, candidate):
            return True
    return False


def _split(url, allow_wildcard=False):
    """Return (scheme, host, port, path) for an absolute http/https URL, else None.

    A wildcard entry keeps its leading ``*.`` in the returned host; only the
    allowlist side may carry one.
    """
    if not url or not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    scheme = (parts.scheme or "").lower()
    if scheme not in _SCHEMES:
        # Also how ``javascript:`` and ``data:`` are refused — no hostname.
        return None
    try:
        host = parts.hostname
        port = parts.port
    except ValueError:
        # urlsplit defers a malformed port to attribute access.
        return None
    if not host:
        return None
    host = host.lower()
    base = host[2:] if (allow_wildcard and host.startswith("*.")) else host
    if not base or not _HOST_CHARS.match(base):
        return None
    return scheme, host, port or _DEFAULT_PORTS[scheme], parts.path or "/"


def _entry_matches(entry, candidate):
    e_scheme, e_host, e_port, e_path = entry
    c_scheme, c_host, c_port, c_path = candidate
    if e_scheme != c_scheme or e_port != c_port:
        return False
    if not _host_matches(e_host, c_host):
        return False
    if c_path == e_path:
        return True
    if e_path.endswith("/"):
        return c_path.startswith(e_path)
    return c_path.startswith(e_path + "/")


def _host_matches(pattern, host):
    """Exact host, or one extra dot-free label under a ``*.base`` pattern."""
    if not pattern.startswith("*."):
        return pattern == host
    base = pattern[2:]
    if not base:
        return False
    if host == base:
        return True
    suffix = "." + base
    if not host.endswith(suffix):
        return False
    label = host[:-len(suffix)]
    return bool(label) and "." not in label


def report_unlisted_destination(destination, request=None, enforced=False):
    """
    File a SUPPRESSED incident for a handoff destination that is not allowed.

    This is what makes monitor mode useful rather than merely harmless: the
    incidents name every destination a deployment would have to allowlist, so
    the incident feed writes `AUTH_HANDOFF_ALLOWED_URLS` for you. Turn
    enforcement on once the feed goes quiet.

    Suppressed to one incident per destination host per hour (Redis), so a
    crafted-link flood cannot spam the incident plane — the host is the useful
    unit, since that is what goes in the allowlist.

    Never raises. In enforced mode the caller is already refusing the request;
    in monitor mode the mint must proceed regardless.
    """
    host = ""
    parts = _split(destination) if destination else None
    if parts is not None:
        host = parts[1]
    try:
        from mojo.helpers.redis import get_connection
        notice_key = f"{_NOTICE_PREFIX}:{'enforced' if enforced else 'monitor'}:{host or 'unparsable'}"
        redis = get_connection()
        if redis.get(notice_key) is not None:
            return
        redis.set(notice_key, "1", ex=_RENOTIFY_SEC)
    except Exception as exc:
        logit.warning(
            "account.redirect_allowlist",
            f"handoff destination suppression check failed: {exc}")
    if enforced:
        body = (
            f"POST /api/auth/handoff was REFUSED and no code was minted: "
            f"destination {destination!r} is not permitted by "
            f"AUTH_HANDOFF_ALLOWED_URLS or AUTH_HANDOFF_RESOLVER. If this "
            f"destination is legitimate, add it to the allowlist.")
        title = f"Refused auth handoff destination: {host or destination}"
        category = "auth:handoff_destination_refused"
    else:
        body = (
            f"POST /api/auth/handoff minted a code for {destination!r}, which "
            f"is NOT on the handoff allowlist. The code WAS issued — handoff "
            f"enforcement is opt-in and neither AUTH_HANDOFF_ALLOWED_URLS nor "
            f"AUTH_HANDOFF_RESOLVER is set, so nothing was blocked. A handoff "
            f"code buys an access AND refresh token pair for the signed-in "
            f"user, so every destination reported here is somewhere those "
            f"tokens can currently be sent. Add the legitimate ones to "
            f"AUTH_HANDOFF_ALLOWED_URLS; once that setting exists, anything "
            f"unlisted is refused instead of reported.")
        title = f"Unlisted auth handoff destination: {host or destination}"
        category = "auth:handoff_destination_unlisted"
    try:
        from mojo.apps import incident
        incident.report_event(
            body,
            title=title,
            category=category,
            level=3,
            request=request,
            destination=destination,
            destination_host=host)
    except Exception as exc:
        logit.error(
            "account.redirect_allowlist",
            f"failed to report handoff destination {destination!r}: {exc}")


def _reset_cache_for_tests():
    """Test-only helper to drop the cached resolver. Production never calls this."""
    _CACHE.clear()
