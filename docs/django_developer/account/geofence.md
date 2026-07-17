# Geofencing — Django Developer Reference

Policy-based geographic access control. Rules are evaluated at the HTTP layer on every decorated endpoint and never reach downstream handlers if a request is blocked.

---

## Rule DSL

Rules are JSON objects with up to three top-level keys. An empty object `{}` is always a no-op (allow everything).

### `country`

```json
{"country": {"in": ["US", "CA"]}}
{"country": {"not_in": ["CN", "RU"]}}
{"country": {"eq": "US"}}
```

Values are ISO 3166-1 alpha-2 codes. `in` / `not_in` take arrays; `eq` takes a string.

### `region`

Same operators as `country`. Values are ISO 3166-2 subdivision codes.

```json
{"region": {"not_in": ["US-FL", "US-TX"]}}
```

### `abuse`

Controls blocking based on connection-type flags (`tor`, `vpn`, `datacenter`, `proxy`).

- `false` — block when the flag is `True` on the IP record (e.g. block Tor users)
- `true` — block when the flag is `False` (unusual; use with care)
- `null` or absent — don't check this flag

```json
{"abuse": {"tor": false, "vpn": false}}
```

### Combined rules

All top-level keys must pass (AND logic).

```json
{
  "country": {"in": ["US", "CA", "GB"]},
  "abuse": {"tor": false, "datacenter": false}
}
```

---

## Two Rule Levels

Both levels must pass for a request to proceed (AND-ed). System rules are evaluated first.

### System rules — `GEOFENCE_SYSTEM_RULES`

Platform-wide hard floor. Applies to every request regardless of which group the user belongs to. Two sources, DB winning over file:

1. **DB-backed `Setting` row** (key `GEOFENCE_SYSTEM_RULES`, global scope) — the
   editable config plane. Managed through `POST /api/geo/rules` (validated,
   attributed, cache-invalidating — see *Config Plane* below) or the generic
   `/api/settings` REST (also validated via `Setting.on_rest_pre_save`).
2. **Django settings file** — the deploy-time fallback:

```python
GEOFENCE_SYSTEM_RULES = {
    "country": {"in": ["US", "CA"]},
    "abuse": {"tor": false}
}
```

The engine reads the setting with `kind="dict"`, so the DB-stored JSON string
parses transparently.

### Group rules — `Group.metadata['geofence']`

Per-tenant override stored in the group's metadata JSON. Evaluated after system rules pass.

```python
group.metadata['geofence'] = {
    "country": {"in": ["US"]},
    "abuse": {"vpn": false}
}
group.save()
```

REST writes to `metadata.geofence` are **validated at save time**
(`Group.on_rest_pre_save` runs `validate_rule` on the merged metadata and
returns a readable 400 — a typo'd rule can no longer lie in wait as a
request-time `rule_invalid` deny). Note REST JSON fields **merge** by default,
recursively — posting `{"metadata": {"geofence": {...}}}` merges into the
existing rule rather than replacing it. To drop a key (or the whole rule),
merge a `null`: e.g. `{"metadata": {"geofence": null}}`, then post the new
rule. Every group save also invalidates that group's cached decisions.

