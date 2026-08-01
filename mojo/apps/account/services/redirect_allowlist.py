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

Coercing a free-form source
---------------------------
`ALLOWED_REDIRECT_URLS` reaches the matcher already normalized to a list, because
it is read through `settings.get(kind="list")`. The per-group source does not:
`group.metadata["allowed_redirect_urls"]` is a free-form JSONField, so a tenant
can store a bare string, a JSON-array string, a number, a bool or an object where
a list belongs. `coerce_entries(value, source=...)` applies the SAME `kind="list"`
rules to it before it is matched, so the two sources cannot drift — a bare string
becomes the single entry it spells, a non-list scalar/object is dropped as an
unusable SOURCE (one suppressed signal, `[]` back), and a falsy value is `[]`
silently. That is what removes the `list(5)` `TypeError` (a 500 on the public
`/begin`) and the char-shattering of a bare string, without validating on write.

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
    ``/application``;
  * neither side's path carries a ``.`` or ``..`` segment in ANY ``%2e``
    spelling. Such a URL is refused OUTRIGHT on both sides rather than
    normalized: a browser resolves those segments before it issues the request,
    so admitting ``/oauth/callback/../../x`` under an entry of
    ``/oauth/callback`` would degrade the SEGMENT-bounded match above into
    exactly the host-only matching the next paragraph rejects.

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

from objict import objict

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

# WHATWG dot-segment refusal. A `.`/`..` path segment is resolved by the browser
# BEFORE it issues the request, so a candidate the matcher admits with such a
# segment escapes the segment-bounded path prefix (`/oauth/callback/../../x`
# under an entry of `/oauth/callback` lands off-prefix). It is refused outright
# rather than normalized here — see "Static matching rules" in the module
# docstring.
_PCT_DOT = re.compile(r"%2e", re.IGNORECASE)
_DOT_SEGMENTS = (".", "..")


def _has_dot_segment(path):
    """Return True when `path` carries a WHATWG dot segment.

    Flags exactly WHATWG's dot-segment set: a single-dot segment (`.`, `%2e`) or
    a double-dot segment (`..`, `.%2e`, `%2e.`, `%2e%2e`), ASCII
    case-insensitively — `%2e` is decoded to `.` before the split on `/`.

    Deliberately NOT decoded: `%252e` (a browser decodes it to the literal text
    `%2e`, never a dot) and `%2f` (WHATWG does not treat it as a segment
    delimiter, so `..%2f..%2fx` is one opaque segment, not a traversal).
    """
    return any(seg in _DOT_SEGMENTS for seg in _PCT_DOT.sub(".", path).split("/"))


# Incident suppression: one report per destination host per hour, per mode.
_NOTICE_PREFIX = "account:handoff:dest_alerted"
_RENOTIFY_SEC = 3600

# Redis-suppressed incident categories for the redirect allowlist. The two
# unusable-entry diagnostics and the refusal diagnostic used to be
# `logit.warning` lines; on the public `/begin` endpoint they were free
# amplification, since `group.metadata["allowed_redirect_urls"]` is
# `manage_group`-writable and `request.group` is anonymously selectable. They now
# file through `incident.report_event_suppressed` — Redis-suppressed, budgeted,
# never-raising. See `docs/django_developer/account/oauth.md` for the table.
CATEGORY_UNUSABLE_ENTRY = "auth:redirect_allowlist_unusable_entry"
CATEGORY_TENANT_ENTRY = "auth:redirect_allowlist_tenant_entry_unusable"
# A tenant SOURCE (the whole `metadata["allowed_redirect_urls"]` value) that
# cannot be coerced into a list of entries — a non-list JSON scalar/object, or a
# bracket-wrapped string that is not valid JSON. Distinct from
# CATEGORY_TENANT_ENTRY, which reports individual list MEMBERS that never match;
# this reports the source being the wrong SHAPE, so the whole per-group list is
# dropped. Same low-trust posture (level 1, budgeted, fail-closed). See
# `coerce_entries` / `_warn_unusable_source`.
CATEGORY_TENANT_SOURCE = "auth:redirect_allowlist_tenant_source_unusable"
CATEGORY_REDIRECT_REFUSED = "auth:oauth_redirect_refused"

