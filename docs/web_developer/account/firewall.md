# Firewall & IP Security — REST API Reference

Build a security dashboard for monitoring and managing IP blocks, threat levels, and firewall activity. All endpoints use standard model CRUD — no custom APIs needed.

**Permissions required:** `view_security` (read), `manage_security` (block/unblock/whitelist actions)

## Overview

Three existing APIs combine to give you full firewall visibility:

| API | What it provides |
|-----|-----------------|
| `GET /api/system/geoip` | IP records with block status, threat level, geolocation |
| `GET /api/logit/log` | Firewall event history (blocks, unblocks, whitelist changes) |
| `GET /api/incident/incident` | Security incidents that triggered auto-blocks |

## IP Block Management

### List Blocked IPs

```
GET /api/system/geoip?is_blocked=true&graph=basic&sort=-blocked_at
```

Response includes block details:

```json
{
  "status": true,
  "data": [
    {
      "id": 42,
      "ip_address": "203.0.113.50",
      "country_code": "CN",
      "country_name": "China",
      "city": "Beijing",
      "is_blocked": true,
      "blocked_at": "2026-03-27T08:15:00Z",
      "blocked_until": "2026-03-27T08:25:00Z",
      "blocked_reason": "manual block: by admin@example.com",
      "block_count": 3,
      "is_whitelisted": false,
      "threat_level": "high",
      "is_tor": false,
      "is_vpn": true,
      "risk_score": 40,
      "block_active": true
    }
  ]
}
```

### Key Fields

| Field | Description |
|-------|-------------|
| `is_blocked` | Currently blocked (may be expired — check `block_active`) |
| `block_active` | Computed: blocked AND not expired AND not whitelisted |
| `blocked_at` | When the current block was applied |
| `blocked_until` | When the block expires (`null` = permanent) |
| `blocked_reason` | Why — includes trigger info (manual, auto:threat_escalation) |
| `block_count` | Total times this IP has been blocked |
| `is_whitelisted` | Whitelisted IPs are never blocked |
| `whitelisted_reason` | Why it was whitelisted |
| `threat_level` | `low`, `medium`, `high`, `critical` |
| `risk_score` | 0–100 computed score from threat signals |

### Block an IP

Use the `block` action on a GeoIP record:

```
POST /api/system/geoip/42
```

