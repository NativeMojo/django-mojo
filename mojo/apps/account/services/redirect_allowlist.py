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

Two callers share the matcher
-----------------------------
`matches_allowlist(url, entries, ...)` is the public, pure form of the rules
below: no settings, no request, just "does this URL match one of these entries".
That is what lets two allowlists with different sources share one implementation
instead of maintaining two `urlsplit` loops that drift apart at the next
hardening:

  * this module's `AUTH_HANDOFF_ALLOWED_URLS` (`allow_wildcard=True`), and
  * `rest/oauth.py`'s `ALLOWED_REDIRECT_URLS`, the OAuth landing allowlist on
    the public `/begin` endpoint (`allow_wildcard=False` — a `*.` entry was
    dead config under the prefix test it replaced, and activating it inside a
    change that tightens everything else would widen an allowlist no operator
    re-consented to).

Static matching rules
---------------------
An entry matches an ``http(s)`` destination when ALL of:

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

Hostnames are additionally required to be plain ASCII host characters, and a URL
containing a raw backslash anywhere is refused outright. That is not cosmetic —
it closes the parser-differential class where Python keeps a character inside
the host that a browser treats as an authority terminator
(``https://attacker\\.example.com/`` parses here as one host ending in
``.example.com``, but a browser navigates to ``attacker``). The charset test
alone is NOT enough: it runs against ``parts.hostname``, which has already
discarded the userinfo, so ``https://evil.tld\\@app.example.com/`` reaches it as
a clean ``app.example.com`` while a browser reads the host as ``evil.tld``. List
IDN destinations in their punycode form.

Custom URL schemes (mobile deep links)
--------------------------------------
An entry may also name a custom scheme — ``myapp://callback``,
``com.example.app:/oauth`` — so a native app can complete OAuth on its own
deep link. A custom-scheme URL is not a web origin, so it is matched under its
own, deliberately narrower rules: exact case-folded **scheme**, exact
case-folded **authority** compared byte-for-byte, and the same
segment-bounded **path** prefix. No default-port logic (there are no default
ports), no ``_HOST_CHARS`` hostname rules (an authority here is an
app-registered label, not a DNS name), and no wildcards. The backslash guard
applies to every scheme.

The two families never mix: a custom-scheme entry cannot admit an ``http(s)``
URL, and an ``http(s)`` entry cannot admit a deep link. `_split_custom_scheme`
documents exactly which shapes parse and which fail closed —
``javascript:``/``data:``/``vbscript:`` and the opaque ``myapp:callback`` form
are refused outright.

