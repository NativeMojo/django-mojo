"""
Gated auth-handoff destinations. **Off by default.**

A destination host the DEPLOYMENT has declared gated receives a
GroupScopedToken package at `POST /api/auth/exchange` — a `gt1.` bearer
confined to one group, with no refresh token and no route back to a platform
JWT — instead of the access+refresh pair every other destination gets. The
OAuth completion leg refuses a gated destination outright (see `rest/oauth.py`).

The decision is taken at the DELIVERY boundary — the handoff mint, from the
already-server-validated `redirect_uri` — stamped into the Redis handoff
payload as `gid`, and honored at exchange without ever being re-derived. Never
at login, and never from a client-supplied `group_uuid`: an attacker simply
omits a parameter they control.

Sources, all FILE-ONLY (`settings.get_static`)
----------------------------------------------
  AUTH_HANDOFF_GROUP_TOKEN_MODE      "off" (default) / "monitor" / "enforce"
  AUTH_HANDOFF_GROUP_TOKEN_RESOLVER  dotted path to
                                     ``fn(url, request=None) -> Group | uuid |
                                     int pk | None``. When set it DECIDES and
                                     the static map is not consulted.
  AUTH_HANDOFF_GROUP_TOKEN_HOSTS     ``{host_entry: group_uuid}``

File-only is deliberate, and it is not symmetry with `AUTH_HANDOFF_*` (which
use `settings.get`). The resolver loads an arbitrary importable callable, and
the mode is a security switch: a DB/Redis-backed `Setting` row is writable
through the generic settings REST plane, so a remotely-writable mode would let
settings-write access silently downgrade every gated destination back to a
platform JWT. Group-scoped `Setting` rows are not consulted either, and neither
is `group.metadata` — a tenant must never hold the switch that decides which
credential its own visitors receive.

Enforce hard-requires the destination allowlist
-----------------------------------------------
`enforce` is meaningless unless `redirect_allowlist.is_enforced()` is also
true: a DENY map cannot enumerate "every host that is not mine", so a tenant
could point `?redirect=` at any host it controls that is merely ABSENT from
the map and collect a full JWT pair. Setting one without the other refuses
EVERY handoff — see `prerequisite_ok()`. Necessary, not sufficient: a resolver
that admits every registered tenant domain satisfies the prerequisite while
leaving hosts unmapped, which is why an allowlist-admitted destination that
resolves to no gating entry files `auth:handoff_group_token_unmapped` under
enforce. Gating is complete only when the allowlist and the gating map are
co-extensive per tenant zone.

The matcher is a DENY rule, and deliberately not the allowlist's
----------------------------------------------------------------
`redirect_allowlist`'s matcher requires exact scheme AND port equality, admits
exactly one extra label under `*.`, and returns "no match" for an unparsable
host. Every one of those is correct for an ALLOW rule and a live bypass in a
DENY rule: an entry of `https://gated.example.com` would fail to gate
`http://gated.example.com`, `https://gated.example.com:8443`,
`https://gated.example.com.` and `https://a.b.gated.example.com`, each minting
a plain-JWT handoff whose code lands right back on the gated origin. So:

  * entries are HOSTS. A full URL is accepted and reduced to its host with a
    warning — a deny rule must never be narrowed by a scheme, a port or a path.
  * every entry covers the host AND all of its subdomains, at any depth.
    `example.com` and `*.example.com` are the same rule and the `*.` is
    normalized away: a forgotten star is a silent hole, and
    `app.tenant.example.com` is the same tenant as `tenant.example.com`.
    Most specific (most labels) wins when several match.
  * IDN destinations must be listed in PUNYCODE, and IP-literal entries are
    refused in every encoding — we do not normalize numeric host forms and
    must not pretend to. A destination in one of those shapes is reported
    SUSPICIOUS, which under enforce means refused. `host_of()` returning False
    is the exact inversion of the allowlist's "no match": suspicious fails
    CLOSED.
  * a defective entry, or two entries normalizing to one host with different
    groups, is a compile-time map defect and refuses every handoff while
    enforcing. A dropped entry is a silent hole; a fatal entry is a loud,
    correctable outage.

Nothing here ever raises: `resolve_group()` answers with a three-outcome tuple
and the incident reporters swallow their own failures.
"""
import re
from urllib.parse import urlsplit

from mojo.helpers import logit, modules
from mojo.helpers.settings import settings


MODE_OFF = "off"
MODE_MONITOR = "monitor"
MODE_ENFORCE = "enforce"
_MODES = (MODE_OFF, MODE_MONITOR, MODE_ENFORCE)