```json
{
  "action": "block",
  "value": {
    "reason": "Brute force attack",
    "ttl": 600
  }
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `reason` | No | Why the IP is being blocked (defaults to "manual block: by {username}") |
| `ttl` | No | Seconds until auto-unblock (`null` or `0` = permanent) |

The block is broadcast to all servers in the fleet automatically.

### Unblock an IP

```
POST /api/system/geoip/42
```

```json
{
  "action": "unblock",
  "value": "Verified as legitimate traffic"
}
```

The `value` is a string reason. The unblock is broadcast fleet-wide.

### Whitelist an IP

Whitelisted IPs are never blocked, even by automatic threat escalation:

```
POST /api/system/geoip/42
```

```json
{
  "action": "whitelist",
  "value": "Office IP — verified safe"
}
```

If the IP is currently blocked, whitelisting also unblocks it fleet-wide.

### Remove Whitelist

```
POST /api/system/geoip/42
```

```json
{
  "action": "unwhitelist"
}
```

### Refresh Threat Data

Re-fetch geolocation and run threat intelligence checks:

```
POST /api/system/geoip/42
```

```json
{
  "action": "threat_analysis"
}
```

## Useful Queries

### Currently Active Blocks

```
GET /api/system/geoip?is_blocked=true&sort=-blocked_at&graph=basic
```

Note: `is_blocked=true` includes expired blocks. Check the `block_active` computed field in the response to determine if the block is still in effect.

### High-Threat IPs

```
GET /api/system/geoip?threat_level=high&sort=-modified
GET /api/system/geoip?threat_level=critical&sort=-modified
```

### Tor/VPN/Proxy Traffic

```
GET /api/system/geoip?is_tor=true
GET /api/system/geoip?is_vpn=true
GET /api/system/geoip?is_proxy=true
```

### Whitelisted IPs

```
GET /api/system/geoip?is_whitelisted=true
```

### IPs by Country

```
GET /api/system/geoip?country_code=CN&is_blocked=true
```

### Search by IP, City, ISP

```
GET /api/system/geoip?search=203.0.113
GET /api/system/geoip?search=cloudflare
```

## Firewall Activity Log

All firewall actions are logged to logit with `kind` values prefixed by `firewall:`.

### List All Firewall Activity

```
GET /api/logit/log?kind__startswith=firewall:&sort=-created&size=50
```

### Filter by Action Type

| Query | Shows |
|-------|-------|
| `?kind=firewall:block` | Manual and API-triggered blocks |
| `?kind=firewall:unblock` | Unblock events |
| `?kind=firewall:auto_block` | Automatic blocks from threat escalation |
| `?kind=firewall:whitelist` | Whitelist additions |
| `?kind=firewall:unwhitelist` | Whitelist removals |
| `?kind=firewall:broadcast_block` | Fleet-wide block broadcasts |
| `?kind=firewall:broadcast_unblock` | Fleet-wide unblock broadcasts |

### Activity for a Specific IP

Use `model_id` to get all firewall events for a GeoIP record:

```
GET /api/logit/log?kind__startswith=firewall:&model_id=42&sort=-created
```

### Activity by Admin User

```
GET /api/logit/log?kind__startswith=firewall:&uid=5&sort=-created
```

### Activity in Date Range

```
GET /api/logit/log?kind__startswith=firewall:&dr_start=2026-03-26&dr_end=2026-03-27
```

### Log Entry Shape

```json
{
  "id": 5001,
  "created": "2026-03-27T08:15:00Z",
  "level": "info",
  "kind": "firewall:block",
  "log": "IP Blocked: 203.0.113.50 - Brute force attack",
  "payload": "{\"ip\": \"203.0.113.50\", \"reason\": \"Brute force attack\", \"ttl\": 600, \"blocked_until\": \"2026-03-27T08:25:00Z\", \"block_count\": 3, \"trigger\": \"manual\"}",
  "uid": 5,
  "username": "admin@example.com",
  "ip": "10.0.0.1",
  "model_name": "account.GeoLocatedIP",
  "model_id": 42
}
```

The `payload` field is a JSON string with structured data. Parse it client-side for detailed filtering and display.

## Connecting Incidents to Blocks

When an incident auto-escalates an IP's threat level, it may trigger an automatic block. To see the full chain:

### 1. Get incidents for an IP

```
GET /api/incident/incident?search=203.0.113.50&sort=-created
```

### 2. Get the auto-block log for that IP

```
GET /api/logit/log?kind=firewall:auto_block&model_id=42
```

### 3. Get the GeoIP record for current state

```
GET /api/system/geoip/42?graph=detailed
```

## Dashboard Patterns

### Security Overview Widget

Poll these three queries to build a summary card:

```
GET /api/system/geoip?is_blocked=true&size=0      → count of blocked IPs
GET /api/system/geoip?threat_level=critical&size=0 → count of critical threats
GET /api/system/geoip?is_whitelisted=true&size=0   → count of whitelisted IPs
```

Use `size=0` to get just the count without fetching records.

### Recent Activity Feed

```
GET /api/logit/log?kind__startswith=firewall:&sort=-created&size=20
```

### IP Detail View

For a single IP's full picture, fetch in parallel:

```
GET /api/system/geoip/42?graph=detailed
GET /api/logit/log?kind__startswith=firewall:&model_id=42&sort=-created
GET /api/incident/incident?search={ip_address}&sort=-created
```

## Firewall Metrics

Time-series metrics for firewall events are recorded under the `firewall` category. Use the metrics API to build trend charts and dashboards.

### Available Metrics

Only blocking events are tracked as metrics. Unblocks and whitelist changes are low-volume admin actions tracked via logit only.

| Slug | Description |
|------|-------------|
| `firewall:blocks` | Total IP blocks (manual + auto) |
| `firewall:auto_blocks` | Automatic blocks from threat escalation |
| `firewall:broadcasts` | Fleet-wide broadcast operations |
| `firewall:blocks:country:{CC}` | Blocks by country code (e.g. `firewall:blocks:country:CN`) |

### Query Examples

```
# Blocks per hour over the last 24 hours
GET /api/metrics/metric?slug=firewall:blocks&granularity=hours&dr_start=2026-03-26

# All firewall metrics for the last 7 days
GET /api/metrics/metric?category=firewall&granularity=days&dr_start=2026-03-20

# Auto-blocks trend (are we seeing more automated threats?)
GET /api/metrics/metric?slug=firewall:auto_blocks&granularity=days&dr_start=2026-03-01
```

### Dashboard Chart Ideas

- **Blocks over time** — line chart of `firewall:blocks` at hourly granularity
- **Auto vs manual** — stacked chart comparing `firewall:blocks` and `firewall:auto_blocks`
- **Top blocked countries** — query `firewall:blocks:country:*` slugs and rank
- **Block activity feed** — combine metrics chart with recent logit entries for full context

## Graphs

### GeoIP Graphs

| Graph | Use for |
|-------|---------|
| `default` | List views — all fields except raw data, includes computed `is_threat`, `is_suspicious`, `risk_score` |
| `basic` | Compact cards — core location + security + block fields |
| `detailed` | Detail views — everything including raw provider data |
