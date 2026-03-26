# GeoIP — Django Developer Reference

IP geolocation, threat intelligence, and time lookup via the `GeoLocatedIP` model and REST endpoints.

## Model: `GeoLocatedIP`

Located at `mojo.apps.account.models.geolocated_ip`.

Caches geolocation results per IP to reduce redundant API calls. Tracks security metadata (VPN, Tor, proxy, cloud, known attacker/abuser) and supports incident-driven threat escalation.

### Key Fields

| Field | Description |
|---|---|
| `ip_address` | Unique, indexed IP address |
| `subnet` | First three octets, used for subnet-based fallback lookups |
| `country_code`, `country_name`, `region`, `city`, `postal_code` | Location fields |
| `latitude`, `longitude` | Coordinates |
| `timezone` | IANA timezone string (e.g. `America/New_York`) |
| `is_tor`, `is_vpn`, `is_proxy`, `is_cloud`, `is_datacenter`, `is_mobile` | Connection type flags |
| `is_known_attacker`, `is_known_abuser` | Threat flags |
| `threat_level` | `low`, `medium`, `high`, or `critical` |
| `is_blocked`, `blocked_at`, `blocked_reason` | Incident-driven blocking |
| `provider` | Source of the geolocation data |
| `expires_at` | Cache expiration (internal records never expire) |

### Computed Properties

| Property | Description |
|---|---|
| `is_expired` | True if the cached data needs a refresh |
| `is_threat` | True if `is_known_attacker` or `is_known_abuser` |
| `is_suspicious` | True if Tor, VPN, proxy, or high/critical threat level |
| `risk_score` | 0–100 score based on threat indicators |

### Key Methods

| Method | Description |
|---|---|
| `GeoLocatedIP.geolocate(ip_address, auto_refresh=True)` | Get or create a record; refreshes if expired |
| `GeoLocatedIP.lookup(ip_address)` | Alias for `geolocate()` |
| `instance.refresh(check_threats=False)` | Re-fetch geolocation data from provider |
| `instance.check_threats()` | Run threat intelligence checks |
| `instance.update_threat_from_incident(priority)` | Escalate threat level from incident priority (0–15 scale) |

### RestMeta

| Setting | Value |
|---|---|
| `VIEW_PERMS` | `['manage_users']` |
| `SEARCH_FIELDS` | `ip_address`, `city`, `country_name`, `asn_org`, `isp` |
| `POST_SAVE_ACTIONS` | `refresh`, `threat_analysis` |

### Graphs

| Graph | Description |
|---|---|
| `default` | All fields except `data` and `provider`, plus computed extras |
| `basic` | Core location + threat fields only |
| `detailed` | All fields including raw `data` |

All graphs include `is_threat`, `is_suspicious`, and `risk_score` as extras.

### Settings

| Setting | Default | Description |
|---|---|---|
| `GEOLOCATION_ALLOW_SUBNET_LOOKUP` | `False` | Allow fallback to subnet match when exact IP not found |
| `GEOLOCATION_CACHE_DURATION_DAYS` | `90` | Days before a cached record expires |

---

## REST Endpoints

All endpoints are under the `account` app prefix (e.g. `/api/account/system/geoip`).

### `GET/POST system/geoip` — List / Create

```
GET  /api/account/system/geoip
POST /api/account/system/geoip
```

Standard CRUD via `GeoLocatedIP.on_rest_request`. Requires `manage_users` permission (from RestMeta).

### `GET/PUT/DELETE system/geoip/<pk>` — Detail / Update / Delete

```
GET    /api/account/system/geoip/123
PUT    /api/account/system/geoip/123
DELETE /api/account/system/geoip/123
```

Requires `manage_users` permission.

### `GET system/geoip/lookup` — Public IP Lookup

```
GET /api/account/system/geoip/lookup?ip=1.2.3.4
```

**Public endpoint** — no authentication required. Rate limited to 30 requests/minute per IP.

| Param | Required | Description |
|---|---|---|
| `ip` | Yes | IP address to geolocate |
| `auto_refresh` | No | Refresh expired cache (default: `true`) |

Returns the `GeoLocatedIP` record via `on_rest_get` (default graph).

### `GET system/geoip/time` — Public IP Time Lookup

```
GET /api/account/system/geoip/time
```

**Public endpoint** — no authentication required. Rate limited to 30 requests/minute per IP. Uses the caller's IP address automatically.

**Response:**

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

Returns an error if timezone data is not available for the IP:

```json
{
    "status": false,
    "error": "Timezone not available for this IP"
}
```