# At most this many raw entries are quoted in an unusable-entry incident body;
# the incident still reports the true total count.
_UNUSABLE_SAMPLES = 5
# Per-hour budget on DISTINCT tenant groups that may each file an unusable-entry
# incident, so a tenant that mints many groups cannot turn per-group suppression
# into an unbounded event flood. The operator (deployment) category is
# self-bounding (one source name) and is not budgeted.
_TENANT_BUDGET = 25
# Per-hour budget on DISTINCT refused-redirect hosts, so an anonymous caller
# cannot mint one incident per fabricated host.
_REFUSAL_BUDGET = 50

# Resolver cache keyed by the dotted path, mirroring
# mojo.apps.account.services.extensions — a settings change (including a test
# server_settings override) naturally lands on a different key. A path that
# failed to import caches as None so the error is logged once, not per request.
# Resolver cache ONLY — the unusable-entry diagnostics no longer ride it.
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


def matches_allowlist(url, entries, source="allowlist", allow_wildcard=False,
                      request=None, tenant=False):
    """Return True when `url` matches any entry in `entries`. Never raises.

    The shared matcher, in its pure form: it reads no settings, which is exactly
    what lets two allowlists with different sources use one implementation (see
    the module docstring). `entries` is the caller's already-resolved list;
    `source` names the origin of the list (the setting name, or `group:<pk>` for
    a tenant list) and is the suppression key for the unusable-entry incident;
    `allow_wildcard` decides whether a ``*.host`` entry is honored or dropped as
    unusable. `request` and `tenant` only steer the unusable-entry incident
    (which category, level, budget and provenance) — they never change the match.

    Matching is by parsed URL, never by string prefix — scheme, host and port
    compared after parsing, path prefix terminating on a segment boundary, query
    and fragment ignored on both sides. A ``.``/``..`` path segment (any ``%2e``
    spelling) is refused before matching — applied to the candidate AND to every
    entry, so a dot-segment entry is dropped as unusable and admits nothing. A
    custom-scheme entry (a mobile deep link) matches on exact scheme + exact
    authority + the same path rule. See "Static matching rules" and "Custom URL
    schemes" above.

    Unusable entries (an entry `_split` rejects) are counted and, if there are
    any, reported ONCE via a Redis-suppressed incident after the scan — never a
    per-request log line, because these lists are attacker-amplifiable on the
    public `/begin` endpoint. The scan does NOT early-out on the first match: a
    broken entry sitting AFTER the matching one is still a deployment bug the
    operator should learn about, and a clean list (the common case) does zero
    extra Redis/DB work either way (the `if unusable_count` guard below).
    """
    candidate = _split(url)
    if candidate is None:
        return False
    matched = False
    unusable_count = 0
    unusable = []
    for raw_entry in entries or []:
        entry = _split(raw_entry, allow_wildcard=allow_wildcard)
        if entry is None:
            unusable_count += 1
            if len(unusable) < _UNUSABLE_SAMPLES:
                unusable.append(raw_entry)
            continue
        if not matched and _entry_matches(entry, candidate):
            matched = True
    if unusable_count:
        try:
            _report_unusable_entries(
                source, unusable, unusable_count, request=request, tenant=tenant)
        except Exception as exc:
            # The module contract is "never raises". A broken incident plane must
            # not turn an allowlist check into a 500 on the public endpoint.
            logit.error(
                "account.redirect_allowlist",
                f"failed to report unusable {source} entries: {exc}")
    return matched


