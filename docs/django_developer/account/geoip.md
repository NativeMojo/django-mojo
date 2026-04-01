# GeoIP — Django Developer Reference

IP geolocation, threat intelligence, fleet-wide blocking, and whitelisting via the `GeoLocatedIP` model.

## Model: `GeoLocatedIP`

Located at `mojo.apps.account.models.geolocated_ip`.

Caches geolocation results per IP to reduce redundant API calls. Tracks security metadata (VPN, Tor, proxy, cloud, known attacker/abuser), maintains threat scoring, and serves as the **source of truth for IP blocking** across the fleet.

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
| `asn`, `asn_org`, `isp` | Network provider info |
| `mobile_carrier` | Mobile carrier name (Verizon, AT&T, etc.) |
| `connection_type` | `residential`, `business`, `hosting`, `cellular`, etc. |
| `last_seen` | Last time this IP was encountered in the system |
| `provider` | Source of the geolocation data |
| `data` | JSON bag for raw provider data and threat check results |
| `expires_at` | Cache expiration (internal records never expire) |

### Blocking Fields

| Field | Description |
|---|---|
| `is_blocked` | Whether this IP is currently blocked |
| `blocked_at` | When the block was applied |
| `blocked_until` | When the block expires (`null` = permanent) |
| `blocked_reason` | Why the IP was blocked (e.g. `auto:threat_escalation`, `manual block: by admin`) |
| `block_count` | Number of times this IP has been blocked |

### Whitelisting Fields

| Field | Description |
|---|---|
| `is_whitelisted` | Whitelisted IPs are never blocked, even by auto-escalation |
| `whitelisted_reason` | Why this IP is whitelisted |

### Computed Properties

| Property | Description |
|---|---|
| `is_expired` | True if the cached data needs a refresh |
| `is_threat` | True if `is_known_attacker` or `is_known_abuser` |
| `is_suspicious` | True if Tor, VPN, proxy, or high/critical threat level |
| `risk_score` | 0–100 score based on threat indicators |
| `block_active` | True if `is_blocked` AND not whitelisted AND `blocked_until` hasn't passed |

### Key Methods

| Method | Description |
|---|---|
| `GeoLocatedIP.geolocate(ip_address, auto_refresh=True)` | Get or create a record; refreshes if expired |
| `GeoLocatedIP.lookup(ip_address)` | Alias for `geolocate()` |
| `instance.refresh(check_threats=False)` | Re-fetch geolocation data from provider |
| `instance.check_threats()` | Run threat intelligence checks |
| `instance.update_threat_from_incident(priority)` | Escalate threat level from incident priority (0–15 scale) |
| `instance.block(reason, ttl, broadcast)` | Block this IP fleet-wide (DB + broadcast) |
| `instance.unblock(reason, broadcast)` | Unblock this IP fleet-wide |
| `instance.whitelist(reason)` | Whitelist — also unblocks if currently blocked |
| `instance.unwhitelist()` | Remove whitelist status |

---

## Fleet-Wide IP Blocking

`GeoLocatedIP` is the single source of truth for IP blocking. When `block()` is called, it:

1. Returns `True` immediately if `is_blocked` is already `True` and the block has not expired (idempotent — no re-broadcast, no `block_count` increment)
2. Updates the database record (`is_blocked`, `blocked_at`, `blocked_until`, `blocked_reason`, `block_count`)
3. Broadcasts `broadcast_block_ip` to all instances via `jobs.broadcast_execute()`
4. Each instance's job runner (as `ec2-user`) applies the iptables DROP rule

### block(reason, ttl, broadcast)

```python
geo = GeoLocatedIP.geolocate("1.2.3.4")
geo.block(reason="ssh_brute_force", ttl=3600)  # Block for 1 hour fleet-wide
geo.block(reason="repeat_offender")             # Permanent block (no ttl)
```

- Returns `True` if the block succeeded or the IP was already actively blocked
- Returns `False` if the IP is whitelisted (whitelisting always wins)
- `ttl` in seconds. `None` or `0` = permanent (no auto-unblock)
- `broadcast=False` to update DB only (used during bulk operations)

### unblock(reason, broadcast)

```python
geo.unblock(reason="manual: false positive")
```

- Updates DB and broadcasts fleet-wide iptables removal
- `broadcast=False` for DB-only updates

### whitelist(reason)

```python
geo.whitelist(reason="office IP range")
```

- Sets `is_whitelisted=True`
- If the IP is currently blocked, it unblocks fleet-wide immediately
- Prevents all future auto-blocks (threat escalation, rule handlers)

### unwhitelist()

```python
geo.unwhitelist()
```

Removes whitelist protection. Does not auto-block — the IP would need to trigger rules again.

### Auto-block via threat escalation

`update_threat_from_incident(priority)` is called when incidents are created for an IP. It escalates `threat_level` (never downgrades) and **auto-blocks** IPs that reach `high` or `critical`:

| Incident Priority | Threat Level | Auto-Block? |
|---|---|---|
| 0–6 | No change | No |
| 7–9 | `medium` | No |
| 10–12 | `high` | Yes |
| 13–15 | `critical` | Yes |

Whitelisted IPs get the threat level update but are never auto-blocked.

### Expiry sweep

A cron job runs every minute (`sweep_expired_blocks`) that:
1. Finds all `GeoLocatedIP` records where `is_blocked=True` and `blocked_until` has passed
2. Bulk updates `is_blocked=False` in the DB
3. Broadcasts fleet-wide unblock for all expired IPs

This is a single job per minute — not one job per blocked IP.

---

## POST_SAVE_ACTIONS

All blocking and management operations are exposed as POST_SAVE_ACTIONS on the model, following the standard CRUD pattern.

| Action | Payload | Description |
|---|---|---|
| `block` | `{"reason": "...", "ttl": 600}` or omit for defaults | Block IP fleet-wide. Defaults to 600s TTL and logs the admin username. |
| `unblock` | `"reason string"` or omit for default | Unblock IP fleet-wide |
| `whitelist` | `"reason string"` or omit for default | Whitelist IP, unblocks if currently blocked |
| `unwhitelist` | — | Remove whitelist status |
| `refresh` | — | Re-fetch geolocation data from provider (with threat checks) |
| `threat_analysis` | — | Run threat intelligence checks only |

### Example REST calls

```
POST /api/system/geoip/123
{"block": {"reason": "confirmed attacker", "ttl": 86400}}

POST /api/system/geoip/123
{"unblock": "false positive confirmed"}

POST /api/system/geoip/123
{"whitelist": "office VPN exit node"}

POST /api/system/geoip/123
{"unwhitelist": 1}
```

All actions require `manage_users` permission (from RestMeta).

---

## RestMeta

| Setting | Value |
|---|---|
| `VIEW_PERMS` | `['manage_users']` |
| `SEARCH_FIELDS` | `ip_address`, `city`, `country_name`, `asn_org`, `isp` |
| `POST_SAVE_ACTIONS` | `refresh`, `threat_analysis`, `block`, `unblock`, `whitelist`, `unwhitelist` |

### Graphs

| Graph | Description |
|---|---|
| `default` | All fields except `data` and `provider`, plus computed extras |
| `basic` | Core location + threat + blocking fields |
| `detailed` | All fields including raw `data` |

All graphs include `is_threat`, `is_suspicious`, and `risk_score` as extras. The `basic` graph also includes `block_active`.

---

## REST Endpoints

The account app sets `APP_NAME = ""`, so these endpoints register directly under `/api/system/`.

### `GET/POST system/geoip` — List / Create

```
GET  /api/system/geoip
POST /api/system/geoip
```

Standard CRUD via `GeoLocatedIP.on_rest_request`. Requires `manage_users` permission.

### `GET/PUT/DELETE system/geoip/<pk>` — Detail / Update / Delete

```
GET    /api/system/geoip/123
PUT    /api/system/geoip/123
DELETE /api/system/geoip/123
```

Requires `manage_users` permission. PUT supports POST_SAVE_ACTIONS for block/unblock/whitelist.

### `GET system/geoip/lookup` — Public IP Lookup

```
GET /api/system/geoip/lookup?ip=1.2.3.4
```

**Public endpoint** — no authentication required. Rate limited to 30 requests/minute per IP.

| Param | Required | Description |
|---|---|---|
| `ip` | Yes | IP address to geolocate |
| `auto_refresh` | No | Refresh expired cache (default: `true`) |

Returns the `GeoLocatedIP` record via `on_rest_get` (default graph).

### `GET system/geoip/time` — Public IP Time Lookup

```
GET /api/system/geoip/time
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

---

## Settings

| Setting | Default | Description |
|---|---|---|
| `GEOLOCATION_ALLOW_SUBNET_LOOKUP` | `False` | Allow fallback to subnet match when exact IP not found |
| `GEOLOCATION_CACHE_DURATION_DAYS` | `90` | Days before a cached record expires |

---

## Integration with Incident System

`GeoLocatedIP` and the incident system form a feedback loop. See [Incident System](../logging/incidents.md) for the full architecture.

1. **Events enrich GeoLocatedIP**: `sync_metadata()` calls `geolocate()` to attach geo/threat data to events.
2. **Incidents escalate threat levels**: `update_threat_from_incident()` is called on incident creation, escalating `threat_level` and auto-blocking at `high`/`critical`.
3. **Rules can auto-block**: The `block://` handler in a RuleSet calls `GeoLocatedIP.block()` when conditions are met.
4. **Admins manage via CRUD**: Block, unblock, whitelist actions through the standard REST interface with `manage_users` permission.