# Destination hosts: plain ASCII host characters only. This is what refuses
# unicode IDN labels and bracketed IPv6 literals rather than guessing at them.
_HOST_CHARS = re.compile(r"^[a-z0-9_.-]+$")
# Entry hosts: at least two labels. Combined with the all-digits final-label
# test in _is_host_shaped this rejects every IP-literal encoding — dotted-quad,
# decimal, hex, octal, bracketed IPv6.
_ENTRY_CHARS = re.compile(r"^[a-z0-9_-]+(\.[a-z0-9_-]+)+$")

# Incident suppression: one report per destination host per outcome per hour.
_NOTICE_PREFIX = "account:handoff:group_token_alerted"
_RENOTIFY_SEC = 3600

# Compiled entry table + resolver, keyed by the raw configuration, mirroring
# redirect_allowlist._CACHE: a settings change (including a test
# server_settings override) naturally lands on a different key, and a dotted
# path that failed to import caches as None so it is logged once.
_CACHE = {}
_WARNED_INERT = "warned:configured_but_off"
_WARNED_URL_ENTRY = "warned:url_entry"


# ---------------------------------------------------------------------------
# Mode
# ---------------------------------------------------------------------------

def get_mode():
    """Return the normalized gating mode. Unknown strings are treated as
    `enforce`: a typo in a security switch must not silently disable it."""
    raw = settings.get_static("AUTH_HANDOFF_GROUP_TOKEN_MODE", MODE_OFF)
    mode = str(raw or MODE_OFF).strip().lower()
    if mode not in _MODES:
        logit.error(
            "account.handoff_group",
            f"AUTH_HANDOFF_GROUP_TOKEN_MODE {raw!r} is not one of {_MODES} — "
            f"treating it as {MODE_ENFORCE!r}")
        return MODE_ENFORCE
    if mode == MODE_OFF and not _CACHE.get(_WARNED_INERT) and _sources_configured():
        _CACHE[_WARNED_INERT] = True
        logit.warning(
            "account.handoff_group",
            "AUTH_HANDOFF_GROUP_TOKEN_HOSTS/_RESOLVER is configured but "
            "AUTH_HANDOFF_GROUP_TOKEN_MODE is 'off' — the gating map is inert "
            "and every destination receives a platform JWT")
    return mode


def is_enforcing():
    return get_mode() == MODE_ENFORCE


def _sources_configured():
    if settings.get_static("AUTH_HANDOFF_GROUP_TOKEN_RESOLVER", ""):
        return True
    return bool(settings.get_static(
        "AUTH_HANDOFF_GROUP_TOKEN_HOSTS", {}, kind="dict"))


def prerequisite_ok():
    """False when gating enforces without destination-allowlist enforcement.

    The caller refuses EVERY handoff in that case. Not "refuse only gated
    ones" and not "degrade to monitor" — both keep shipping platform JWTs
    while the operator believes gating is on, which is the whole failure this
    prerequisite exists to prevent. `monitor` never requires it, which is what
    makes a monitor rehearsal safe.
    """
    if not is_enforcing():
        return True
    from mojo.apps.account.services import redirect_allowlist
    return redirect_allowlist.is_enforced()


# ---------------------------------------------------------------------------
# Host parsing — the deny matcher
# ---------------------------------------------------------------------------

def _is_host_shaped(host):
    """Two-or-more ASCII labels whose final label is not all digits.

    The one rule that requires a real domain and rejects every IP-literal
    encoding. Applied to ENTRIES (an operator cannot gate an IP) and, per the
    same reasoning, to DESTINATIONS: no entry can ever be an IP form, so
    treating an IP-literal destination as "matches nothing" would be fail-OPEN
    for a deployment whose gated box is an internal address.
    """
    if not _ENTRY_CHARS.match(host):
        return False
    return not host.rsplit(".", 1)[-1].isdigit()