def coerce_entries(value, source="allowlist"):
    """Coerce a free-form allowlist SOURCE into a list of entries. Never raises.

    Mirrors `settings.SettingsHelper._convert_value(value, "list")` EXACTLY, so
    the per-group source (`group.metadata["allowed_redirect_urls"]`, a free-form
    JSONField that never passed through `settings.get`) behaves identically to
    the deployment list. The two must not drift — a test pins them together.

      * a ``list`` is returned AS-IS, NOT copied. The caller reads FROM the
        returned list (it hands it to `matches_allowlist`, which never mutates
        its `entries`), so a copy would be dead work. A list with non-string
        members is still returned whole: a junk member fails closed in `_split`
        as an unusable ENTRY (the per-member `matches_allowlist` path), a
        different and narrower diagnostic than an unusable SOURCE.
      * a ``str`` wrapped in ``[`` … ``]`` is parsed as JSON; a JSON array is
        returned, anything else is broken JSON (comma-splitting it would
        manufacture nonsense entries) → an unusable SOURCE: one signal, ``[]``.
      * any other ``str`` is comma-split: ``"a,b"`` → ``["a", "b"]`` and
        ``"https://a/"`` → ``["https://a/"]``, the single entry it spells.
      * a falsy value (``None``, ``""``, ``[]``, ``{}``, ``0``, ``False``) is
        ``[]`` SILENTLY — for a self-service field, unset and empty are the same
        thing and not worth an incident.
      * anything else — a truthy int, bool, float, dict, tuple, set — is an
        unusable SOURCE: one signal, ``[]``. In particular a ``dict`` value's
        KEYS no longer act as entries (the old `list(value)` yielded its keys);
        write a list.

    `source` names the origin (``group:<pk>``) and is the suppression key of the
    unusable-SOURCE signal.
    """
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        if value.startswith("[") and value.endswith("]"):
            try:
                parsed = objict.from_json(value)
            except Exception:
                parsed = None
            if isinstance(parsed, list):
                return parsed
            # Bracket-wrapped but unparsable: comma-splitting would manufacture
            # nonsense entries (e.g. '["a"' , ']'), so the whole SOURCE is junk.
            _warn_unusable_source(source, value)
            return []
        return [x.strip() for x in value.split(",") if x.strip()]
    if not value:
        # None / "" / [] / {} / 0 / False — indistinguishable from unset for a
        # self-service field, so no signal.
        return []
    _warn_unusable_source(source, value)
    return []


def _warn_unusable_source(source, value):
    """File a Redis-suppressed incident for a SOURCE that cannot become a list.

    Distinct from `_report_unusable_entries` (which names individual list MEMBERS
    that never match): this fires when the source VALUE itself is the wrong shape
    — a non-list JSON scalar/object, or a bracket-wrapped string that is not valid
    JSON — so the whole per-group list is dropped and the tenant silently gets no
    entry. Same low-trust posture as the tenant unusable-ENTRY incident: level 1,
    keyed by `source` (``group:<pk>``), BUDGETED (`_TENANT_BUDGET` distinct
    sources/hour) and FAIL-CLOSED, because `request.group` is anonymously
    selectable and `metadata` is `manage_group`-writable. Never raises — the
    module contract; a broken incident plane must not 500 the public endpoint.
    """
    try:
        from mojo.apps import incident

        body = (
            f"The tenant redirect allowlist source ({source}) is a "
            f"{type(value).__name__} ({value!r:.200}) that cannot be read as a "
            f"list of URLs, so the whole per-group allowlist was dropped and the "
            f"tenant silently gets no entry. `metadata['allowed_redirect_urls']` "
            f"is `manage_group`-writable free-form JSON; set it to a JSON array "
            f"of URL strings.")
        incident.report_event_suppressed(
            body,
            key=source,
            title=f"Unusable tenant redirect allowlist source: {source}",
            category=CATEGORY_TENANT_SOURCE,
            level=1,
            window=_RENOTIFY_SEC,
            budget=_TENANT_BUDGET,
            fail_open=False,
            allowlist_source=source,
            value_type=type(value).__name__)
    except Exception as exc:
        logit.error(
            "account.redirect_allowlist",
            f"failed to report unusable {source} source: {exc}")


