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

Platform-wide hard floor. Applies to every request regardless of which group the user belongs to. Set in Django settings:

```python
GEOFENCE_SYSTEM_RULES = {
    "country": {"in": ["US", "CA"]},
    "abuse": {"tor": false}
}
```

### Group rules — `Group.metadata['geofence']`

Per-tenant override stored in the group's metadata JSON. Evaluated after system rules pass.

```python
group.metadata['geofence'] = {
    "country": {"in": ["US"]},
    "abuse": {"vpn": false}
}
group.save()
```

---

## GeoDecision Shape

The result of a geofence evaluation is a `GeoDecision` dict:

| Field | Type | Description |
|---|---|---|
| `allowed` | bool | `True` if all rules passed |
| `reason` | str | `"allowed"`, `"system_rule"`, `"group_rule"`, `"lookup_failed"`, etc. |
| `detail` | str | Human-readable explanation |
| `ip` | str | Evaluated IP address |
| `country` | str | Country name |
| `country_code` | str | ISO 3166-1 alpha-2 code |
| `region` | str | Subdivision name |
| `region_code` | str | ISO 3166-2 code |
| `abuse` | dict | `{tor, vpn, datacenter, proxy}` booleans |
| `checked_at` | str | ISO 8601 timestamp |
| `rule_level` | str | `"system"` or `"group"` — which level caused a block |

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

When a request is blocked the decorator returns 403 immediately with only:

```json
{
  "error": "Access denied",
  "code": "geofence_blocked",
  "reason": "system_rule",
  "detail": "Your country is not permitted."
}
```

Country, region, and abuse details are intentionally omitted from the blocked response to prevent information leakage.

### OAuth `/complete` is not decorated

The OAuth `/callback` endpoint returns an HTTP redirect, not JSON, so `@md.requires_geofence` is not applied there. System rules still apply at other OAuth steps; group rules do not apply at `/complete` because the `group_uuid` is encoded inside the signed OAuth state string and is not decoded until inside the view — after any decorator would have run.

---

## `bypass_geofence` Permission

A user with the `bypass_geofence` permission short-circuits all geofence checks entirely. The check returns `allowed=True` immediately without writing a cache entry, so revoking the permission takes effect on the very next request.

---

## Settings Reference

| Setting | Default | Description |
|---|---|---|
| `GEOFENCE_ENABLED` | `True` | Master kill switch. Set to `False` to disable all checks globally. |
| `GEOFENCE_SYSTEM_RULES` | `{}` | Platform-wide hard-floor rule (no-op by default). |
| `GEOFENCE_CACHE_TTL` | `300` | Redis cache TTL in seconds for geo decisions. |
| `GEOFENCE_FAIL_CLOSED` | `False` | If `True`, deny access when the geoip lookup fails. If `False`, allow on failure. |
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
