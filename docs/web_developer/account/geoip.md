# GeoIP — REST API Reference

IP geolocation and time lookup endpoints.

The account app sets `APP_NAME = ""`, so these endpoints have no app prefix — they register directly under `/api/system/`.

---

## `GET geo/check` — Geofence Pre-flight Check

```
GET /api/geo/check
GET /api/geo/check?group_uuid=<uuid>
```

**Public** — no authentication required. This endpoint is itself not geofenced.

Use this before showing a login or registration form to detect whether the current user's IP is permitted to use the platform (or a specific tenant). Render a "not available in your region" page when `allowed` is `false` rather than letting the user hit a 403 mid-flow.

| Param | Required | Description |
|---|---|---|
| `group_uuid` | No | UUID of a specific group. When provided, group-level geofence rules are evaluated in addition to system rules. Omit to check system rules only. |
| `scope` | No | Endpoint scope to preview fail posture for — a scope listed in `GEOFENCE_FAIL_CLOSED_SCOPES` fails **closed** (denies) on a geo-lookup failure instead of the fail-open default. |

### Response

```json
{
    "status": true,
    "data": {
        "allowed": true,
        "reason": "passed",
        "detail": "Allowed.",
        "ip": "1.2.3.4",
        "country": "US",
        "country_code": "US",
        "region": "US-NY",
        "region_code": "US-NY",
        "abuse": {
            "tor": false,
            "vpn": false,
            "datacenter": false,
            "proxy": false
        },
        "checked_at": "2026-05-15T10:00:00Z",
        "rule_level": null
    }
}
```

`country`/`country_code` and `region`/`region_code` always carry the same ISO code — there is no separate human-readable name field.

When `allowed` is `false`, `rule_level` indicates which level caused the block (`"system"` or `"group"`), and `reason` and `detail` describe the specific rule that matched.