def _report_unusable_entries(source, samples, total, request=None, tenant=False):
    """File a Redis-suppressed incident naming entries that can never match.

    An unusable entry is silent config rot: it widens nothing, it just never
    matches, so a destination is refused for no visible reason. Two provenances,
    two postures:

      * tenant list (`group.metadata["allowed_redirect_urls"]`, `tenant=True`) —
        a tenant self-service value, so it is a low-severity (level 1) TENANT
        incident, BUDGETED (`_TENANT_BUDGET` distinct groups/hour) and
        FAIL-CLOSED, because `request.group` is anonymously selectable and the
        list is `manage_group`-writable — a Redis outage must not let it flood
        the incident table. `request` is passed so the auto-stamp attaches the
        tenant group.
      * deployment list (`ALLOWED_REDIRECT_URLS`, `AUTH_HANDOFF_ALLOWED_URLS`,
        `tenant=False`) — an operator config bug, so a higher-severity (level 3)
        UNUSABLE incident, self-bounding (one source name ⇒ no budget) and
        FAIL-OPEN, with `group=None` so it is never group-scoped even when the
        request that tripped it carried a group.

    Suppression key is `source`, so the whole list reports at most once per
    window regardless of how many entries are broken. Never raises.
    """
    from mojo.apps import incident

    shown = ", ".join(f"{s!r:.200}" for s in samples)
    plural = "entry" if total == 1 else "entries"
    if tenant:
        body = (
            f"The tenant redirect allowlist ({source}) has {total} unusable "
            f"{plural} that can never match any URL and were skipped. This list "
            f"is `metadata['allowed_redirect_urls']` on the group, writable by a "
            f"holder of manage_group; fix or remove the broken entries. "
            f"First {len(samples)}: {shown}.")
        incident.report_event_suppressed(
            body,
            key=source,
            title=f"Unusable tenant redirect allowlist entries: {source}",
            category=CATEGORY_TENANT_ENTRY,
            level=1,
            request=request,
            window=_RENOTIFY_SEC,
            budget=_TENANT_BUDGET,
            fail_open=False,
            allowlist_source=source,
            unusable_total=total)
    else:
        body = (
            f"The redirect allowlist setting {source} has {total} unusable "
            f"{plural} that can never match any URL and were skipped, so a "
            f"legitimate destination they were meant to admit is being refused. "
            f"Fix or remove them. First {len(samples)}: {shown}.")
        incident.report_event_suppressed(
            body,
            key=source,
            title=f"Unusable redirect allowlist entries: {source}",
            category=CATEGORY_UNUSABLE_ENTRY,
            level=3,
            request=request,
            window=_RENOTIFY_SEC,
            fail_open=True,
            group=None,
            allowlist_source=source,
            unusable_total=total)


def report_refused_redirect_uri(redirect_uri, request=None):
    """File a Redis-suppressed incident for a redirect_uri `/begin` refused.

    Replaces the per-request `logit.warning` refusal line, which was free
    amplification on the public, unauthenticated `/begin` endpoint. Keyed by the
    refused HOST (the useful unit — that is what an operator would allowlist or
    block) and BUDGETED (`_REFUSAL_BUDGET` distinct hosts/hour) and FAIL-CLOSED,
    so an anonymous caller cannot mint one incident per fabricated host and a
    Redis outage does not open that floodgate. Never raises.
    """
    from mojo.apps import incident

    parts = _split(redirect_uri)
    host = parts[1] if parts else "unparsable"
    body = (
        f"OAuth /begin refused redirect_uri {redirect_uri!r:.200}: it is not on "
        f"ALLOWED_REDIRECT_URLS and did not match the resolved group's "
        f"allowed_redirect_urls. On this public endpoint a refusal is a probe or "
        f"a misconfigured client; the host is what to allowlist or block.")
    incident.report_event_suppressed(
        body,
        key=host or "unparsable",
        title=f"Refused OAuth redirect_uri: {host or 'unparsable'}",
        category=CATEGORY_REDIRECT_REFUSED,
        level=3,
        request=request,
        window=_RENOTIFY_SEC,
        budget=_REFUSAL_BUDGET,
        fail_open=False,
        redirect_uri=redirect_uri,
        redirect_host=host)


