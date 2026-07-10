# Geofence Admin — REST API Reference

The geofence **config plane**: editable system rules, IP allowlist, what-if
simulation, and exemption audit endpoints — the backend for an admin-portal
geofencing section maintained by legal/business staff. Also the **member
plane**: `GET /api/geo/policy` (below), the group-scoped policy read for a
brand's own admin. For the public pre-flight check (`GET /api/geo/check`)
see [GeoIP & Geofencing](geoip.md); for rule semantics see the
django-developer [Geofencing reference](../../django_developer/account/geofence.md).

## Permissions — Two Audiences

**Platform staff (global grants)** — the config plane:

| Permission | Grants |
|---|---|
| `view_geofence` | Read endpoints (`GET geo/rules`, `GET geo/allowlist`, `GET geo/bypass_holders`, `POST geo/simulate`) |
| `manage_geofence` | Everything, including writes |
| `security` | Domain category — grants all of the above |

Config-plane permissions must be **global user grants** — group-scoped
(member) permissions do NOT apply, because these endpoints manage
platform-wide config; passing a `group` param does not change that. Writes
are recorded as `geofence_config` incident events (queryable via
`GET /api/incident/event?category=geofence_config`, needs global
`view_security`) — that stream is the change history.

**Brand admins (member grants)** — a `view_security`/`security` permission
granted on a GroupMember (assignable by that group's admin) opens exactly
two group-scoped reads, both confined to the member's own group (a grant on
a parent group extends to its child groups):

- `GET /api/geo/policy` — that group's effective policy, deliberately narrowed
- `GET /api/incident/event?category=geofence_block|geofence_exempt` — that
  group's enforcement events (see "Member event feed" below)

Everything else returns `403` without a matching permission.

---

## `GET /api/geo/policy` — Member-Readable Effective Policy

The group-scoped read for a brand's own admin: the geofencing policy that
actually applies to **their** group — nothing platform-wide.

Query params: `group_uuid` (or numeric `group`) — **required**, and must be
a group the caller holds a member `view_security`/`security` grant in
(global holders may read any group). Without a group param members get
`403` and global holders get `400`. An inactive group's uuid or numeric id
does not resolve here — inspecting inactive groups stays a `GET geo/rules`
(admin) affordance.

```json
{
  "status": true,
  "data": {
    "group": {"id": 4, "uuid": "…", "name": "Acme", "is_active": true},
    "enabled": true,
    "evaluation_order": ["system", "group"],
    "system_rule": {"country": {"in": ["US", "CA"]}},
    "group_rule": {"country": {"in": ["US"]}},
    "strict_posture": null,
    "strict_posture_effective": false
  }
}
```

`system_rule` is the platform baseline that applies to all traffic including
this group's; `group_rule` is the group's own rule (evaluated after the
baseline — either can block). `strict_posture` is the group's raw tri-state
override (`null` = inherit) and `strict_posture_effective` the resolved
outcome; **changing** the override still requires global `manage_geofence`.

The payload is **deliberately narrower** than `GET geo/rules`: no
`enforced_endpoints`, no `allowlist_summary`, no cache TTL / fail-closed
scopes / config provenance — operational detail stays platform-staff only,
and new `geo/rules` fields do not automatically appear here.

### Member event feed

The same member grant scopes `GET /api/incident/event` to the member's own
group, so a brand dashboard can list its enforcement history:

```
GET /api/incident/event?category=geofence_block
GET /api/incident/event?category=geofence_exempt
```

Only **group-attributed** events appear: an enforcement event carries a
group when the blocked/exempted request itself supplied `group`/`group_uuid`
(e.g. white-label auth pages). Requests without group context are recorded
globally and are not member-visible — read the feed as reported activity on
your auth surface, not a verified total (same caveat as the per-group
metrics below). `geofence_config` events are platform config history and
effectively always global-only.

---

## `GET /api/geo/rules` — Effective Configuration

The machine-readable "rules in an active state" artifact.

Query params: `group_uuid` (optional) — include that group's rule.

```json
{
  "status": true,
  "data": {
    "system": {"rule": {"country": {"in": ["US", "CA"]}}, "source": "setting",
               "modified": "2026-07-08T14:11:02+00:00"},
    "posture": {"enabled": true, "fail_closed": false,
                "fail_closed_scopes": ["payments"],
                "allow_private_ips": true, "strict_posture": false,
                "cache_ttl": 300},
    "allowlist_summary": {"setting_entries": 2, "geoip_active": 3},
    "evaluation_order": ["system", "group"],
    "enforced_endpoints": [
      {"endpoint": "mojo.apps.account.rest.user.on_user_login", "scope": "auth"}
    ],
    "group": {"id": 4, "uuid": "…", "is_active": true,
              "rule": {"country": {"in": ["US"]}},
              "strict_posture": null,
              "strict_posture_effective": false}
  }
}
```

`system.source` is `"setting"` (DB row, editable), `"conf"` (deploy file), or
`"none"`.

`posture.strict_posture` is the global compliance switch (fail-closed on
lookup failure + deny private IPs + deny when no rules are configured).
`group.strict_posture` is that group's raw override — `null` inherits the
global, `true`/`false` overrides it — and `group.strict_posture_effective` is
the resolved outcome. Set the override by writing the group's metadata:
`POST /api/group/<pk>` with `{"metadata": {"geofence_strict": true}}`
(non-boolean values are rejected 400; write `null` to go back to inherit).
Changing it requires the **global** `manage_geofence` (or `security`)
permission — group-scoped admin rights return 403 — and every change is
recorded as a `geofence_config` incident event.
Requests denied because a strict deployment has no rules configured return
the 403 reason `no_rules_strict`.

