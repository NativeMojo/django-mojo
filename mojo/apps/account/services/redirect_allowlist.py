"""
Auth-handoff destination allowlist.

`is_allowed_destination(url, request=None)` answers exactly one question: may
this deployment mint a cross-origin auth handoff code for `url`?

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

Neither configured → every cross-origin handoff is refused. Fail closed by
design: the alternative is a crafted ``?redirect=`` link handing a signed-in
user's refresh token to an attacker-chosen origin.

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

# Resolver cache keyed by the dotted path, mirroring
# mojo.apps.account.services.extensions — a settings change (including a test
# server_settings override) naturally lands on a different key. A path that
# failed to import caches as None so the error is logged once, not per request.
_CACHE = {}


def is_allowed_destination(url, request=None):
    """
    Return True when a handoff code may be minted for `url`.

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


def _reset_cache_for_tests():
    """Test-only helper to drop the cached resolver. Production never calls this."""
    _CACHE.clear()
