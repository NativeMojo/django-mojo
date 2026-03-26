# GeoIP — REST API Reference

IP geolocation and time lookup endpoints.

All endpoints are under the `account` app prefix.

---

## `GET system/geoip` — List GeoIP Records

```
GET /api/account/system/geoip
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
GET /api/account/system/geoip/123
```

**Requires:** `manage_users` permission.

Returns a single GeoIP record. Supports `?graph=` parameter.

### Actions (via POST)

| Action | Description |
|---|---|
| `refresh` | Re-fetch geolocation data from provider with threat checks |
| `threat_analysis` | Run threat intelligence checks |

---

## `GET system/geoip/lookup` — Public IP Lookup

```
GET /api/account/system/geoip/lookup?ip=1.2.3.4
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
GET /api/account/system/geoip/time
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