This is one matcher, so custom schemes are usable in BOTH allowlists —
``ALLOWED_REDIRECT_URLS`` and ``AUTH_HANDOFF_ALLOWED_URLS``. That is intended:
the alternative is a per-caller scheme policy, which is exactly the drift the
shared implementation exists to prevent. A handoff code is a bearer credential,
so treat a deep-link handoff entry with the same care as a web one — the OS
decides which installed app receives that scheme.
"""
import re
from urllib.parse import urlsplit

from mojo.helpers import logit, modules
from mojo.helpers.settings import settings


_SCHEMES = ("http", "https")
_DEFAULT_PORTS = {"http": 80, "https": 443}
_HOST_CHARS = re.compile(r"^[a-z0-9._-]+$")
# RFC 3986 scheme grammar. urlsplit is looser in places, so a custom scheme is
# tested explicitly rather than assumed well-formed.
_SCHEME_CHARS = re.compile(r"^[a-z][a-z0-9+.-]*$")
# Script pseudo-schemes are a navigation sink, never a destination. They cannot
# match unless an operator lists one, but a matcher that would hand them back is
# a configuration footgun that ends in stored XSS on the auth origin, so they
# are refused outright — the same trio `mojo.helpers.urls` refuses.
_SCRIPT_SCHEMES = ("javascript", "data", "vbscript")

# Incident suppression: one report per destination host per hour, per mode.
_NOTICE_PREFIX = "account:handoff:dest_alerted"
_RENOTIFY_SEC = 3600

# Unusable-entry warnings are logged once per distinct entry, not once per
# request: the OAuth caller sits on a public, unauthenticated endpoint, so a
# per-request line is a log flood an anonymous caller drives for free. Entries
# come from deployment configuration, so "per distinct entry" is bounded by the
# configuration; the cap only guards against a future caller passing a
# per-request list.
_WARNED_ENTRY = "warned:entry"
_WARN_CAP = 256

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


def matches_allowlist(url, entries, source="allowlist", allow_wildcard=False):
    """Return True when `url` matches any entry in `entries`. Never raises.

    The shared matcher, in its pure form: it reads no settings and takes no
    request, which is exactly what lets two allowlists with different sources
    use one implementation (see the module docstring). `entries` is the caller's
    already-resolved list; `source` names the setting in the unusable-entry
    warning; `allow_wildcard` decides whether a ``*.host`` entry is honored or
    dropped as unusable.

    Matching is by parsed URL, never by string prefix — scheme, host and port
    compared after parsing, path prefix terminating on a segment boundary, query
    and fragment ignored on both sides. A custom-scheme entry (a mobile deep
    link) matches on exact scheme + exact authority + the same path rule. See
    "Static matching rules" and "Custom URL schemes" above.
    """
    candidate = _split(url)
    if candidate is None:
        return False
    for raw_entry in entries or []:
        entry = _split(raw_entry, allow_wildcard=allow_wildcard)
        if entry is None:
            _warn_unusable_entry(source, raw_entry)
            continue
        if _entry_matches(entry, candidate):
            return True
    return False


def _warn_unusable_entry(source, raw_entry):
    """Name an entry that can never match — once per distinct entry.

    An unusable entry is silent config rot: it does not widen anything, it just
    never matches, so the operator sees a destination refused for no visible
    reason. Rides the module cache so `_reset_cache_for_tests` re-arms it.
    """
    key = (_WARNED_ENTRY, source, repr(raw_entry))
    if key in _CACHE:
        return
    if len(_CACHE) < _WARN_CAP:
        _CACHE[key] = True
    logit.warning(
        "account.redirect_allowlist",
        f"ignoring unusable {source} entry {raw_entry!r}")


def _matches_static_list(url):
    # The candidate early-out stays AHEAD of the settings read: an unparsable
    # destination short-circuits before any Redis/DB settings lookup, and this
    # path is attacker-reachable on the public monitor leg.
    if _split(url) is None:
        return False
    allowed = settings.get("AUTH_HANDOFF_ALLOWED_URLS", [], kind="list") or []
    return matches_allowlist(
        url, allowed, source="AUTH_HANDOFF_ALLOWED_URLS", allow_wildcard=True)


def _split(url, allow_wildcard=False):
    """Return (scheme, host, port, path) for an absolute URL, else None.

    http(s) URLs take the branch below: a real DNS hostname, a scheme-default
    port, and optional ``*.`` wildcard support on the allowlist side (the
    leading ``*.`` stays in the returned host; only an entry may carry one).

    Anything else with a scheme goes to `_split_custom_scheme`, which returns
    the same 4-tuple shape with the raw authority in the host slot and None in
    the port slot. No scheme at all — ``//host/x``, ``/relative`` — refuses.
    """
    if not url or not isinstance(url, str):
        return None
    raw = url.strip()
    # Python's urlsplit does not treat a backslash as an authority terminator;
    # browsers (WHATWG) do. The `_HOST_CHARS` test below cannot cover this on
    # its own: it runs against `parts.hostname`, which has ALREADY discarded the
    # userinfo, so `https://evil.tld\@app.example.com/` arrives there as a clean
    # `app.example.com` while a browser navigates to `evil.tld`. Refusing any
    # raw backslash is the only safe reading of that disagreement — the sibling
    # `handoff_group._bare_host` carries the same guard for the same reason.
    if "\\" in raw:
        return None
    try:
        parts = urlsplit(raw)
    except ValueError:
        return None
    scheme = (parts.scheme or "").lower()
    if scheme not in _SCHEMES:
        return _split_custom_scheme(scheme, parts)
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


def _split_custom_scheme(scheme, parts):
    """Return (scheme, authority, None, path) for a custom-scheme URL, else None.

    A custom scheme is a mobile deep link — ``myapp://callback``,
    ``com.example.app:/oauth``. It is not a web origin: the authority is a label
    the app registered with the OS, not a DNS name, and there is no default
    port. So NONE of the http(s) rules apply here. The authority is compared
    byte-for-byte after case-folding (which is also why userinfo and
    percent-encoding cannot smuggle anything: ``myapp://evil@callback`` is
    simply a different authority, not a rewriting of ``callback``), the port
    slot is always None, and ``*.`` is inert — `_entry_matches` never reaches
    `_host_matches` for a custom scheme.

    Refused, fail-closed, because the shape cannot be read unambiguously:

      * no scheme at all (``//host/x``, ``/relative``), or a scheme `urlsplit`
        tolerated that is not RFC 3986 scheme syntax;
      * ``javascript:`` / ``data:`` / ``vbscript:`` — see `_SCRIPT_SCHEMES`;
      * the opaque form, where a non-empty path does not start with ``/``
        (``myapp:callback``, ``mailto:a@b``): there is no way to tell an
        authority from a path there;
      * a bare ``myapp:`` or ``myapp://`` with neither authority nor path —
        nothing to match on, and as an entry it would authorize a whole scheme.

    ``com.example.app:/oauth`` and ``com.example.app:///oauth`` are the SAME
    value: both carry an empty authority and the path ``/oauth``. An empty
    authority is a real, distinct value — it never equals ``callback``.
    """
    if not scheme or not _SCHEME_CHARS.match(scheme):
        return None
    if scheme in _SCRIPT_SCHEMES:
        return None
    authority = (parts.netloc or "").lower()
    path = parts.path or ""
    if path and not path.startswith("/"):
        return None
    if not authority and not path:
        return None
    return scheme, authority, None, path or "/"


def _entry_matches(entry, candidate):
    e_scheme, e_host, e_port, e_path = entry
    c_scheme, c_host, c_port, c_path = candidate
    if e_scheme != c_scheme or e_port != c_port:
        return False
    if e_scheme in _SCHEMES:
        if not _host_matches(e_host, c_host):
            return False
    elif e_host != c_host:
        # Custom scheme: exact, case-folded authority. `_host_matches` is
        # deliberately not reached, so a ``*.`` inside a custom-scheme entry is
        # an authority nothing will ever equal rather than a wildcard.
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
