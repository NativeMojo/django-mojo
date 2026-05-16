# Geofencing Policy Engine (System + Group Levels)

**Type**: request
**Status**: planned
**Date**: 2026-05-15
**Priority**: high

## Description

Add a geofencing policy engine to django-mojo that lets consumer apps gate
auth + sensitive actions based on the requester's IP geolocation and
abuse signals (Tor / VPN / datacenter). Rules are expressed as a JSON
DSL and apply at two levels:

1. **System level** — platform-wide rules in settings (always-on,
   acts as a hard floor — group rules cannot loosen).
2. **Group level** — per-tenant rules in `Group.metadata.geofence`
   (layered on top of system rules; can further restrict).

The engine exposes a single decision API, a public-pre-flight REST
endpoint for UI use, and a decorator that any endpoint can apply for
enforcement. It integrates with the existing `user_registered` /
`user_logged_in` signals so consumer apps can refuse at auth time
without writing custom view code.

This builds on the existing `mojo/helpers/geoip/` infrastructure — IP
lookups, multi-provider config (ipinfo / MaxMind / ipapi / ipstack),
abuse-signal extraction, and the `GeoLocatedIP` cache model are all
already in place. This request adds only the policy/decision layer.

## Context

Many django-mojo consumer apps have compliance, regulatory, or
legitimate-traffic concerns that require geofencing:

- Apps with jurisdictional restrictions (only serve users from
  certain countries / sub-national regions).
- Apps with abuse-traffic concerns (always block Tor, datacenter
  IPs, anonymous VPNs).
- Apps with sanctions-list obligations (always block specific
  countries).

Today each app has to roll its own gate — call `geolocate_ip()`,
write its own rule evaluation, decide where to enforce, build its own
admin UI for managing the rules. The IP lookup is already centralized
in the framework, but the policy layer is not. That leaves consistency
on the floor: some apps gate login but not register; some allow
admins to bypass and some don't; some return generic 403s and some
return helpful "not available in your region" messages.

A framework-level policy engine standardizes the decision contract,
the rule shape, the enforcement points, and the bypass permission —
so every consumer app does it the same way and UI clients can
pre-flight a single endpoint to render appropriate messaging.

## Acceptance Criteria

### Rule DSL

- Rules are expressed as a JSON object with three top-level keys —
  `country`, `region`, `abuse` — each containing simple matchers.
  Example:
  ```json
  {
    "country": {"in": ["US", "CA"]},
    "region":  {"in": ["US-FL", "US-NJ", "US-PA"]},
    "abuse":   {"tor": false, "vpn": false, "datacenter": false}
  }
  ```
- Supported matcher operators:
  - `{"in": [...]}` — value must be in the list
  - `{"not_in": [...]}` — value must NOT be in the list
  - `{"eq": "..."}` — strict equality
- For `abuse` flags, the value is a boolean or `null`:
  - `false` — flag must be False (e.g. `{"tor": false}` means
    Tor IPs are not allowed)
  - `true` — flag must be True (rare; not gaming-relevant)
  - `null` or absent — don't care
- Region uses ISO 3166-2 codes (e.g. `US-FL`) — the
  `geolocate_ip` output's `region` field is mapped to this format.
- Empty rule object `{}` is a no-op (allows everything).

### Decision engine (`mojo/apps/account/services/geofence/engine.py`, new)

- `GeoFenceEngine.check(request, group=None) → GeoDecision`
- Decision flow:
  1. Resolve IP from request (existing `request.ip`).
  2. Look up geo + abuse signals via `mojo.helpers.geoip.geolocate_ip`
     (with `check_threats=True`).
  3. Evaluate system rules from `GEOFENCE_SYSTEM_RULES` setting.
  4. If system check passes, evaluate group rules from
     `group.metadata.geofence` (if `group` is provided).
  5. Return a `GeoDecision` (dataclass / objict) with the full
     evaluation.
- Decisions are cached in Redis keyed by `(ip, group_id_or_none)`
  with TTL `GEOFENCE_CACHE_TTL` (default 300s). Bypass permission
  short-circuits the cache lookup (no caching for bypassed users).
- Composition rule: system + group are AND-ed; both must pass.
  System failure short-circuits and group rules are not evaluated.

### GeoDecision shape

