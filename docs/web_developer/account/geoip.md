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

### Response

```json
{
    "status": true,
    "data": {
        "allowed": true,
        "reason": "allowed",
        "detail": "Request is permitted.",
        "ip": "1.2.3.4",
        "country": "United States",
        "country_code": "US",
        "region": "New York",
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

When `allowed` is `false`, `rule_level` indicates which level caused the block (`"system"` or `"group"`), and `reason` and `detail` describe the specific rule that matched.

| Field | Description |
|---|---|
| `allowed` | `true` if all geofence rules passed |
| `reason` | `"allowed"`, `"system_rule"`, `"group_rule"`, or `"lookup_failed"` |
| `detail` | Human-readable explanation of the decision |
| `rule_level` | `"system"` or `"group"` when blocked; `null` when allowed |
| `abuse` | Connection-type flags from the IP intelligence lookup |

---

## `GET system/geoip` — List GeoIP Records

```
GET /api/system/geoip
```

**Requires:** `manage_users` permission.

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

**Requires:** `manage_users` permission.

Returns a single GeoIP record. Supports `?graph=` parameter.

### Actions (via POST)

| Action | Value | Description |
|---|---|---|
| `refresh` | — | Re-fetch geolocation data from provider with threat checks |
| `threat_analysis` | — | Run threat intelligence checks |
| `block` | `{"reason": "...", "ttl": 600}` | Block this IP fleet-wide (ttl in seconds, null=permanent) |
| `unblock` | `"reason string"` | Unblock this IP fleet-wide |
| `whitelist` | `"reason string"` | Whitelist — prevents all future blocks |
| `unwhitelist` | — | Remove whitelist status |

See [firewall.md](firewall.md) for full firewall management and security dashboard guide.

---

## `GET system/geoip/lookup` — Public IP Lookup

```
GET /api/system/geoip/lookup?ip=1.2.3.4
```

**Public** — no authentication required. Rate limited to **30 requests/minute** per IP.

| Param | Required | Description |
|---|---|---|
| `ip` | Yes | IP address to geolocate |
| `auto_refresh` | No | Refresh expired cache (default: `true`) |

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
