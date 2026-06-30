# GeoIP — Django Developer Reference

IP geolocation, threat intelligence, fleet-wide blocking, and whitelisting via the `GeoLocatedIP` model.

## Model: `GeoLocatedIP`

Located at `mojo.apps.account.models.geolocated_ip`.

Caches geolocation results per IP to reduce redundant API calls. Tracks security metadata (VPN, Tor, proxy, cloud, known attacker/abuser), maintains threat scoring, and serves as the **source of truth for IP blocking** across the fleet.

### Key Fields

| Field | Description |
|---|---|
| `ip_address` | Unique, indexed IP address |
| `subnet` | Subnet used for fallback lookups — IPv4: the dot-based `/24` prefix (first three octets); IPv6: the `/64` network. `CharField(max_length=45)`, nullable. |
| `country_code`, `country_name`, `region`, `region_code`, `city`, `postal_code` | Location fields. `region_code` is the ISO 3166-2 subdivision code (e.g. `US-FL`); populated from MaxMind subdivisions, ip-api, ipstack, or ipinfo (paid tier) and backfilled lazily via `refresh()`. |
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
| `instance.check_threats(from_sync=False)` | Run threat intelligence checks. Pass `from_sync=True` to suppress outbound federation push. |
| `instance.update_threat_from_incident(priority, block=False, from_sync=False)` | Escalate threat level from incident priority (0–15 scale). Pass `block=True` to allow auto-blocking when threat reaches `high`/`critical`. Pass `from_sync=True` to suppress outbound federation push. |
| `instance.block(reason, ttl, broadcast, from_sync=False)` | Block this IP fleet-wide (DB + broadcast). Always escalates `threat_level` to at least `high`. Pass `from_sync=True` to suppress outbound federation push. |
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

### block(reason, ttl, broadcast, from_sync=False)

```python
geo = GeoLocatedIP.geolocate("1.2.3.4")
geo.block(reason="ssh_brute_force", ttl=3600)  # Block for 1 hour fleet-wide
geo.block(reason="repeat_offender")             # Permanent block (no ttl)
```

- Returns `True` if the block succeeded or the IP was already actively blocked
- Returns `False` if the IP is whitelisted (whitelisting always wins)
- `ttl` in seconds. `None` or `0` = permanent (no auto-unblock)
- `broadcast=False` to update DB only (used during bulk operations)
- Always escalates `threat_level` to at least `high` atomically in the same UPDATE — never downgrades. This ensures every block entry point (admin REST, LLM agent, rule-engine handler, asyncjobs, manual) feeds the federation signal loop without extra code at each call site.

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

`update_threat_from_incident(priority, block=False)` is called when incidents are created for an IP. It escalates `threat_level` (never downgrades) based on incident priority:

| Incident Priority | Threat Level |
|---|---|
| 0–6 | No change |
| 7–9 | `medium` |
| 10–12 | `high` |
| 13–15 | `critical` |

By default (`block=False`) the method only updates the threat level — no automatic blocking occurs. This is intentional: blocking is delegated to the rule engine (`block://` handlers), which has full context on conditions and can apply TTLs and thresholds appropriately.

Pass `block=True` if you want the method to also block the IP when the new level reaches `high` or `critical`. When blocking is enabled, a 15-minute TTL (`ttl=900`) is applied via `block()`.

Whitelisted IPs get the threat level update but are never blocked regardless of the `block` parameter.

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

### `GET system/geoip/lookup` — Authenticated IP Lookup

```
GET /api/system/geoip/lookup?ip=1.2.3.4
```

**Requires authentication** (`@md.requires_auth()`). Rate limited to 30 requests/minute per IP. Used by the `mojo` provider on downstream instances to query the upstream.

| Param | Required | Description |
|---|---|---|
| `ip` | Yes | IP address to geolocate |
| `auto_refresh` | No | Refresh expired cache (default: `true`) |
| `graph` | No | Response graph (`default`, `basic`, `detailed`). The `mojo` provider requests `graph=detailed`. |

Returns the `GeoLocatedIP` record via `on_rest_get`.

### `POST system/geoip/sync` — Federation Abuse-Signal Receiver

```
POST /api/system/geoip/sync
```

**Requires:** ApiKey with `geoip_sync` permission (group-scoped). This endpoint is called by downstream mojo instances to push abuse signals observed locally back to this upstream.

| Body field | Required | Description |
|---|---|---|
| `ip` | Yes | IP address |
| `threat_level` | No* | New threat level (`low`, `medium`, `high`, `critical`). Applied as MAX — never downgrades. |
| `is_known_attacker` | No* | `true` only. OR semantics — never flips `True → False`. |
| `is_known_abuser` | No* | `true` only. OR semantics — never flips `True → False`. |

*At least one of `threat_level`, `is_known_attacker`, `is_known_abuser` must be present.

Payloads containing per-fleet enforcement fields (`is_blocked`, `is_whitelisted`, `blocked_*`, `whitelisted_*`) are rejected with a 200 error response.

**Loop prevention:** the receiver applies changes via raw `save(update_fields=...)`, not via `block()`/`check_threats()`, so `_maybe_push_abuse_signals` never fires on the receiver side.