```python
{
    "allowed": bool,
    "reason": str | None,   # "country_not_allowed", "region_not_allowed",
                            # "tor_detected", "vpn_detected",
                            # "datacenter_detected", "system_block",
                            # "lookup_failed"
    "detail": str | None,   # human-readable, suitable for UI
    "ip": str,
    "country": str | None,  # ISO 3166-1 alpha-2
    "region": str | None,   # ISO 3166-2
    "abuse": {"tor": bool, "vpn": bool, "datacenter": bool, "proxy": bool},
    "checked_at": datetime,
    "rule_level": "system" | "group" | None,  # which level rejected
}
```

### REST endpoint

- `GET /api/geo/check?group=<uuid>` — public, rate-limited.
  Returns `GeoDecision` for the calling IP against the named group
  (or system-only if `group` omitted).
- Intended for UI pre-flight: render a "not available in your region"
  page instead of letting the user attempt a login they can't complete.
- Public so unauthenticated pages can use it. The endpoint itself
  is NOT geofenced (otherwise users could never see *why* they're
  blocked).

### Enforcement decorator

- `@md.requires_geofence(scope="auth"|"action"|None)` —
  drop-in decorator that wraps any endpoint. Pulls the active group
  from the request (existing `request.group` middleware), calls
  `GeoFenceEngine.check`, returns 403 with the `GeoDecision` payload
  if blocked.
- `scope` is informational metadata for logging/metrics; it does
  not change the check logic. Endpoints can pass any string.
- If user has `bypass_geofence` permission, the decorator is a
  no-op (returns through to the wrapped view).

### Signal handlers (in consumer apps; framework provides hooks)

The framework does not auto-attach handlers — it documents the
pattern and provides a tested helper:

- `mojo.apps.account.services.geofence.helpers.enforce_or_raise(
   request, group) → None | raises PermissionDeniedException(decision)`
- Consumer apps register their own receivers for `user_registered`
  / `user_logged_in` and call this helper. Documenting the helper
  + showing an example handler in docstrings is the framework's
  contribution; the wiring per app is the consumer's call.

### Bouncer integration (optional, follow-up)

- `mojo/apps/account/services/bouncer/environment.py` —
  `EnvironmentService.analyze_request` already builds a geo-aware
  signal set. Extend it to read the geofence decision and bump risk
  score on `decision.allowed=False` so the bouncer pre-screen can
  serve a decoy *before* the user attempts to authenticate.
- This is documented as a follow-up in this request; not in v1
  acceptance criteria.

### Bypass permission

- New permission key: `bypass_geofence`. Users holding this permission
  pass any geofence check (decorator no-ops, signal-handler helper
  no-ops). Decisions are not cached when bypassed (so revoking the
  permission immediately re-enforces).
- Added to django-mojo's documented permission list.

## Investigation

**What exists**:
- `mojo/helpers/geoip/__init__.py:28` — `geolocate_ip(ip,
  check_threats=False)` returns country / region / tor / vpn /
  proxy / datacenter signals.
- `mojo/helpers/geoip/{ipinfo,maxmind,ipapi,ipstack}.py` —
  multi-provider lookup, configurable.
- `mojo/helpers/geoip/threat_intel.py` and `detection.py` —
  abuse-signal detection (Tor exit nodes, etc.).
- `mojo/apps/account/models/geolocated_ip.py` — `GeoLocatedIP`
  model with persisted lookups, including `is_known_abuser`,
  `is_mobile`, etc.
- `mojo/apps/account/services/bouncer/` — risk scoring + pre-screen
  that *could* consume geofence decisions in a follow-up.
- `mojo.apps.account.models.Group.metadata` — JSON field already
  used for oauth config, branding, allowed_redirect_urls; perfect
  home for `geofence` config.
- `@md.requires_perms(...)` decorator pattern — model for
  `@md.requires_geofence(...)`.
- `request.group` middleware — existing way to pick the active
  group off a request.

**What changes**:
- `mojo/apps/account/services/geofence/` — new service package.
  - `engine.py` — `GeoFenceEngine`, `GeoDecision`.
  - `dsl.py` — rule parser + matcher (`{in}`, `{not_in}`, `{eq}`,
    abuse flags).
  - `helpers.py` — `enforce_or_raise(request, group)` for use
    inside consumer-app signal handlers.
- `mojo/decorators/geofence.py` (or extend existing decorators) —
  `requires_geofence` decorator.
