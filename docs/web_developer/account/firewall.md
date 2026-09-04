# Firewall & IP Security — REST API Reference

Build a security dashboard for monitoring desired IP blocks, checked fleet
enforcement, bulk IP sets, and firewall activity. GeoLocatedIP actions use the
model endpoint; IPSet lifecycle changes use the governed action endpoint.

**Permissions required:** `view_security` (read), `manage_security` (block/unblock/whitelist actions)

## Overview

These APIs combine to give you firewall visibility:

| API | What it provides |
|-----|-----------------|
| `GET /api/system/geoip` | IP records with block status, threat level, geolocation |
| `GET /api/logs` | Firewall event history (blocks, unblocks, whitelist changes) |
| `GET /api/incident/incident` | Security incidents that triggered auto-blocks |
| `GET /api/incident/ipset` | Bulk set metadata, desired state, dispatch time, and bounded sync error |
| `POST /api/incident/ipset/action` | Governed enable/disable/sync with revision and confirmation |

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
      "block_active": true,
      "firewall_generation": 7,
      "firewall_pending": false,
      "firewall_sync_error": "",
      "firewall_observed_at": "2026-03-27T08:15:02Z"
    }
  ]
}
```

### Key Fields

| Field | Description |
|-------|-------------|
| `is_blocked` | Desired block state (may be expired — check `block_active`); it is not observed kernel state |
| `block_active` | Computed desired state: blocked AND not expired AND no *active* whitelist (an expired whitelist no longer counts) |
| `blocked_at` | When the current desired block was recorded |
| `blocked_until` | When the block expires (`null` = permanent) |
| `blocked_reason` | Why — includes trigger info (manual, auto:threat_escalation) |
| `block_count` | Total times this IP has been blocked |
| `is_whitelisted` | Whitelisted IPs are never blocked while the whitelist is active — see `whitelist_active` |
| `whitelisted_reason` | Why it was whitelisted |
| `whitelisted_until` | When the whitelist expires (`null` = permanent) — mirrors `blocked_until` |
| `whitelist_active` | Computed: `is_whitelisted` AND `whitelisted_until` hasn't passed |
| `threat_level` | `low`, `medium`, `high`, `critical` |
| `risk_score` | 0–100 computed score from threat signals |
| `firewall_generation` | Monotonic desired-state generation used to reject stale receipts |
| `firewall_pending` | `true` until a checked action or aggregation of fresh matching host observations proves the current compatible-host snapshot |
| `firewall_sync_error` | Bounded code/message for partial, unknown, or superseded reconciliation |
| `firewall_observed_at` | Latest successful reconciliation observation; it can predate the current generation while `firewall_pending=true` |

A cleared pending flag records the exact compatible-host snapshot finalized at
`firewall_observed_at`; a host that joins later is not retroactively part of
that historical proof and must reconcile before the next current-roster
aggregation verifies.

During a mixed API rollout, a response without these `firewall_*` fields is a
legacy response, not evidence of enforcement. Migration
`0054_geolocatedip_firewall_reconciliation` marks every historically
firewall-touched or whitelisted row pending because the old broadcasts supplied
no compatible-host observation. One host's periodic repair cannot clear shared
truth: an exact-current-roster aggregator requires matching fenced observations
from every compatible host.

### Block an IP

Use the `block` action on a GeoIP record:

```
POST /api/system/geoip/42
```

```json
{
  "block": {
    "reason": "Brute force attack",
    "ttl": 600
  }
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `reason` | No | Why the IP is being blocked (defaults to "manual block: by {username}") |
| `ttl` | No | Seconds until auto-unblock (`null` or `0` = permanent) |

The response's `action_response` is a checked result. Treat the action as
enforced only when `status` is `verified`, `ok` is `true`, and `owned` is
`true`. `partial` means at least one checked-command dispatch was confirmed
without complete fleet proof; it does not prove that a broker mutation ran.
`unknown` means no dispatch or observation was confirmed. Neither
is proof that no host changed after a transport failure. Both leave durable
pending state for repair and must remain failure/pending in the UI.

### Unblock an IP

```
POST /api/system/geoip/42
```

```json
{
  "unblock": "Verified as legitimate traffic"
}
```

The value is a string reason. Use the same checked-result rule as block;
desired database state can be unblocked while `firewall_pending=true` records
an unverified fleet absence tombstone.

### Whitelist an IP

Whitelisted IPs are never blocked, even by automatic threat escalation:

```
POST /api/system/geoip/42
```

```json
{
  "whitelist": "Office IP — verified safe"
}
```

Or with an expiry instead of a permanent whitelist:

```json
{
  "whitelist": {
    "reason": "Contractor laptop",
    "ttl": 86400
  }
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `reason` | No | Why the IP is being whitelisted (defaults to "manual whitelist: by {username}") |
| `ttl` | No | Seconds until the whitelist expires |
| `until` | No | Explicit ISO expiry — wins over `ttl` if both are given. Invalid `until` → 400. |

Omit both `ttl` and `until` for a permanent whitelist. Whitelisting always
checks fleet-wide absence, even when database state was already unblocked, so
a stale host rule cannot be presented as success. An expired whitelist (past
`until`) stops suppressing blocks — it is not a permanent exemption.

### Remove Whitelist

```
POST /api/system/geoip/42
```

```json
{
  "unwhitelist": 1
}
```

Firewall mutation targets are canonical IPv4 addresses only. IPv6 is refused
with `unsupported_family` before desired block/whitelist state is written.

## Bulk IPSet lifecycle

Create or edit an IPSet through `POST /api/incident/ipset`. New rows always
start disabled. CIDRs are validated all-or-nothing, canonicalized, sorted, and
deduplicated; IPv6 is refused. Names are immutable, reserved broker/cache names
cannot be claimed, `is_enabled` is not generically writable, and rows cannot be
deleted because disabled rows are durable absence tombstones.

Enable, disable, or re-check a set with a fresh global human session:

```http
POST /api/incident/ipset/action
```

```json
{
  "action": "ipset.enable",
  "ipset_id": 17,
  "expected_modified": "2026-03-27T08:10:00Z",
  "confirm": "ENABLE IPSET 17"
}
```

The confirmation forms are `ENABLE IPSET <id>`, `DISABLE IPSET <id>`, and
`SYNC IPSET <id>`. The response includes `enforcement_status`,
`enforcement_ok`, `last_synced`, and `sync_error`. `last_synced` is the latest
checked dispatch or exact aggregated observation, not proof by itself. Shared
success is finalized only from exact current-roster observations; the same
action response with `enforcement_status=verified` plus
`enforcement_ok=true` is the direct compatible-host proof. API-key-backed and
stale-auth sessions are refused.

Migration `0054_geolocatedip_firewall_reconciliation` resets legacy IPSet
dispatch fields to unverified state. Wait until at least one v1 checked-capable
job engine is live on every intended host, then call `ipset.sync` to establish
new fleet proof. An invalid or IPv6 Geo row or legacy IPSet with an
invalid/reserved name, an enabled name over 27 characters, IPv6 CIDRs, or more
than 250,000 networks is quarantined individually while valid rows continue.
The configured permanent-aggregate set name is reserved dynamically too.
Migration forces a quarantined legacy IPSet disabled. Valid sibling rows can
still verify. Quarantined rows remain pending/error;
have the backend operator repair them
before expecting their action to verify. Names are immutable and deletion is
unsupported after cutover.

## Refresh Threat Data

Re-fetch geolocation and run threat intelligence checks:

```
POST /api/system/geoip/42
```

```json
{
  "threat_analysis": 1
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

Verified firewall actions are logged to logit with `kind` values prefixed by
`firewall:`. Partial/unknown checked results deliberately produce no success
log or success metric.

### List All Firewall Activity

```
GET /api/logs?kind__startswith=firewall:&sort=-created&size=50
```

### Filter by Action Type

| Query | Shows |
|-------|-------|
| `?kind=firewall:block` | Manual and API-triggered blocks |
| `?kind=firewall:unblock` | Unblock events |
| `?kind=firewall:auto_block` | Automatic blocks triggered by a rule handler |
| `?kind=firewall:whitelist` | Whitelist additions |
| `?kind=firewall:unwhitelist` | Whitelist removals |
| `?kind=firewall:broadcast_block` | Fleet-wide block broadcasts |
| `?kind=firewall:broadcast_unblock` | Fleet-wide unblock broadcasts |

### Activity for a Specific IP

Use `model_id` to get all firewall events for a GeoIP record:

```
GET /api/logs?kind__startswith=firewall:&model_id=42&sort=-created
```

### Activity by Admin User

```
GET /api/logs?kind__startswith=firewall:&uid=5&sort=-created
```

### Activity in Date Range

```
GET /api/logs?kind__startswith=firewall:&dr_start=2026-03-26&dr_end=2026-03-27
```

### Log Entry Shape

```json
{
  "id": 5001,
  "created": "2026-03-27T08:15:00Z",
  "level": "info",
  "kind": "firewall:block",
  "log": "IP Blocked: 203.0.113.50 - Brute force attack",
  "payload": "{\"ip\": \"203.0.113.50\", \"reason\": \"Brute force attack\", \"ttl\": 600, \"trigger\": \"manual\"}",
  "uid": 5,
  "username": "admin@example.com",
  "ip": "10.0.0.1",
  "model_name": "account.GeoLocatedIP",
  "model_id": 42
}
```

The `payload` field is a JSON string with structured data. Parse it client-side for detailed filtering and display.

## Connecting Incidents to Blocks

Incidents escalate an IP's `threat_level` but do not directly trigger blocks. Blocking is handled by the rule engine — when a rule with a `block://` handler fires, it calls `GeoLocatedIP.block()` fleet-wide. To trace the full chain for a given IP:

### 1. Get incidents for an IP

```
GET /api/incident/incident?search=203.0.113.50&sort=-created
```

### 2. Get the auto-block log for that IP

```
GET /api/logs?kind=firewall:auto_block&model_id=42
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
GET /api/logs?kind__startswith=firewall:&sort=-created&size=20
```

### IP Detail View

For a single IP's full picture, fetch in parallel:

```
GET /api/system/geoip/42?graph=detailed
GET /api/logs?kind__startswith=firewall:&model_id=42&sort=-created
GET /api/incident/incident?search={ip_address}&sort=-created
```

## Firewall Metrics

Time-series metrics for firewall events are recorded under the `firewall` category. Use the metrics API to build trend charts and dashboards.

### Available Metrics

Only blocking events are tracked as metrics. Unblocks and whitelist changes are low-volume admin actions tracked via logit only.

| Slug | Description |
|------|-------------|
| `firewall:blocks` | Total IP blocks (manual + auto) |
| `firewall:auto_blocks` | Automatic blocks triggered by rule handlers |
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