def host_of(url):
    """Return (ok, host) for a destination URL. `ok=False` means SUSPICIOUS.

    False is not "no match" — it fails CLOSED at the caller, the exact
    inversion of `redirect_allowlist._split` returning None. Scheme is not
    examined: `http`, `https` and protocol-relative `//host/x` all carry a
    host, while `javascript:`, `data:` and a bare relative path carry none.
    """
    if not url or not isinstance(url, str):
        return False, None
    raw = url.strip()
    # Python's urlsplit does not treat a backslash as an authority terminator;
    # browsers (WHATWG) do. `https://gated.example\@evil.tld/` is host
    # `evil.tld` here and host `gated.example` in a browser. Refusing is the
    # only safe reading of a parser differential.
    if "\\" in raw:
        return False, None
    try:
        parts = urlsplit(raw)
        host = parts.hostname
    except ValueError:
        # urlsplit defers a malformed port to attribute access.
        return False, None
    if not host:
        return False, None
    host = host.lower()
    if host.endswith("."):
        host = host[:-1]
    if not host or not _HOST_CHARS.match(host):
        return False, None
    if not _is_host_shaped(host):
        return False, None
    return True, host


def request_host(request):
    """The host this request arrived on, normalized, or None.

    HTTP_HOST is ALLOWED_HOSTS-validated by Django, so it is a trustworthy
    signal — unlike a white-label auth_domain on some other host, which this
    cannot see and which the documentation covers instead.
    """
    if request is None:
        return None
    try:
        raw = (request.META.get("HTTP_HOST") or "").strip()
    except Exception:
        return None
    if not raw:
        return None
    # "//" so urlsplit reads "example.com:8443" as an authority and not as a
    # scheme — a bare host:port parses as scheme "example.com" otherwise.
    ok, host = host_of(f"//{raw}")
    return host if ok else None


def is_own_host(request, url):
    """True when a gated destination is the auth origin itself (C9).

    Listing an auth-page origin in the gating map is a configuration error:
    the auth page short-circuits a same-origin redirect to direct navigation,
    and the JWT is already in that origin's storage.
    """
    mine = request_host(request)
    if not mine:
        return False
    ok, host = host_of(url)
    return bool(ok and host == mine)


def _entry_host(raw):
    """Normalize one gating-map key to a bare host, or None when unusable."""
    if not raw or not isinstance(raw, str):
        return None
    entry = raw.strip().lower()
    if not entry or "\\" in entry:
        return None

    if "//" in entry:
        try:
            entry = urlsplit(entry).hostname or ""
        except ValueError:
            return None
        _warn_url_entry(raw)
    else:
        # A bare "host/path" or "host:port" entry. A deny rule is never
        # narrowed by a path or a port, so both are dropped, loudly.
        if "/" in entry or ":" in entry:
            _warn_url_entry(raw)
            entry = entry.split("/", 1)[0].split(":", 1)[0]

    if entry.startswith("*."):
        entry = entry[2:]
    if entry.endswith("."):
        entry = entry[:-1]
    if not entry or not _is_host_shaped(entry):
        return None
    return entry


def _warn_url_entry(raw):
    if _CACHE.get(_WARNED_URL_ENTRY) == raw:
        return
    _CACHE[_WARNED_URL_ENTRY] = raw
    logit.warning(
        "account.handoff_group",
        f"AUTH_HANDOFF_GROUP_TOKEN_HOSTS entry {raw!r} carries a scheme, port "
        f"or path — only its host is used. A deny rule must not be narrowed "
        f"by them; list the bare host instead.")


def _compiled():
    """Return (table, defect). `defect` non-None means refuse everything."""
    raw = settings.get_static(
        "AUTH_HANDOFF_GROUP_TOKEN_HOSTS", {}, kind="dict") or {}
    cache_key = ("hosts", repr(sorted(
        ((str(k), str(v)) for k, v in raw.items()))))
    cached = _CACHE.get(cache_key)
    if cached is not None:
        return cached

    table = {}
    defect = None
    for key, value in raw.items():
        host = _entry_host(key)
        if host is None:
            defect = (f"AUTH_HANDOFF_GROUP_TOKEN_HOSTS entry {key!r} is not a "
                      f"usable host (IP literals and single-label names are "
                      f"refused; list IDN hosts in punycode)")
            break
        if not value or isinstance(value, bool) or not isinstance(value, (str, int)):
            defect = (f"AUTH_HANDOFF_GROUP_TOKEN_HOSTS entry {key!r} has no "
                      f"usable group reference: {value!r}")
            break
        if host in table and table[host] != value:
            defect = (f"AUTH_HANDOFF_GROUP_TOKEN_HOSTS maps host {host!r} to "
                      f"two different groups ({table[host]!r} and {value!r})")
            break
        table[host] = value

    result = (table, defect)
    _CACHE[cache_key] = result
    return result