## `POST /api/geo/rules` — Replace System Rules

Body: `{"rule": {…}}` — the full rule object (replace, never merge). Invalid
rules are rejected with a readable message:

```json
{"error": "geofence rule: 'country' has unknown operator 'bogus'; valid operators are ['eq', 'in', 'not_in']", "code": 400, "status": false}
```

On success the rule is live immediately (cached decisions are invalidated
automatically). Response: `{"rule": …, "source": "setting", "modified": …}`.

## `DELETE /api/geo/rules`

Removes the DB override; the engine falls back to the deploy-file value (or no
rules). Response: `{"removed": true}`.

---

## `POST /api/geo/simulate` — What-If Decision

Demonstrate "a WA IP is blocked" without owning a WA IP. Evaluates uncached,
never emits evidence events, and works even while `GEOFENCE_ENABLED` is off
(the response carries `enabled` so you can stage rules before enabling).

Body — one of `ip` or `geo`, plus options:

| Field | Description |
|---|---|
| `ip` | IP address to resolve and evaluate (also consults the allowlist) |
| `geo` | Geo dict instead of an IP: `{"country_code": "US", "region_code": "US-WA", "is_tor": false, …}` |
| `group_uuid` | Optional — include that group's rules |
| `scope` | Optional — preview fail posture for a decorator scope |

Response is a full `GeoDecision` (all fields — this surface is perm-gated, so
nothing is withheld). An allowlisted IP returns `reason: "ip_allowlisted"`
with `allowlist_source`, `allowlist_reason`, and the shadow outcome
`would_block` / `would_block_reason`.

---

## `GET /api/geo/allowlist` — Active IP Exemptions

The auditor's "who is exempt" list (IP/CIDR side — user grants are
`bypass_holders`). Expired entries are listed with `active: false`, not
hidden.

```json
{
  "status": true,
  "data": {
    "setting": [
      {"cidr": "198.51.100.0/24", "reason": "office egress", "until": null, "active": true}
    ],
    "geoip": [
      {"ip": "203.0.113.7", "reason": "dev box", "until": "2026-08-01T00:00:00+00:00", "active": true}
    ]
  }
}
```

## `POST /api/geo/allowlist` — Replace the CIDR Allowlist

Body: `{"entries": […]}` — full replace; an empty list clears it. Entries are
`"CIDR-or-IP"` strings or `{"cidr": "…", "reason": "…", "until": "<ISO>"}`.
Each entry is validated (parseable CIDR/IP, string reason, parseable `until`)
— the first bad entry is named in the 400.

Allowlisted IPs pass **all** geofence rules (jurisdiction *and* abuse flags).
When an exemption actually bypasses a block, a `geofence_exempt` incident
event records it. Changes invalidate cached decisions immediately.

Per-IP entries are managed on the existing GeoIP admin surface instead:
`POST /api/system/geoip/<pk>` with
`{"whitelist": {"reason": "…", "ttl": 3600}}` or
`{"whitelist": {"reason": "…", "until": "<ISO>"}}` (bare-string reason still
works; `{"unwhitelist": 1}` removes it). See [GeoIP](geoip.md).

---

## `GET /api/geo/bypass_holders` — User Exemptions

Users who bypass geofencing entirely: explicit `bypass_geofence` permission
grants plus superusers (who hold every permission implicitly).

```json
{
  "status": true,
  "data": {
    "holders": [
      {"id": 12, "username": "dev@example.com", "is_active": true,
       "source": "permission", "value": true}
    ],
    "count": 1,
    "capped": false
  }
}
```

`source` is `"permission"` or `"superuser"`. The list is capped at 200 rows
(`capped: true` when truncated). The response deliberately carries
id/username only — `email`/`display_name` are "users"-category data and are
not exposed through a geofence-only permission.

---

## Metrics

Geofence enforcement records aggregate counters (category `geofence`,
counted for **every** occurrence — the hourly event dedupe does not apply to
metrics):

| Slug | Counts | Accounts |
|---|---|---|
| `geofence:blocks` | every blocked request | `global` + `group-<id>` |
| `geofence:blocks:country:{CC}` | blocks by country | `global` only |
| `geofence:blocks:region:{ISO-3166-2}` | blocks by region | `global` only |
| `geofence:exempt` | allowlisted passes that would have blocked | `global` + `group-<id>` |

When the blocked/exempted request carries a group (`group` / `group_uuid`
param — e.g. white-label auth pages), the base slugs are also recorded under
that group's metrics account, so a tenant dashboard can chart its own
geofence activity:

```
GET /api/metrics/fetch?slug=geofence:blocks&account=group-42&granularity=days&with_labels=true
```

Reading a `group-<id>` account requires `view_metrics`/`metrics` — a
group-member grant is enough (see [Metrics](../metrics/metrics.md)). The
country/region breakdown is deliberately global-only (a per-group geographic
cross-product would explode the Redis key space); chart those from the
`global` account, which needs a global metrics grant.

Attribution comes from the `group`/`group_uuid` param the blocked/exempted
request itself supplied — on a public auth surface there is no membership
check on that param — so read a group's counters as **reported activity on
that tenant's auth surface**, not a verified count. Reads stay
permission-gated regardless.