def matchable_scheme(url):
    """Return the lowercased scheme of `url` IFF `_split` parses it, else "".

    This PARSES; it does NOT authorize. It answers one question — "is this a
    shape the matcher would even compare, and under what scheme?" — and is
    non-empty ONLY for a URL `_split` accepts. So every shape `_split` fails
    closed on comes back "": the script pseudo-schemes (`javascript:`, `data:`,
    `vbscript:`), the opaque `myapp:callback` form, a bare `myapp://`, a
    scheme-relative `//host/x`, a backslash-bearing URL, and — inherited through
    `_split` — any `.`/`..` dot-segment URL. An http(s) URL returns
    `"http"`/`"https"`; a well-formed custom-scheme deep link returns its scheme.

    It routes through `_split`, so it inherits every refusal `_split` makes (the
    dot-segment rule from #1101 included) and the scheme is returned already
    lowercased. It deliberately does NOT file an unusable-entry incident: the
    caller passes a per-request candidate, not a configured allowlist entry, and
    reporting on a candidate would let an anonymous caller drive the incident
    plane.
    """
    parts = _split(url)
    return parts[0] if parts is not None else ""


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

    A path carrying a ``.``/``..`` segment (any ``%2e`` spelling) refuses here,
    ahead of the scheme dispatch, so `_split_custom_scheme` inherits the rule.
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
    # Refuse a `.`/`..` path segment (any `%2e` spelling) rather than normalize
    # it — the browser resolves it before the request, so an admitted one
    # escapes the segment-bounded prefix. Run it on `parts.path` ONLY: a
    # raw-string check would both miss cases and wrongly refuse a legitimate
    # `?next=../x` query, and a `%2E` in the AUTHORITY stays the `_HOST_CHARS`
    # test's job below. Placed ahead of the scheme dispatch so
    # `_split_custom_scheme` inherits it.
    if _has_dot_segment(parts.path or ""):
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
        nothing to match on, and as an entry it would authorize a whole scheme;
      * a ``.``/``..`` path segment (any ``%2e`` spelling) — already refused by
        `_split` before this function is reached, so it never gets here.

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

    NOTE: this predates `incident.report_event_suppressed` and hand-rolls the
    same Redis suppression. New code should use `report_event_suppressed`
    (see `report_refused_redirect_uri` above); this one is left as-is because a
    test pins its exact `_NOTICE_PREFIX` key shape and per-mode bucketing.

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
    elif destination:
        # `_split` refused it (a dot segment, a backslash, unparsable), but the
        # real host still buckets the suppression key and `destination_host`
        # better than collapsing every refused destination into one shared
        # `unparsable` slot — recover it directly, defensively.
        try:
            host = urlsplit(destination).hostname or ""
        except ValueError:
            host = ""
    try:
        from mojo.helpers.redis import get_connection
        notice_key = f"{_NOTICE_PREFIX}:{'enforced' if enforced else 'monitor'}:{host or 'unparsable'}"
        redis = get_connection()
        if redis.get(notice_key) is not None:
            return
        redis.set(notice_key, "1", ex=_RENOTIFY_SEC)
    except Exception as exc:
        # DELIBERATELY a file log, not an incident. This IS the suppression
        # machinery's own degraded path (Redis is unreachable). Filing an
        # incident here is unsuppressible-by-construction — the suppression it
        # would need is the very thing that just failed — and would recurse on
        # any report_event fault. Execution falls through and reports the
        # destination UNSUPPRESSED on purpose while Redis is down. A later
        # log-to-incident sweep must leave this line as a file log.
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
    """Test-only helper to drop the cached resolver. Production never calls this.

    `_CACHE` holds ONLY the AUTH_HANDOFF_RESOLVER lookup now; the unusable-entry
    diagnostics moved to Redis-suppressed incidents and no longer ride it, so
    this clears the resolver cache and nothing else. Incident suppression is
    reset by clearing the Redis notice/budget keys (see `incident.notice_key` /
    `incident.budget_key`), not this helper.
    """
    _CACHE.clear()