- `mojo/apps/account/rest/geofence.py` (new) — `GET /api/geo/check`.
- Settings: new defaults in
  `config/settings/defaults/geofence.py` (or wherever the
  framework's default settings live).
- Documentation: new doc page in `docs/django_developer/` covering
  the rule DSL, decision shape, decorator usage, and bypass
  permission.

**Constraints**:
- Backward compatibility: no existing endpoints change behavior
  unless they opt in via `@requires_geofence` or via a
  consumer-app signal handler. Default state of the framework is
  "no geofencing." Settings with no `GEOFENCE_SYSTEM_RULES`
  defined → engine returns `allowed=True` for everything.
- IP lookup failures (provider down, IP not resolvable) must not
  crash auth. Decision is `allowed=True` (fail-open) by default,
  with `reason="lookup_failed"` so consumer apps can log /
  monitor. A `GEOFENCE_FAIL_CLOSED` setting (default False) lets
  high-security apps flip the default.
- Performance: every gated request is a cache hit in steady state.
  First-touch per IP is a single geoip lookup (already cached by
  `GeoLocatedIP`). Avoid extra round-trips.
- Local dev: provide a `GEOFENCE_TEST_OVERRIDE` setting (object
  with `country`, `region`, `is_tor`, `is_vpn`, etc.) that, when
  set, replaces the geoip lookup result. Local devs and the test
  suite use this to exercise blocked-region behavior without
  having to route traffic through real IPs.
- IPv6: must work. `geolocate_ip` already handles it; the policy
  layer is IP-version-agnostic.

**Related files**:
- `mojo/helpers/geoip/__init__.py`
- `mojo/apps/account/models/group.py`
- `mojo/apps/account/services/bouncer/environment.py`
- `mojo/decorators/` (wherever existing `requires_*` decorators
  live)
- `mojo/apps/account/services/auth_handoff.py` — geofence helper
  should be importable alongside this for consumer-app handlers.

## Endpoints

| Method | Path | Description | Permission |
|---|---|---|---|
| GET | `/api/geo/check` | Pre-flight geofence decision for UI. Optional `?group=<uuid>` to evaluate against a specific tenant. Returns full `GeoDecision`. | Public, rate-limited |

## Settings

| Setting | Default | Purpose |
|---|---|---|
| `GEOFENCE_SYSTEM_RULES` | `{}` (no system rules) | JSON rule DSL applied to every request before group rules. Hard floor — group rules cannot loosen it. |
| `GEOFENCE_CACHE_TTL` | `300` | Seconds to cache decisions in Redis, keyed by `(ip, group_id)`. |
| `GEOFENCE_FAIL_CLOSED` | `False` | When an IP lookup fails, deny instead of allow. Default fails open for resilience. |
| `GEOFENCE_TEST_OVERRIDE` | `None` | Dict with geoip-shaped fields (`country_code`, `region`, `is_tor`, etc.) that, when set, bypasses real lookup. For local dev + tests only. |
| `GEOFENCE_ENABLED` | `True` | Master kill-switch. When `False`, every decision is `allowed=True`. Useful for emergencies / local dev. |

## Tests Required

- DSL parser: each matcher operator (`in`, `not_in`, `eq`) returns
  correct truth value for representative inputs.
- DSL parser: malformed rule raises a clear error at config-load
  time (not at request time).
- Engine: system-only rules — allowed and blocked country cases.
- Engine: group-only rules — allowed and blocked country cases
  (system rules empty).
- Engine: composition — system allows but group blocks → blocked
  with `rule_level="group"`. System blocks → group is not
  evaluated, `rule_level="system"`.
- Engine: abuse signals — `tor=false` rule + IP is Tor → blocked
  with `reason="tor_detected"`. Same for VPN, datacenter.
- Engine: IP lookup failure → fails open (allowed=True,
  `reason="lookup_failed"`) by default. `GEOFENCE_FAIL_CLOSED=True`
  → fails closed.
- Engine: cache — repeated `check` with same `(ip, group)` does
  one geoip lookup. Different group → second lookup (intentional —
  group rules differ).
- Engine: `bypass_geofence` permission — user with permission
  short-circuits to `allowed=True`, no cache write.
- Decorator: blocked endpoint returns 403 with the decision in
  body. Allowed endpoint returns through.
- Decorator: user with `bypass_geofence` permission always
  returns through.
- Endpoint: `GET /api/geo/check?group=<uuid>` returns expected
  decision shape for happy path and blocked path. Unknown group
  uuid → 400.
- `enforce_or_raise` helper: raises `PermissionDeniedException`
  with the decision attached for use inside signal handlers.
- `GEOFENCE_ENABLED=False` master kill: every decision is
  `allowed=True` regardless of rules.
- `GEOFENCE_TEST_OVERRIDE` — supplying override bypasses real
  geoip lookup and returns expected decision.

## Out of Scope

- Bouncer pre-screen integration (raising risk score on geofence
  block). Documented as a follow-up; not part of v1 acceptance.
- An admin UI / CRUD REST for managing `GeoFenceRule` records.
  v1 uses `Group.metadata.geofence` + settings. A first-class
  model can come later if audit trails or multi-rule-set support
  is needed.
- Time-window rules (e.g., "block this region only between
  midnight and 6am"). Static rules only in v1.
- IP-allowlist support (specific corporate IPs always pass). Can
  be done by consumer apps via the bypass permission; not in v1.
- Mobile-network-vs-residential differentiation. The signal exists
  in `GeoLocatedIP.is_mobile` but is not part of the v1 DSL.
- Reverse geofencing (require physical proximity to a known
  location). Different problem — needs precise lat/long + opt-in
  device location, not IP geolocation.

## Open Questions

1. **Rule precedence within a level**: if a group sets
   `{"country": {"in": ["US"]}}` AND
   `{"country": {"not_in": ["RU"]}}`, what wins? Default proposal:
   the rule object is a single object, not an array — so the
   *last* key written wins (Python dict semantics). If multiple
   rule sets are ever needed, they belong in a separate model
   (see Out of Scope).
2. **`bypass_geofence` permission scope**: should bypass also
   skip the abuse-signal checks (Tor / VPN / datacenter), or only
   the country/region rules? Default proposal: bypasses everything
   (full short-circuit). Tighten only if support staff legitimately
   need to *enforce* abuse blocks even when they're testing from
   home.
3. **Decision payload exposure on 403**: returning the full
   `GeoDecision` to the caller leaks signal details that may be
   useful to attackers crafting bypass attempts (e.g., "Tor
   detected" tells them the platform sees Tor). Alternative: 403
   with a generic message and the full decision only in server
   logs. Default proposal: return `reason` + `detail` only;
   suppress `country`/`region`/`abuse` from the response. UI
   pre-flight endpoint returns full payload because it's already
   per-user.
4. **`group=None` semantics on `GET /api/geo/check`**: return
   only system-level evaluation, or 400? Default proposal: return
   system-only (useful for marketing landing pages that aren't
   tied to a specific tenant).

## Plan

**Status**: planned
**Planned**: 2026-05-15

### Objective
Add a framework-level geofencing precondition (decorator + DSL engine + per-IP cache + pre-flight endpoint) that gates all built-in auth endpoints when rules are configured, and is a zero-cost no-op when no rules are defined.

### Scope Clarifications (changes from request file framing)
- **No integration with `USER_REGISTERED_HANDLER` / `USER_LOGIN_HANDLER`.** Those are consumer extension points for downstream workflows and are orthogonal. The request file's "signal integration" framing pre-dates the asymmetric error contract those hooks ship with — geofence is enforced at the HTTP layer via decorator, not inside extension handlers.
- **Default = no-op.** When `GEOFENCE_SYSTEM_RULES={}` (default) AND group has no `metadata.geofence`, the engine returns `allowed=True, reason="no_rules"` without performing a geoip lookup. Apps that don't configure rules pay zero cost.
- **No `enforce_or_raise` helper in v1.** Removed from scope — the only consumer of that helper was the now-dropped handler-integration path. If a future v2 needs non-HTTP enforcement (jobs, etc.) it can ship then.

### Files Touched

**Provider layer (region code plumbing):**
1. `mojo/helpers/geoip/maxmind.py` — emit `region_code` from `response.subdivisions.most_specific.iso_code` (ISO 3166-2 like `US-FL`).
2. `mojo/helpers/geoip/ipinfo.py` — emit `region_code` from response.
3. `mojo/helpers/geoip/ipapi.py`, `ipstack.py` — emit `region_code` where the provider supports it; else None.
4. `mojo/helpers/geoip/__init__.py` — pass `region_code` through the normalized return dict.

**Model:**
5. `mojo/apps/account/models/geolocated_ip.py` — add `region_code = CharField(max_length=10, null=True, blank=True, db_index=True)`. Populate in `refresh()`. New migration.

**Geofence service (new package):**
6. `mojo/apps/account/services/geofence/__init__.py` — re-export `GeoFenceEngine`, `GeoDecision`, `evaluate_rule`, `validate_rule`.
7. `mojo/apps/account/services/geofence/dsl.py` — `evaluate_rule(rule_dict, geo) -> (allowed, reason, level_field)`. Matchers `in`/`not_in`/`eq` for `country` (uses `country_code`) and `region` (uses `region_code`); boolean checks for `abuse.tor` / `abuse.vpn` / `abuse.datacenter` / `abuse.proxy`. `validate_rule(rule_dict)` raises `ValueError` with clear message for malformed shape (unknown top-level key, unknown operator, non-list operand for `in`/`not_in`).
8. `mojo/apps/account/services/geofence/engine.py` — `GeoFenceEngine.check(request, group=None, user=None) -> GeoDecision` (objict). Decision flow:
   1. If `GEOFENCE_ENABLED=False` → `allowed=True, reason="disabled"`.
   2. If `user and user.has_permission("bypass_geofence")` → `allowed=True, reason="bypass"`, NO cache write.
   3. **Zero-cost no-op**: if `system_rules == {} and group_rules == {}` → `allowed=True, reason="no_rules"`, NO geoip lookup, NO cache write.
   4. Cache lookup `geofence:dec:{ip}:{group_id_or_'_'}`. Hit → return.
   5. Resolve geo: `GEOFENCE_TEST_OVERRIDE` if set, else `GeoLocatedIP.geolocate(ip)`.
   6. Geo failure → `allowed=(not GEOFENCE_FAIL_CLOSED)`, `reason="lookup_failed"`. Cache.
   7. Private/reserved IP (no country_code) → `allowed=GEOFENCE_ALLOW_PRIVATE_IPS` (default True), `reason="private_ip"`. Cache.
   8. Evaluate system rules → blocked → `rule_level="system"`. No group eval. Cache.
   9. Evaluate group rules → blocked → `rule_level="group"`. Cache.
   10. Else `allowed=True, reason="passed"`. Cache.
9. `mojo/apps/account/services/geofence/cache.py` — thin wrapper around `mojo.helpers.redis.client.get_connection()` for set/get/delete with TTL. Keeps engine.py readable.

**Decorator:**
10. `mojo/decorators/geofence.py` (new) — `@requires_geofence(scope=None)`. Pulls `request.group` (set by dispatcher from `?group=<id>` or `?group_uuid=<uuid>` middleware extension — see step 11). Calls `GeoFenceEngine.check(request, group, request.user)`. On block: returns `JsonResponse({"error":"geofence_blocked","code":403,"reason":r,"detail":d}, status=403)` — **omits `country`/`region`/`abuse` from the body** (info-leak guard). Records `scope` in `SECURITY_REGISTRY` for audit. Always passes through if engine returns `allowed=True`.
11. `mojo/decorators/http.py` — extend the dispatcher's group-lookup at line ~74 to ALSO accept `?group_uuid=<uuid>` (lookup by `Group.uuid`) when `group` (integer-id) is absent. Keeps the existing int-id path. Lets `/auth/oauth/<provider>/begin?group_uuid=<uuid>` populate `request.group` for the decorator.
12. `mojo/decorators/__init__.py` — re-export `requires_geofence` so callers do `md.requires_geofence`.

**Apply decorator to all built-in auth endpoints (JSON-returning only):**
13. `mojo/apps/account/rest/user.py` — decorate:
    - `on_register`, `on_user_login`, `on_user_password_reset_code`, `on_user_password_reset_token`,
      `on_magic_login_send`, `on_magic_login_complete`, `on_email_verify_send`, `on_email_verify`,
      `on_invite_accept`, `on_auth_handoff`, `on_auth_exchange`.
    - Skip `on_email_change_confirm_get` (HTML render).
14. `mojo/apps/account/rest/oauth.py` — decorate `on_oauth_begin`, `on_oauth_complete`. **Skip `on_oauth_callback`** (returns `HttpResponseRedirect` to bounce browser; a 403 JSON there would break the round-trip mid-flight and leave the user on a raw error page). The block at `/complete` catches the same outcome before user creation.
15. `mojo/apps/account/rest/totp.py` — decorate `on_totp_login`, `on_totp_verify`, `on_totp_recover`.
16. `mojo/apps/account/rest/passkeys.py` — decorate `on_passkeys_login_begin`, `on_passkeys_login_complete`.
17. `mojo/apps/account/rest/sms.py` — decorate `on_sms_login`, `on_sms_verify`.

**Pre-flight endpoint:**
18. `mojo/apps/account/rest/geofence.py` (new) — `GET /api/geo/check?group_uuid=<uuid>`. `@public_endpoint("Geofence pre-flight for UI")` + `@rate_limit("geo_check", ip_limit=30)`. Returns full `GeoDecision` (this IS the per-user pre-flight; full disclosure here is by design, since the user already knows their own IP/region). Unknown `group_uuid` → 400. Inactive group → evaluate as system-only with `reason="group_inactive"` detail. Endpoint itself is NOT decorated with `@requires_geofence` (otherwise blocked users could never see *why* they're blocked).

### Hook Signatures
```python
# engine
GeoFenceEngine.check(request, group=None, user=None) -> GeoDecision
# GeoDecision is an objict with the documented shape (allowed, reason, detail, ip,
# country, country_code, region, region_code, abuse{}, checked_at, rule_level)

# decorator
@md.requires_geofence(scope=None)
def my_view(request): ...

# DSL
geofence.evaluate_rule(rule_dict, geo) -> (allowed: bool, reason: str|None, level_field: str|None)
geofence.validate_rule(rule_dict) -> None   # raises ValueError on malformed shape
```

### Settings (inline defaults at use site, no separate file)
- `GEOFENCE_ENABLED` (bool, default `True`) — master kill.
- `GEOFENCE_SYSTEM_RULES` (dict, default `{}`) — hard-floor system rules.
- `GEOFENCE_CACHE_TTL` (int, default `300`) — Redis TTL.
- `GEOFENCE_FAIL_CLOSED` (bool, default `False`) — when geoip lookup fails, deny.
- `GEOFENCE_ALLOW_PRIVATE_IPS` (bool, default `True`) — let dev/internal IPs through.
- `GEOFENCE_TEST_OVERRIDE` (dict | None, default `None`) — substitute geo dict for tests/local dev.

### Permission
- `bypass_geofence` — added to documented permission list. No schema change (permissions are JSON keys on User.permissions and GroupMember.permissions).

### Design Decisions
- **Decorator-only enforcement**: explicit, discoverable, applies cleanly to OAuth views the same as anything else. No middleware path-matching, no extension-hook conflation.
- **Default no-op when no rules configured**: zero performance impact for apps that don't use geofence. Engine short-circuits before geoip lookup.
- **`USER_*_HANDLER` hooks unchanged**: orthogonal concern. Geofence has no business with consumer extension hooks. Doc clearly separates the two.
- **Region code plumbed from providers**: matches the spec'd DSL (`US-FL`) by exposing `region_code` from MaxMind/ipinfo instead of relying on full-name strings. New nullable field on `GeoLocatedIP`; backfill happens lazily via `refresh()`.
- **403 body omits country/region/abuse**: info-leak guard against attackers probing detection capabilities. Pre-flight `/api/geo/check` returns full decision since the user already knows their own IP.
- **OAuth callback NOT decorated**: returns `HttpResponseRedirect`; a JSON 403 there breaks the bounce-to-frontend and dumps the user on an error page. `/begin` blocks pre-consent (best UX); `/complete` blocks pre-user-creation (correctness).
- **`request.group` populated from `?group_uuid=<uuid>`**: small dispatcher extension (line 74 of `mojo/decorators/http.py`) so OAuth `/begin?group_uuid=<uuid>` and any future endpoint that takes group by UUID can rely on `request.group` being set, the same way integer-id endpoints already do.
- **Group-level rules don't apply at `/complete` and similar endpoints**: documented limitation. The `group_uuid` is buried inside the signed OAuth state and only decoded inside the view (after the decorator runs). System rules DO apply. Workaround: consumer can add a second-pass check inside the view if group-level OAuth enforcement matters to them.
- **Cache key includes group**: same IP can have different decisions for different tenants. Bypass and no-rules short-circuits write nothing to the cache.

### Use Cases
- Marketing landing page calls `GET /api/geo/check` before showing CTA — renders region-blocked messaging instead of letting the user attempt a login they can't complete.
- Tenant strict-country policy: Group X sets `metadata.geofence = {"country": {"in": ["US"]}}`. All auth endpoints reject non-US IPs that scope to Group X.
- System-wide sanctions: `GEOFENCE_SYSTEM_RULES = {"country": {"not_in": ["KP", "IR"]}}`. Hard-floor, all tenants.
- Block Tor across the platform: `GEOFENCE_SYSTEM_RULES = {"abuse": {"tor": false}}`.
- Support-staff bypass: grant `bypass_geofence` permission to support users; they can log in from anywhere to assist customers.
- Local dev: set `GEOFENCE_TEST_OVERRIDE = {"country_code": "US", "region_code": "US-CA", "is_tor": False}` in dev settings.
- Emergency: `GEOFENCE_ENABLED = False` flips everything off without losing rule config.

### Edge Cases
- **No rules at any level** → zero-cost no-op. No geoip lookup. No cache write.
- **`bypass_geofence` user** → allowed, no cache write (revoking permission re-enforces on the next request).
- **Geo lookup fails** → `allowed = not GEOFENCE_FAIL_CLOSED`, reason `lookup_failed`. Cached briefly so a flapping provider doesn't hammer auth.
- **Private/reserved IP** → controlled by `GEOFENCE_ALLOW_PRIVATE_IPS` (default True). Keeps localhost dev working.
- **Malformed rule** → `validate_rule()` raises at first evaluation; engine returns `allowed=False, reason="rule_invalid"`, logs incident. Doesn't crash auth.
- **OAuth `/callback`** → not decorated; system block still catches via `/complete` backstop.
- **OAuth `/complete` group rules** → system rules apply; group rules do not (group_uuid is inside encrypted state). Documented limitation, not a security hole (system rules are the hard floor).
- **HTML-rendering auth endpoints** (`/auth/email/change/confirm` GET, login HTML pages) → not decorated in v1. Future v2 could add `@requires_geofence_html` that renders a 403 template.
- **Unknown `group_uuid` on `/api/geo/check`** → 400.
- **Inactive group on `/api/geo/check`** → evaluate as system-only, `reason` carries `"group_inactive"` detail; don't 400.
- **Decorator on an endpoint whose request has no group context** → evaluates system rules only. Group-level rules silently inapplicable. Matches the partial-coverage trade-off above.
- **Same IP, different groups** → cache key includes group_id; each combination is one decision.
- **Cache poisoning** → cache keys are framework-generated (`geofence:dec:{ip}:{group_id}`); attacker can't influence them.

### Testing — `tests/test_geofence/` (new serial package)
Module `__init__.py` marks `"serial": True` because some tests toggle `GEOFENCE_ENABLED`, `GEOFENCE_SYSTEM_RULES`, etc. via `th.server_settings(...)`. A `_capture.py`-style helper module (underscore-prefixed so testit skips it) provides a stable `GEOFENCE_TEST_OVERRIDE` for offline geoip simulation.

**DSL (`dsl.py`):**
- Each matcher operator (`in`, `not_in`, `eq`) returns correct truth for country / region / boolean abuse fields.
- Empty rule `{}` → allowed.
- Malformed rule (`{"country": {"badop": ["US"]}}`, `{"unknown_top": {...}}`, `{"country": {"in": "US"}}` — non-list) → `validate_rule` raises with clear message.

**Engine (`engine.py`):**
- **No-op fast path**: empty system + empty group → `allowed=True, reason="no_rules"`, geoip mock asserted NOT called.
- System-only allow / block per country (with override-supplied geo).
- Group-only allow / block (system empty).
- Composition: system passes, group blocks → `rule_level="group"`. System blocks → group not evaluated, `rule_level="system"`.
- Abuse flags: each of tor/vpn/datacenter/proxy independently blocked when rule says `false` and geo flag is `True`.
- Lookup failure: fails open by default; `GEOFENCE_FAIL_CLOSED=True` → fails closed.
- Cache hit: two consecutive `check(ip, group)` calls → one geo lookup (mock or override counter). Different group → second lookup (intentional).
- `bypass_geofence` permission → `allowed=True, reason="bypass"`, cache NOT written (verify by revoking permission and re-checking — second call hits the rules).
- `GEOFENCE_ENABLED=False` → always allowed regardless of rules.
- `GEOFENCE_TEST_OVERRIDE = {...}` → uses the override, geoip mock not called.
- Private IP (`127.0.0.1` or `192.168.x.x`) → allowed by default; `GEOFENCE_ALLOW_PRIVATE_IPS=False` → falls through to rules and likely blocks (no country_code to match against).

**Decorator (`mojo/decorators/geofence.py`):**
- Blocked endpoint returns 403 with body containing `{reason, detail}` ONLY; assert `country`, `region`, `abuse` are NOT in the response body (info-leak regression guard).
- Allowed endpoint passes through normally.
- User with `bypass_geofence` always passes through.
- Decorator with `scope="auth"` records scope in SECURITY_REGISTRY (verify via `registry.get(...)`).

**Pre-flight endpoint (`/api/geo/check`):**
- Happy path (no group): returns full decision shape, `allowed=True` (with no rules) or matching block.
- With valid `?group_uuid=<uuid>`: returns full decision evaluated against that group.
- Unknown `group_uuid` → 400.
- Inactive group → 200, `reason` reflects system-only fallback, body carries `"group_inactive"` detail.
- Endpoint itself is reachable even when system rules would block (verify by setting `GEOFENCE_SYSTEM_RULES={"country":{"in":["US"]}}` + `GEOFENCE_TEST_OVERRIDE={"country_code":"RU"}` and confirming `/api/geo/check` still returns 200 with `allowed=False` in the body).
- Rate limit: 31st call from same IP within window → 429.

**OAuth integration (specifically):**
- `GET /api/auth/oauth/google/begin?group_uuid=<blocked-by-group-rules>` → 403 with reason+detail body (no full decision).
- `POST /api/auth/oauth/google/complete` with state carrying group_uuid → system rules ARE evaluated (verify a system-block reaches 403); group rules are NOT evaluated at `/complete` (documented limitation — assert via a system-allows-but-group-blocks scenario reaching the view).
- `/callback` is NOT decorated → verify a 302 redirect still happens even with rules that would block (the block has to come at `/complete` or `/begin`).

**Auth-endpoint coverage spot-checks:**
- One test per family: `/auth/login`, `/auth/register`, `/auth/magic/send`, `/auth/totp/login`, `/auth/passkeys/login/begin`, `/auth/sms/login` — confirm each returns 403 when system rules block.

**Region code plumbing:**
- Provider mock returns `region_code="US-FL"`; engine matches `{"region": {"in": ["US-FL"]}}`. Confirms the spec'd DSL works end-to-end.

### Migration
- One migration adds `region_code` to `GeoLocatedIP`. Backfill is `null`; populated lazily on `refresh()`. Run `bin/create_testproject` to regenerate test project migrations.

### Docs
- `docs/django_developer/account/geofence.md` (new) — rule DSL, decision shape, decorator usage, settings reference, `bypass_geofence` permission, OAuth-`/complete` group-rule limitation, the relationship to `USER_*_HANDLER` ("they don't talk to each other; geofence is enforced at the HTTP layer").
- `docs/django_developer/account/geoip.md` — note new `region_code` field on `GeoLocatedIP`.
- `docs/django_developer/account/README.md` — link to `geofence.md`.
- `docs/web_developer/account/` — document `GET /api/geo/check` (request format, response shape, when to call it as pre-flight).
- `CHANGELOG.md` — entry under unreleased.

### Out of Scope (carried + reaffirmed)
- Bouncer pre-screen integration (raise risk score / serve decoy on geofence block). Documented follow-up.
- Admin UI / CRUD model for rules. v1 uses settings + `Group.metadata`.
- Time-window rules.
- IP-allowlist support (use `bypass_geofence` permission as a workaround).
- HTML-rendering 403 page (`@requires_geofence_html` variant).
- Reverse geofencing (lat/long proximity).
- Mobile-network-vs-residential differentiation in the DSL.
- Non-HTTP enforcement helper (was `enforce_or_raise`); dropped from v1 because the USER_*_HANDLER integration story it served is itself out of scope.