def _get_resolver():
    """Return (is_configured, callable_or_None) — mirrors redirect_allowlist."""
    path = settings.get_static("AUTH_HANDOFF_GROUP_TOKEN_RESOLVER", "")
    if not path:
        return False, None
    cached = _CACHE.get(path, Ellipsis)
    if cached is not Ellipsis:
        return True, cached
    try:
        fn = modules.load_function(path)
    except Exception as exc:
        logit.error(
            "account.handoff_group",
            f"failed to load AUTH_HANDOFF_GROUP_TOKEN_RESOLVER {path!r}: "
            f"{exc} — refusing every gated decision")
        fn = None
    _CACHE[path] = fn
    return True, fn


def _match(table, host):
    """Most specific entry (most labels) covering `host`, else None."""
    best = None
    best_labels = -1
    for entry_host, value in table.items():
        if host == entry_host or host.endswith(f".{entry_host}"):
            labels = entry_host.count(".") + 1
            if labels > best_labels:
                best = value
                best_labels = labels
    return best


def _coerce_group(value):
    """Turn a resolver/map answer into (ok, group). Never raises."""
    from mojo.apps.account.models import Group

    if value is None:
        return True, None
    if isinstance(value, Group):
        group = value
    elif isinstance(value, bool):
        # bool is an int subclass — a True here is a config error, not a pk.
        logit.error(
            "account.handoff_group",
            f"gating resolved to {value!r}, which is not a group reference")
        return False, None
    elif isinstance(value, int):
        group = Group.objects.filter(pk=value).first()
    elif isinstance(value, str):
        group = Group.objects.filter(uuid=value.strip()).first()
    else:
        logit.error(
            "account.handoff_group",
            f"gating resolved to {type(value).__name__} {value!r}, which is "
            f"not a Group, uuid or pk")
        return False, None

    if group is None:
        logit.error(
            "account.handoff_group",
            f"gating references unknown group {value!r} — refusing")
        return False, None
    if not group.is_effectively_active():
        logit.error(
            "account.handoff_group",
            f"gating references inactive group {group.pk} (or an inactive "
            f"ancestor) — refusing")
        return False, None
    return True, group


def resolve_group(url, request=None):
    """Which group confines `url`? Returns (ok, group). NEVER raises.

      (True, None)   not gated — this destination gets a platform JWT
      (True, group)  gated — confine delivery to this group
      (False, None)  suspicious or misconfigured — the caller REFUSES under
                     enforce and reports under monitor
    """
    try:
        return _resolve_group(url, request)
    except Exception as exc:
        logit.error(
            "account.handoff_group",
            f"gating resolution failed for {url!r}: {exc} — refusing")
        return False, None


def _resolve_group(url, request):
    if not url:
        # No destination at all is never gated. Whether a handoff may happen
        # without one is the allowlist's question, not this module's.
        return True, None

    configured, resolver = _get_resolver()
    if configured:
        if resolver is None:
            return False, None
        try:
            return _coerce_group(resolver(url, request=request))
        except Exception as exc:
            logit.error(
                "account.handoff_group",
                f"AUTH_HANDOFF_GROUP_TOKEN_RESOLVER raised for {url!r}: "
                f"{exc} — refusing")
            return False, None

    table, defect = _compiled()
    if defect:
        logit.error("account.handoff_group", f"{defect} — refusing every handoff")
        return False, None
    ok, host = host_of(url)
    if not ok:
        return False, None
    return _coerce_group(_match(table, host))


def can_deliver(user, group):
    """Would this visitor actually receive a group token? READ-ONLY.

    A thin wrapper so this module never owns a copy of the mint guards.
    """
    from mojo.apps.account.services import group_token
    return group_token.can_mint(user, group)


# ---------------------------------------------------------------------------
# Incidents — suppressed, and they never raise
# ---------------------------------------------------------------------------

def _notice_key(*parts):
    """The Redis suppression key for one incident kind. Tests clear it."""
    return ":".join([_NOTICE_PREFIX] + [str(p or "-") for p in parts])


def _should_report(*parts):
    """One report per key per hour. True on a miss, False while suppressed."""
    key = _notice_key(*parts)
    try:
        from mojo.helpers.redis import get_connection
        redis = get_connection()
        if redis.get(key) is not None:
            return False
        redis.set(key, "1", ex=_RENOTIFY_SEC)
    except Exception as exc:
        logit.warning(
            "account.handoff_group",
            f"gating suppression check failed: {exc}")
    return True


def _report(body, title, category, level, request=None, **kwargs):
    try:
        from mojo.apps import incident
        incident.report_event(
            body, title=title, category=category, level=level,
            request=request, **kwargs)
    except Exception as exc:
        logit.error(
            "account.handoff_group",
            f"failed to file {category}: {exc}")