| Field | Description |
|---|---|
| `allowed` | `true` if all geofence rules passed |
| `reason` | One of `no_rules`, `disabled`, `bypass`, `ip_allowlisted`, `passed`, `lookup_failed`, `private_ip`, `country_not_allowed`, `region_not_allowed`, `tor_detected`, `vpn_detected`, `proxy_detected`, `datacenter_detected`, `rule_invalid`, `group_inactive` — see the django-developer [GeoDecision Shape](../../django_developer/account/geofence.md#geodecision-shape) reference |
| `detail` | Human-readable explanation of the decision |
| `rule_level` | `"system"` or `"group"` when blocked; `null` when allowed |
| `abuse` | Connection-type flags from the IP intelligence lookup |

When `reason` is `ip_allowlisted` (the IP matches the [IP allowlist](../../django_developer/account/geofence.md#ip-allowlist--full-exemption)), the response also carries `allowlist_source` (`"setting"` or `"geoip"`), `allowlist_reason`, `allowlist_until`, and the shadow-evaluation outcome `would_block` / `would_block_reason` (what the rules would have decided without the exemption).

---

## `GET system/geoip` — List GeoIP Records

```
GET /api/system/geoip
```

**Requires:** `manage_users` permission. A group ApiKey is rejected here even
if its `permissions` dict includes `manage_users` — `GeoLocatedIP` has no
per-group ownership, so this endpoint requires a real `User` session. See
[API Keys](api_keys.md) and the federation-sync endpoint below for the
supported key-based access path.

Returns a paginated list of cached GeoIP records. Supports standard query parameters (`search`, `sort`, `start`, `size`, `graph`).

**Search fields:** `ip_address`, `city`, `country_name`, `asn_org`, `isp`

### Graphs

| Graph | Description |
|---|---|
| `default` | All fields except raw provider data, plus `is_threat`, `is_suspicious`, `risk_score` |
| `basic` | Core location and threat fields only |
| `detailed` | All fields including raw data |

---

## `GET system/geoip/<pk>` — GeoIP Detail

```
GET /api/system/geoip/123
```

**Requires:** `manage_users` permission. Same ApiKey restriction as the list endpoint above.

Returns a single GeoIP record. Supports `?graph=` parameter.

### Actions (via POST)

| Action | Value | Description |
|---|---|---|
| `refresh` | — | Re-fetch geolocation data from provider with threat checks |
| `threat_analysis` | — | Run threat intelligence checks |
| `block` | `{"reason": "...", "ttl": 600}` | Block this IP fleet-wide (ttl in seconds, null=permanent) |
| `unblock` | `"reason string"` | Unblock this IP fleet-wide |
| `whitelist` | `"reason string"` or `{"reason": "...", "ttl": 3600, "until": "<iso>"}` | Whitelist — prevents all future blocks. `ttl` seconds or ISO `until` sets an expiry (invalid `until` → 400); omit both for permanent. |
| `unwhitelist` | — | Remove whitelist status |

See [firewall.md](firewall.md) for full firewall management and security dashboard guide.

---

## `GET system/geoip/lookup` — Authenticated IP Lookup

```
GET /api/system/geoip/lookup?ip=1.2.3.4
```

**Requires authentication.** Rate limited to **30 requests/minute** per IP.

| Param | Required | Description |
|---|---|---|
| `ip` | Yes | IP address to geolocate |
| `auto_refresh` | No | Refresh expired cache (default: `true`) |
| `graph` | No | Response graph (`default`, `basic`, `detailed`) |

### Response

```json
{
    "status": true,
    "data": {
        "id": 42,
        "ip_address": "1.2.3.4",
        "country_code": "US",
        "country_name": "United States",
        "region": "New York",
        "city": "New York",
        "timezone": "America/New_York",
        "is_tor": false,
        "is_vpn": false,
        "is_proxy": false,
        "is_threat": false,
        "is_suspicious": false,
        "risk_score": 0
    }
}
```

---

## `POST system/geoip/sync` — Federation Abuse-Signal Receiver

```
POST /api/system/geoip/sync
```

**Requires:** ApiKey token with `geoip_sync` permission (group-scoped). This endpoint is used by downstream django-mojo instances to push abuse signals observed locally back to this upstream. You do not call this manually — it is invoked by the `push_abuse_signals` async job.

### Request Body

| Field | Required | Description |
|---|---|---|
| `ip` | Yes | IP address |
| `threat_level` | No* | `low`, `medium`, `high`, or `critical`. Never downgrades. |
| `is_known_attacker` | No* | `true` only. Never flips back to `false`. |
| `is_known_abuser` | No* | `true` only. Never flips back to `false`. |

*At least one of the three signal fields is required.

Payloads containing `is_blocked`, `is_whitelisted`, `blocked_*`, or `whitelisted_*` are rejected — those fields are never federated.

### Response

```json
{
    "status": true,
    "data": {
        "ip": "1.2.3.4",
        "threat_level": "high",
        "is_known_attacker": true,
        "is_known_abuser": false,
        "applied": {
            "threat_level": "high",
            "is_known_attacker": true
        }
    }
}
```

`applied` contains only fields whose values changed. An empty `applied` means the incoming signals were already met or exceeded on this instance.

### Error Responses

| Condition | Response |
|---|---|
| No signal fields in body | `{"status": false, "error": "At least one signal field is required"}` |
| Invalid `threat_level` value | `{"status": false, "error": "Invalid threat_level"}` |
| Forbidden field in body | `{"status": false, "error": "Field 'is_blocked' is not federated"}` |
| Missing `geoip_sync` permission | `403` |

---

## `GET system/geoip/time` — Public IP Time Lookup

```
GET /api/system/geoip/time
```

**Public** — no authentication required. Rate limited to **30 requests/minute** per IP.

No parameters required. Uses the caller's IP address automatically to determine timezone and return the current time.

### Response

```json
{
    "status": true,
    "data": {
        "ip": "1.2.3.4",
        "timezone": "America/New_York",
        "epoch": 1711300800,
        "iso": "2026-03-24T12:00:00-04:00"
    }
}
```

| Field | Description |
|---|---|
| `ip` | The caller's detected IP address |
| `timezone` | IANA timezone string |
| `epoch` | Current time as Unix timestamp (seconds) |
| `iso` | Current time as ISO 8601 string with timezone offset |

### Error Response

If timezone data is not available for the IP:

```json
{
    "status": false,
    "error": "Timezone not available for this IP"
}
```