**Response:**

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

`applied` contains only the fields that actually changed. An empty `applied` dict means the incoming values were already at or above the current values.

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

## GeoIP Providers

`geolocate_ip()` queries the configured primary provider, with an optional fallback. Set the provider name via `GEOIP_PRIMARY_PROVIDER` (or `GEOIP_FALLBACK_PROVIDER`).

Built-in providers: `ipinfo`, `ipstack`, `ip-api`, `maxmind`, `mojo`.

### `mojo` Provider

Use another django-mojo instance as a GeoIP data source. The downstream instance calls the upstream's `GET /api/system/geoip/lookup?graph=detailed` with an ApiKey token and caches the result locally.

**Configuration:**

| Setting | Default | Description |
|---|---|---|
| `GEOIP_PRIMARY_PROVIDER` | — | Set to `'mojo'` to use a mojo instance as primary |
| `GEOIP_MOJO_PROVIDER_URL` | `None` | Base URL of the upstream mojo instance (e.g. `https://hub.example.com`) |
| `GEOIP_API_KEY_MOJO` | — | ApiKey token sent as `Authorization: apikey <token>` |
| `GEOIP_MOJO_SYNC_ENABLED` | `True` | Master kill switch for outbound abuse-signal push-back |

**Behavior:**

- The upstream is trusted for all third-party detection: Tor, VPN, proxy, cloud, external blocklists. Local re-detection is skipped for `provider='mojo'` records (`skip_external=True`).
- Local internal-threat analysis (`check_internal_threats`) still runs so events observed only on this instance are captured.
- Threat flags are OR-merged with upstream values — `is_known_attacker` and `is_known_abuser` are never downgraded.
- Per-fleet firewall fields (`is_blocked`, `is_whitelisted`, `blocked_*`, `whitelisted_*`) are stripped at the boundary. Local enforcement state from the upstream never enters this instance's cache.

---

## Federation with Another Mojo Instance

When a downstream uses the `mojo` provider, it pushes newly observed abuse signals back to the upstream so a mesh of instances builds a shared abuse list.

### What is federated

| Signal | Semantics |
|---|---|
| `threat_level` | MAX — only pushed when level strictly rises |
| `is_known_attacker` | OR — only pushed on `False → True` flip |
| `is_known_abuser` | OR — only pushed on `False → True` flip |

### What is never federated

Per-fleet enforcement decisions stay local and are never pushed upstream:

`is_blocked`, `is_whitelisted`, `blocked_at`, `blocked_until`, `blocked_reason`, `block_count`, `whitelisted_reason`

### How a push is triggered

The following methods call `_maybe_push_abuse_signals()` after a change, provided:

- `self.provider == 'mojo'`
- `GEOIP_MOJO_PROVIDER_URL` is configured
- `GEOIP_MOJO_SYNC_ENABLED` is `True`

Triggering methods:

- `block()` — block always escalates `threat_level` to `high`, so a push fires on first block of any `mojo`-sourced IP
- `update_threat_from_incident()` — fires when incident escalation produces a rise
- `check_threats()` — fires when local analysis flips an attacker/abuser flag

Pass `from_sync=True` to suppress the push (used by the sync endpoint receiver to prevent loops).

### Push is always async

`_maybe_push_abuse_signals()` enqueues via `jobs.publish` — HTTP is never made inline. `block()` return latency is unaffected by upstream availability. Retries on 5xx with backoff; 4xx (auth, permission, validation) logs and drops without retry.

The async job is `mojo.apps.account.asyncjobs.push_abuse_signals`. It posts `{ip, threat_level?, is_known_attacker?, is_known_abuser?}` to `POST /api/system/geoip/sync` on the upstream.

---

## Settings

| Setting | Default | Description |
|---|---|---|
| `GEOLOCATION_ALLOW_SUBNET_LOOKUP` | `False` | Allow fallback to subnet match when exact IP not found |
| `GEOLOCATION_CACHE_DURATION_DAYS` | `90` | Days before a cached record expires |
| `GEOIP_MOJO_PROVIDER_URL` | `None` | Base URL of upstream mojo instance (enables mojo provider) |
| `GEOIP_API_KEY_MOJO` | — | ApiKey token for upstream mojo instance |
| `GEOIP_MOJO_SYNC_ENABLED` | `True` | Enable outbound abuse-signal federation push |

---

## Integration with Incident System

`GeoLocatedIP` and the incident system form a feedback loop. See [Incident System](../logging/incidents.md) for the full architecture.

1. **Events enrich GeoLocatedIP**: `sync_metadata()` calls `geolocate()` to attach geo/threat data to events.
2. **Incidents escalate threat levels**: `update_threat_from_incident()` is called on incident creation, escalating `threat_level` (never downgrades). It does not auto-block — blocking is delegated to the rule engine.
3. **Rules can auto-block**: The `block://` handler in a RuleSet calls `GeoLocatedIP.block()` when conditions are met.
4. **Admins manage via CRUD**: Block, unblock, whitelist actions through the standard REST interface with `manage_users` permission.
