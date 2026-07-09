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
request-time `rule_invalid` deny). Note REST JSON fields **merge** by default;
send `{"geofence": {..., "__replace": true}}` to replace the rule wholesale.
Every group save also invalidates that group's cached decisions.

---

## GeoDecision Shape

The result of a geofence evaluation is a `GeoDecision` dict:

| Field | Type | Description |
|---|---|---|
| `allowed` | bool | `True` if all rules passed |
| `reason` | str | One of: `no_rules`, `disabled`, `bypass`, `ip_allowlisted`, `passed`, `lookup_failed`, `private_ip`, `country_not_allowed`, `region_not_allowed`, `tor_detected`, `vpn_detected`, `proxy_detected`, `datacenter_detected`, `rule_invalid`, `group_inactive` |
| `detail` | str | Human-readable explanation |
| `ip` | str | Evaluated IP address |
| `country` / `country_code` | str | ISO 3166-1 alpha-2 code |
| `region` / `region_code` | str | ISO 3166-2 code |
| `abuse` | dict | `{tor, vpn, datacenter, proxy}` booleans |
| `checked_at` | str | ISO 8601 timestamp |
| `rule_level` | str | `"system"` or `"group"` — which level caused a block |

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

### OAuth `/complete` is not decorated

The OAuth `/callback` endpoint returns an HTTP redirect, not JSON, so `@md.requires_geofence` is not applied there. System rules still apply at other OAuth steps; group rules do not apply at `/complete` because the `group_uuid` is encoded inside the signed OAuth state string and is not decoded until inside the view — after any decorator would have run.

---

## `bypass_geofence` Permission

A user with the `bypass_geofence` permission short-circuits all geofence checks entirely. The check returns `allowed=True` immediately without writing a cache entry, so revoking the permission takes effect on the very next request.

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
| `GET /api/geo/rules` | view | Effective config: system rule + source (`setting`/`conf`/`none`) + modified stamp, posture (enabled, fail modes, scopes, cache TTL), allowlist summary, evaluation order, and every `@requires_geofence` endpoint with its scope. Pass `group_uuid` to include a group's rule. |
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

## Evidence Plane — Incident Events + Metrics

Every enforcement outcome that matters to an auditor becomes an incident
Event (`mojo.apps.incident`); blocks also record metrics. Emission happens at
the **decorator** (so cache hits still emit); `/api/geo/check` and simulate
are advisory and emit nothing.

| Category | When | Level |
|---|---|---|
| `geofence_block` | `rule_invalid` at evaluation (broken rule denying traffic — pages via `INCIDENT_LEVEL_THRESHOLD` default 7) | 7 |
| `geofence_block` | lookup failure while failing OPEN (enforcement silently off) | 6 |
| `geofence_block` | abuse-flag block, or any block on a scope in `GEOFENCE_FAIL_CLOSED_SCOPES` | 5 |
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
| `GEOFENCE_TEST_OVERRIDE` | `None` | Dict that substitutes the geoip lookup. Use in tests or local dev to simulate specific countries/flags. |

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
