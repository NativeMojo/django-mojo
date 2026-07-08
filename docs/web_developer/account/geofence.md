# Geofence Admin — REST API Reference

The geofence **config plane**: editable system rules, IP allowlist, what-if
simulation, and exemption audit endpoints — the backend for an admin-portal
geofencing section maintained by legal/business staff. For the public
pre-flight check (`GET /api/geo/check`) see [GeoIP & Geofencing](geoip.md);
for rule semantics see the django-developer
[Geofencing reference](../../django_developer/account/geofence.md).

## Permissions

| Permission | Grants |
|---|---|
| `view_geofence` | Read endpoints (`GET geo/rules`, `GET geo/allowlist`, `GET geo/bypass_holders`, `POST geo/simulate`) |
| `manage_geofence` | Everything, including writes |
| `security` | Domain category — grants all of the above |

All endpoints return `403` without a matching permission. Writes are recorded
as `geofence_config` incident events (queryable via
`GET /api/incident/event?category=geofence_config`, needs `view_security`) —
that stream is the change history.

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
                "allow_private_ips": true, "cache_ttl": 300},
    "allowlist_summary": {"setting_entries": 2, "geoip_active": 3},
    "evaluation_order": ["system", "group"],
    "enforced_endpoints": [
      {"endpoint": "mojo.apps.account.rest.user.on_user_login", "scope": "auth"}
    ],
    "group": {"id": 4, "uuid": "…", "is_active": true,
              "rule": {"country": {"in": ["US"]}}}
  }
}
```

`system.source` is `"setting"` (DB row, editable), `"conf"` (deploy file), or
`"none"`.

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
      {"id": 12, "username": "dev@example.com", "display_name": "Dev",
       "email": "dev@example.com", "is_active": true,
       "source": "permission", "value": true}
    ],
    "count": 1,
    "capped": false
  }
}
```

`source` is `"permission"` or `"superuser"`. The list is capped at 200 rows
(`capped: true` when truncated).