`__replace` is only honored at the **top level** of the JSONField value —
nested inside `geofence` it is not interpreted (it would be stored literally).
A top-level `{"metadata": {"__replace": true, ...}}` does replace wholesale,
but it replaces the **entire** `metadata` value, and `geofence` and
`protected` live in the same JSONField: if the group has a `protected`
subtree, the replace is treated as touching it and requires
`PROTECTED_JSON_PERMS` (see [Protected Metadata](group.md#protected-metadata))
even though the payload only mentions `geofence`. Callers with only
geofence-management perms should use merge + `null` deletion instead.

---

## Strict / Compliance Posture (opt-in)

The engine defaults **fail-open** (a resilience choice). Deployments that
require geofencing for jurisdictional compliance — where "can't verify
location" must mean deny, not allow — opt into the strict posture. One bundled
switch that, when on:

1. **Fails closed on lookup failure** — composes with the existing flags:
   `fail_closed = GEOFENCE_FAIL_CLOSED OR scope ∈ GEOFENCE_FAIL_CLOSED_SCOPES OR strict`
2. **Denies private/reserved IPs** (`allow_private = GEOFENCE_ALLOW_PRIVATE_IPS AND NOT strict`)
3. **Denies when geofencing has no rules configured** — reason
   `no_rules_strict`; no silent allow-all on an enabled-but-unconfigured
   deployment.

Strict only ever **tightens**; it never loosens the granular flags, and with
it off (the default) behavior is bit-for-bit unchanged.

**Global**: `GEOFENCE_STRICT_POSTURE` (bool Setting, default `False`,
global-only — group-scoped rows are rejected 400; writes via `/api/settings`
must be a JSON boolean, anything else is rejected at write time so a posture
flag can never be ambiguous — see *Settings Reference* below for the full
write-validation contract).

**Per-group override**: `Group.metadata["geofence_strict"]` — tri-state:
absent/`null` inherits the global, `true`/`false` overrides in either
direction (some groups strict, others permissive). Validated on REST write
(non-boolean → 400); a flat sibling of `metadata["geofence"]` (which remains
the raw rule dict). **Changing it requires the global `manage_geofence` (or
`security`) permission** — the same trust level as the global switch; a
tenant admin who can merely edit the group gets a 403 and cannot opt their
group out of a platform-mandated posture. Every flip is recorded as a
`geofence_config` incident event (`target: "group:<id>"`, old/new/actor).

The **IP allowlist still wins** under strict (bypass → allowlist → rules): an
allowlisted office IP gets in even on a strict no-rules deployment, with the
exemption evidence recording `would_block_reason: "no_rules_strict"`. Blocks
under strict posture are compliance-grade — evidence level **5**. Posture
flips invalidate cached decisions automatically (Setting hook / group save
hook); strict `lookup_failed` denials are never cached, so a transient
provider outage recovers on the next successful lookup.

Test header (test-mode gate only): `X-Mojo-Test-Geofence-Strict: 0|1`.

---

## GeoDecision Shape

The result of a geofence evaluation is a `GeoDecision` dict:

| Field | Type | Description |
|---|---|---|
| `allowed` | bool | `True` if all rules passed |
| `reason` | str | One of: `no_rules`, `no_rules_strict`, `disabled`, `bypass`, `ip_allowlisted`, `passed`, `lookup_failed`, `private_ip`, `country_not_allowed`, `region_not_allowed`, `tor_detected`, `vpn_detected`, `proxy_detected`, `datacenter_detected`, `rule_invalid`, `group_inactive` |
| `detail` | str | Human-readable explanation |
| `ip` | str | Evaluated IP address |
| `country` / `country_code` | str | ISO 3166-1 alpha-2 code |
| `region` / `region_code` | str | ISO 3166-2 code |
| `abuse` | dict | `{tor, vpn, datacenter, proxy}` booleans |
| `checked_at` | str | ISO 8601 timestamp |
| `rule_level` | str | `"system"` or `"group"` — which level caused a block |
| `strict_posture` | bool | `True` when the decision was evaluated under strict posture (see below) |

`ip_allowlisted` decisions additionally carry `allowlist_source`
(`"setting"` or `"geoip"`), `allowlist_reason`, `allowlist_until`, and the
shadow-evaluation outcome `would_block` / `would_block_reason` (what the rules
would have decided without the exemption). `simulate()` results also carry
`enabled`.

The full `GeoDecision` is returned by the pre-flight endpoint. Blocked HTTP responses omit geo/abuse fields intentionally (info-leak guard).

---

## Decorator

`@md.requires_geofence(scope="auth")` is applied to every built-in auth endpoint. Use it on your own views the same way:

```python
@md.URL('myapp/protected')
@md.requires_geofence(scope="auth")
@md.requires_auth()
def on_protected(request):
    ...
```

The `scope` string is recorded in the security registry AND drives fail
posture: scopes listed in `GEOFENCE_FAIL_CLOSED_SCOPES` fail **closed** on
geo-lookup failure (use for money/payment endpoints), while everything else
keeps the fail-open default. Because posture is scope-sensitive,
`lookup_failed` decisions are never cached.

Registry entries merge across stacked decorators, so
`GET /api/geo/rules` → `enforced_endpoints` lists every
`@requires_geofence` endpoint regardless of which other security decorators
(`@public_endpoint`, `@requires_auth`, ...) sit above or below it (DM-044).

### Post-credential enforcement (`after_auth=True`)

Identity-bearing auth endpoints — everything that verifies a credential
mid-flow and issues a JWT (password login, TOTP/SMS MFA finish and standalone
logins, passkey complete, OAuth complete, handoff exchange, magic link, email
verify, invite accept, password reset) — carry
`@md.requires_geofence(scope="auth", after_auth=True)`. The deferred mode
registers the endpoint in the security registry — it still appears in
`GET /api/geo/rules` → `enforced_endpoints` with its `scope`, annotated
`after_auth: true` so an auditor can distinguish pre-view from
post-credential enforcement — but does **not** block pre-view. Enforcement
instead runs **after credential verification** with the verified user, via
the shared
`services.geofence.enforcement.enforce(request, scope, user)` routine at two
points:

1. **The top of `jwt_login()`** — before `last_login`, the `UserLoginEvent`,
   and `USER_LOGIN_HANDLER`, so a blocked login records **zero** success side
   effects. Every issuance flow funnels through here.
2. **The MFA branch of the password login** — a blocked user is denied before
   the challenge and never receives an `mfa_token`.

Why: the engine's `bypass_geofence` short-circuit needs an identified user, and
block evidence should name who was blocked. The ordering contract is: invalid
credentials → the normal 401 (a blocked geo never changes it); valid
credentials → the standard geofence 403 (body shape unchanged).

Consequences to know:

- **Exempt sources** — `jwt_login` skips the check for
  `source in GEOFENCE_EXEMPT_JWT_SOURCES` (`"sessions_revoke"`,
  `"email_change"`): authed re-issues of an existing session, not logins — a
  user in a blocked geo must still be able to revoke their own sessions. Every
  other source (including new ones you add) is geofenced by default.
- **Token-proven actions complete before the session is withheld** — a
  password reset / email verify / invite accept from a blocked geo applies its
  mutation (the emailed secret proved it), then returns the geofence 403
  instead of tokens.
- **Accepted tradeoff**: a caller in a blocked geo holding valid stolen
  credentials can distinguish 403 (valid, geo-blocked) from 401 (invalid).
  Geofencing is not a credential-testing defense; bouncer + rate limits still
  run first.

Identity-less auth endpoints (register, forgot-password, magic-link/OTP sends,
passkey/OAuth begin, phone-register) keep the default pre-view blocking mode —
register in particular must block **before** account creation.

When a request is blocked the decorator returns 403 immediately with only:

```json
{
  "error": "geofence_blocked",
  "code": 403,
  "reason": "country_not_allowed",
  "detail": "Service is not available in your country."
}
```

Country, region, and abuse details are intentionally omitted from the blocked response to prevent information leakage. Every block is also recorded by the evidence plane (below).

### OAuth `/callback` is not decorated

The OAuth `/callback` endpoint returns an HTTP redirect, not JSON, so `@md.requires_geofence` is not applied there. `/begin` is geofenced pre-view; `/complete` uses `after_auth=True` and enforces **immediately after the provider exchange proves the identity — before `_find_or_create_user`** — so a blocked-geo caller can never provision a new account, join a group, fire the registration webhook, or persist provider tokens (existing users resolve lookup-only so `bypass_geofence` is honored; unknown identities enforce anonymously). `jwt_login` re-checks as the backstop. Group rules do not apply at `/complete` because the `group_uuid` is encoded inside the signed OAuth state string and is not decoded until inside the view — so only system rules apply there.

---

## `bypass_geofence` Permission

A user with the `bypass_geofence` permission short-circuits all geofence checks entirely. The check returns `allowed=True` immediately without writing a cache entry, so revoking the permission takes effect on the very next request.

**It works at login too**: identity-bearing auth endpoints enforce geofencing
after credential verification (see *Post-credential enforcement* above), so a
whitelisted user completes a fresh login from a blocked geo — the per-user
whitelist covers the full session lifecycle, not just already-authenticated
traffic.

**This is a high-privilege grant** — superusers hold it implicitly
(`User.has_permission` returns `True` for a superuser on every perm).
`GET /api/geo/bypass_holders` lists everyone who is exempt this way (explicit
grants plus superusers) for audit.

---

## IP Allowlist — Full Exemption

Developer/office IPs can be exempted from geofencing entirely (jurisdiction
**and** abuse flags — a developer on a VPN is not re-blocked as
`vpn_detected`). Decision priority: **bypass → allowlist → rules**. Two
sources, checked in order:

1. **`GEOFENCE_ALLOWLIST` setting** — a list of CIDR entries for office/VPN
   egress ranges. Entries are `"CIDR-or-IP"` strings or
   `{"cidr": ..., "reason": ..., "until": ...}` objects. Managed via
   `GET/POST /api/geo/allowlist` (validated: parseable CIDR, ISO `until`).
2. **Per-IP `GeoLocatedIP.is_whitelisted`** — the existing firewall whitelist,
   managed via the `/api/system/geoip` `whitelist` / `unwhitelist` actions.
   The action accepts a dict `{"reason": ..., "ttl": <seconds>, "until": <iso>}`
   (bare-string reason still works).

**Expiry**: setting entries carry `until`; `GeoLocatedIP` rows carry
`whitelisted_until` (mirrors `blocked_until`; `null` = permanent). Expiry is
evaluated lazily via the `whitelist_active` property — expired entries stop
matching and are listed with `active: false`, and an expired whitelist also
stops suppressing incident-driven firewall blocking (`block_active`,
`block()`, `update_threat_from_incident` all honor it).

**Compliance guardrails**: an allowlisted request still shadow-evaluates the
rules; when it *would* have blocked, the pass is recorded as a
`geofence_exempt` incident event with the would-block reason. Allowlist
changes (either source) invalidate the decision cache automatically and land
in the `geofence_config` event stream. Whitelist state never federates
(`/api/system/geoip/sync` rejects it).

---

## Config Plane — REST + Permissions

New fine-grained perms: **`view_geofence`** (read) and **`manage_geofence`**
(write); the `security` domain-category perm grants both. Legal/business
staff get geofence management *without* `manage_settings` (which exposes every
setting including secrets).

These endpoints check **global `User.permissions` only** (no
`requires_perms`-style group fallback): a GroupMember-scoped grant — which any
group admin can assign — must never authorize platform-wide enforcement
config. Relatedly, `GEOFENCE_SYSTEM_RULES` / `GEOFENCE_ALLOWLIST` are
**global-only Setting keys**: the engine never resolves them per-group, and
group-scoped rows are rejected with a 400 at write time.

| Endpoint | Perms | Purpose |
|---|---|---|
| `GET /api/geo/rules` | view | Effective config: system rule + source (`setting`/`conf`/`none`) + modified stamp, posture (enabled, fail modes, scopes, strict posture, cache TTL), allowlist summary, evaluation order, and every `@requires_geofence` endpoint with its scope. Pass `group_uuid` to include a group's rule plus its `strict_posture` override (raw tri-state) and `strict_posture_effective` (resolved). |
| `POST /api/geo/rules` | manage | Full-replace the system rule (`{"rule": {...}}`). Validated → readable 400. Persists as the global `Setting` row. |
| `DELETE /api/geo/rules` | manage | Drop the DB override (falls back to django.conf). |
| `POST /api/geo/simulate` | view | Uncached what-if: `{"ip": ...}` or `{"geo": {...}}` + optional `group_uuid`/`scope` → full `GeoDecision` (evaluates even while `GEOFENCE_ENABLED` is off; consults the allowlist when `ip` is given; emits nothing). |
| `GET /api/geo/allowlist` | view | Active exemptions from both sources, with reason/until/active. |
| `POST /api/geo/allowlist` | manage | Full-replace the CIDR allowlist (`{"entries": [...]}`, empty list clears). |
| `GET /api/geo/bypass_holders` | view | Users exempt via `bypass_geofence` or superuser. |

**Cache coherence is automatic**: `Setting.save()`/`delete()` for the geofence
keys invalidates every cached decision; `Group` saves invalidate that group's
decisions; `GeoLocatedIP.whitelist()`/`unwhitelist()` invalidate that IP's
decisions. An emergency rule edit takes effect immediately — no ops step.

**Change history**: every config write emits a `geofence_config` incident
event carrying `target` (`system` / `allowlist` / `ip:<addr>`), `old`, `new`,
and the acting user. Query `/api/incident/event?category=geofence_config`
(needs `view_security`).

---

## Member Plane — Group-Scoped Visibility

The config plane is platform-staff only; the **member plane** is its
group-scoped, read-only counterpart for a brand's own admin. One endpoint:
`GET /api/geo/policy`, gated by `@md.requires_perms("view_security",
"security")` — a **global** grant reads any group, a **member** grant reads
the group it is granted in (a grant on a parent group also covers its child
groups — the framework's standard `check_parents` membership convention).
The decorator checks the grant against `request.group`, and the response is
built solely from `request.group`, so cross-tenant reads are structurally
impossible. The keys deliberately match
`Event.VIEW_PERMS`, so one member grant lights up both the policy read and
the group's event feed; the config-plane keys
(`view_geofence`/`manage_geofence`) remain global-only and do NOT open this
endpoint.

The payload is deliberately narrow — the policy that applies to that group's
traffic (`enabled`, `system_rule` baseline, `group_rule`, `strict_posture`
tri-state + `strict_posture_effective`, `evaluation_order`) and never the
config plane's operational detail (`enforced_endpoints`, allowlist
internals, fail-closed scopes, cache TTL, config source/modified). It reads
persisted config (settings + `Group.metadata`) like `geo/rules` does — not
the engine's test-header overlays. A `group`/`group_uuid` param is required:
members without one 403 at the decorator; global holders get a 400. The
dispatcher resolves both `group` and `group_uuid` for **effectively active**
groups only (it and every ancestor — DM-048) — inspecting an inactive group,
including an active child under a deactivated ancestor, stays a `geo/rules`
(admin) affordance.

**Events — no new mechanics.** `Event.VIEW_PERMS = ["view_security",
"security"]` plus the framework's group-scoped list fallback
(`mojo/models/rest.py::on_rest_handle_list`) already confine a member grant
to `group__in=<their groups>`, and groupless rows are excluded. Geofence
events carry a group only when the enforced request itself supplied
`group`/`group_uuid` (the incident reporter falls back to `request.group`;
attribution is client-reported — DM-020), so members see group-attributed
activity, not verified totals. `geofence_config` events are effectively
always groupless (platform config history) and stay platform-only.
Regression tests: `tests/test_geofence/member_visibility.py`.

---

## Evidence Plane — Incident Events + Metrics

Every enforcement outcome that matters to an auditor becomes an incident
Event (`mojo.apps.incident`); blocks also record metrics. Emission happens at
the **enforcement point** — the decorator for pre-view endpoints, the
post-credential check for `after_auth` endpoints (so cache hits still emit);
`/api/geo/check` and simulate are advisory and emit nothing. Post-credential
blocks additionally carry the **verified user**: `uid` on the event plus
`username` in metadata — pre-view blocks are anonymous (`uid=None`) as
before.

| Category | When | Level |
|---|---|---|
| `geofence_block` | `rule_invalid` at evaluation (broken rule denying traffic — pages via `INCIDENT_LEVEL_THRESHOLD` default 7) | 7 |
| `geofence_block` | lookup failure while failing OPEN (enforcement silently off) | 6 |
| `geofence_block` | abuse-flag block, any block on a scope in `GEOFENCE_FAIL_CLOSED_SCOPES`, or any block under strict posture | 5 |
| `geofence_block` | ordinary jurisdiction block | 3 |
| `geofence_exempt` | allowlisted pass that would otherwise have blocked | 3 |
| `geofence_config` | any rules/allowlist/whitelist change | 3 |

Block/exempt events are **deduped per `(ip, reason)` per hour** (Redis key
`geofence:evt:{ip}:{reason}`) so a blocked client hammering login cannot
flood the stream. Metrics count **every** block including deduped ones:
`geofence:blocks`, `geofence:blocks:country:{cc}`,
`geofence:blocks:region:{iso-3166-2}`, plus `geofence:exempt` — all under the
`geofence` category (mirrors `firewall:blocks`). When the request carries a
group (`request.group`, resolved from the `group`/`group_uuid` params), the
base slugs `geofence:blocks` and `geofence:exempt` are **also** recorded under
`account="group-<id>"` (the platform's per-tenant account convention, e.g.
`member_activity_day`) so tenant dashboards can chart their own blocks —
attribution is the client-supplied `group`/`group_uuid` param, so per-group
counters are reported activity, not verified counts. The
country/region breakdown slugs stay global-only — per-group geographic
accounts would cross-product groups × countries × regions in Redis, and the
monthly/yearly counter keys never expire. Escalation of level-3/5/6
events stays with incident RuleSet bundling — blocked-jurisdiction login
traffic is the system *working*, not an incident.

---

## Settings Reference

| Setting | Default | Description |
|---|---|---|
| `GEOFENCE_ENABLED` | `True` | Master kill switch. Set to `False` to disable all checks globally. |
| `GEOFENCE_SYSTEM_RULES` | `{}` | Platform-wide hard-floor rule (no-op by default). DB `Setting` row wins over the settings file. |
| `GEOFENCE_ALLOWLIST` | `[]` | CIDR exemption list (strings or `{cidr, reason, until}`) — full geofence bypass for matching IPs. |
| `GEOFENCE_CACHE_TTL` | `300` | Redis cache TTL in seconds for geo decisions. |
| `GEOFENCE_FAIL_CLOSED` | `False` | If `True`, deny access when the geoip lookup fails. If `False`, allow on failure. |
| `GEOFENCE_FAIL_CLOSED_SCOPES` | `[]` | Decorator scopes that fail CLOSED on lookup failure (e.g. `["payments"]`) while everything else stays fail-open. |
| `GEOFENCE_ALLOW_PRIVATE_IPS` | `True` | Private/reserved IPs (localhost, RFC 1918) are always allowed when `True`. |
| `GEOFENCE_STRICT_POSTURE` | `False` | Opt-in compliance posture: fail-closed on lookup failure + deny private IPs + deny when no rules are configured. Per-group override: `Group.metadata["geofence_strict"]` (tri-state). Global-only key; writes must be a JSON boolean. |
| `GEOFENCE_TEST_OVERRIDE` | `None` | Dict that substitutes the geoip lookup. Use in tests or local dev to simulate specific countries/flags. |

**All `GEOFENCE_*` keys above (except the conf-file-only
`GEOFENCE_TEST_OVERRIDE`) are write-validated, global-only DB settings**: a
malformed value is rejected on every write path (`POST /api/settings` → readable
`400`; `Setting.set()`/shell → `ValueException`), group-scoped rows are refused,
and every write invalidates the geofence decision cache (so e.g. an
`ALLOW_PRIVATE_IPS` flip cannot leave stale cached `private_ip` allows).
Booleans must be JSON `true`/`false` (an unrecognized string no longer
truthy-coerces at read time), `GEOFENCE_CACHE_TTL` a non-negative JSON integer,
`GEOFENCE_FAIL_CLOSED_SCOPES` a JSON list of non-empty strings. Validators are
registered per-key via `Setting.register_validator` — see
[settings helper](../helpers/settings.md) to register app-specific keys.

### Test override example

```python
GEOFENCE_TEST_OVERRIDE = {
    "country_code": "CN",
    "region_code": "CN-BJ",
    "tor": False,
    "vpn": False,
    "datacenter": False,
    "proxy": False,
}
```

---

## Relationship to USER_REGISTERED_HANDLER / USER_LOGIN_HANDLER

Geofencing and the extension handlers do not talk to each other. Geofencing is enforced at the HTTP layer by the decorator before the view body executes. `USER_REGISTERED_HANDLER` and `USER_LOGIN_HANDLER` are downstream workflow hooks that fire only after a request has already passed all access checks. There is no cross-notification — a geofence block never invokes those handlers, and those handlers have no ability to influence geofence decisions.

---

## Provider Integration

The geofence engine reads from `GeoLocatedIP`. A new `region_code` field was added to that model to support region-level rules. See [GeoIP](geoip.md) for the full model reference.