def report_preview(destination, group, would_refuse, request=None):
    """Monitor mode: what enforcement WOULD have done, before it binds."""
    _, host = host_of(destination)
    outcome = "refuse" if would_refuse else "deliver"
    if not _should_report("preview", host or destination, outcome):
        return
    if would_refuse:
        body = (
            f"MONITOR: {destination!r} is gated to group "
            f"{getattr(group, 'name', group)} (id {getattr(group, 'pk', None)}), "
            f"but this visitor would have been REFUSED under enforcement — they "
            f"are not a direct active member of that group (or they are a "
            f"superuser, who can never hold a group token). A platform JWT was "
            f"issued instead, as monitor mode does not bind.")
    else:
        body = (
            f"MONITOR: {destination!r} is gated to group "
            f"{getattr(group, 'name', group)} (id {getattr(group, 'pk', None)}). "
            f"Under enforcement this visitor would receive a group-scoped token "
            f"confined to that group instead of a platform JWT. A platform JWT "
            f"was issued, as monitor mode does not bind.")
    _report(body,
            f"Gated handoff destination ({outcome}): {host or destination}",
            "auth:handoff_group_token_preview", 3, request=request,
            destination=destination, destination_host=host or "",
            would_refuse=bool(would_refuse), group=group)


def report_refusal(destination, request=None, enforced=False):
    """A gated decision could not be taken safely (suspicious host, broken
    resolver, unknown or inactive group, defective map)."""
    _, host = host_of(destination)
    if not _should_report("refused", host or destination, enforced):
        return
    if enforced:
        body = (
            f"POST /api/auth/handoff was REFUSED and no code was minted: "
            f"group-token gating could not resolve {destination!r} safely. "
            f"Causes: a host this deployment cannot parse unambiguously (an IP "
            f"literal, a unicode IDN label, a backslash or a bracketed IPv6 "
            f"address), a resolver that raised or would not import, a gating "
            f"entry naming an unknown or inactive group, or a defective gating "
            f"map. Gating fails CLOSED — check the logs for the specific cause.")
    else:
        body = (
            f"MONITOR: group-token gating could not resolve {destination!r} "
            f"safely, so under enforcement this handoff would be REFUSED. A "
            f"platform JWT was issued. See the logs for the specific cause.")
    _report(body, f"Unresolvable gated handoff destination: {host or destination}",
            "auth:handoff_group_token_refused", 3, request=request,
            destination=destination, destination_host=host or "")


def report_unmapped(destination, request=None):
    """Enforce mode: an ALLOWED destination that no gating entry covers.

    The gating map is a deny rule, so an unmapped host is by definition not
    gated and receives a platform JWT — which is correct for an ordinary
    destination and a hole for one that should have been gated. The allowlist
    already decided this destination is somewhere this deployment sends
    tokens, so naming it here lets the incident feed write the gating map the
    way it already writes the allowlist.
    """
    _, host = host_of(destination)
    if not _should_report("unmapped", host or destination):
        return
    body = (
        f"Gating is ENFORCING, and {destination!r} passed the destination "
        f"allowlist but matches no AUTH_HANDOFF_GROUP_TOKEN_HOSTS entry — so "
        f"it received a full platform access+refresh pair. That is correct for "
        f"an ordinary destination. If this host belongs to a tenant whose "
        f"visitors should only ever hold a group-scoped token, add it to the "
        f"gating map: enforcement is complete only when the allowlist and the "
        f"gating map are co-extensive per tenant zone.")
    _report(body, f"Unmapped auth handoff destination: {host or destination}",
            "auth:handoff_group_token_unmapped", 3, request=request,
            destination=destination, destination_host=host or "")


def report_misconfigured(reason, kind, destination=None, request=None):
    """A deployment-configuration error, at a level that pages someone.

    `kind` names the defect ("prerequisite", "own_host") and is what the
    suppression key is built from, so one misconfiguration cannot drown out
    another and a test can clear exactly the key it is about to exercise.
    """
    _, host = host_of(destination) if destination else (False, None)
    if not _should_report("misconfigured", kind, host):
        return
    _report(reason,
            f"Auth handoff gating is misconfigured: {host or 'deployment'}",
            "auth:handoff_group_token_misconfigured", 2, request=request,
            destination=destination or "", destination_host=host or "")


def _reset_cache_for_tests():
    """Test-only: drop the compiled table, the resolver and the one-shot
    warnings. Production never calls this."""
    _CACHE.clear()
