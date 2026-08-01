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
| `data` | JSON bag for raw provider data and threat check results. `data.threat_data` holds `internal` (event stats), `blocklists` (per-source hits) and `is_blocklisted`. See [Threat Intelligence](#threat-intelligence). |
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
| `whitelisted_until` | When the whitelist expires (`null` = permanent) — mirrors `blocked_until`. An expired whitelist stops suppressing blocks and stops exempting geofence. |

### Computed Properties

| Property | Description |
|---|---|
| `is_expired` | True if the cached data needs a refresh |
| `is_threat` | True if `is_known_attacker` or `is_known_abuser` |
| `is_suspicious` | True if Tor, VPN, proxy, or high/critical threat level |
| `risk_score` | 0–100 score based on threat indicators |
| `block_active` | True if `is_blocked` AND no *active* whitelist AND `blocked_until` hasn't passed |
| `whitelist_active` | True if `is_whitelisted` AND `whitelisted_until` hasn't passed. Every consumer of whitelist state (block suppression, auto-block guards, geofence allowlist) uses this — an expired whitelist means the same thing everywhere. |

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
| `instance.whitelist(reason, ttl=None, until=None)` | Whitelist — also unblocks if currently blocked. `ttl` seconds or an explicit `until` datetime sets `whitelisted_until` (`until` wins; omit both for permanent). |
| `instance.unwhitelist()` | Remove whitelist status (clears `whitelisted_until` too) |

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

### whitelist(reason, ttl=None, until=None)

```python
geo.whitelist(reason="office IP range")                       # permanent
geo.whitelist(reason="contractor laptop", ttl=86400)          # 24h expiry
geo.whitelist(reason="audit window", until=some_datetime)     # explicit expiry
```

- Sets `is_whitelisted=True` (+ `whitelisted_until` from `ttl`/`until`; `until` wins)
- If the IP is currently blocked, it unblocks fleet-wide immediately
- Prevents all future auto-blocks (threat escalation, rule handlers) **while active** — an expired whitelist no longer suppresses anything
- Also exempts the IP from geofencing (see [Geofence — IP Allowlist](geofence.md)); every whitelist change invalidates that IP's cached geofence decisions and emits a `geofence_config` incident event

### unwhitelist()

```python
geo.unwhitelist()
```

Removes whitelist protection (clears `whitelisted_until`). Does not auto-block — the IP would need to trigger rules again.

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
| `whitelist` | `"reason string"` or `{"reason": "...", "ttl": 3600, "until": "<iso>"}` | Whitelist IP, unblocks if currently blocked. `ttl` seconds or ISO `until` sets the expiry (invalid `until` → 400). |
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

All actions are gated by `SAVE_PERMS`: `manage_users`, `manage_security`, or `security` — and the combined `users` term (it includes `manage_users` by definition).

---

## RestMeta

| Setting | Value |
|---|---|
| `VIEW_PERMS` | `['manage_users']` |
| `SAVE_PERMS` | `['manage_users', 'manage_security', 'security']` (the combined `users` term also satisfies `manage_users`) |
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

**A group ApiKey is rejected here** — `GeoLocatedIP` has no `group` FK, so
`_evaluate_permission`'s groupless branch denies an ApiKey identity by default
even if its `permissions` dict includes `manage_users` (`RestMeta` does not set
`ALLOW_API_KEY_GLOBAL`). Use a service-account `User`, or see
`POST system/geoip/sync` below for the supported key-based access path. See
[API Keys](api_keys.md#how-it-works).

### `GET/PUT/DELETE system/geoip/<pk>` — Detail / Update / Delete

```
GET    /api/system/geoip/123
PUT    /api/system/geoip/123
DELETE /api/system/geoip/123
```

Requires `manage_users` permission. PUT supports POST_SAVE_ACTIONS for block/unblock/whitelist. Same ApiKey restriction as List/Create above.

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

### Threat-List Caches (Tor exit list, blocklist.de)

`detect_tor()` and `check_blocklist_de()` read from two **cache-only**
incident `IPSet` rows — `tor_exits` and `blocklist_de` — instead of
downloading the full lists on every call. The rows are created and refreshed
every 6 hours by the incident app's `refresh_threat_lists` cron
(`refresh_from_source()` only). When a row is missing or not yet warmed
(fresh deploy before the first cron tick, or incident app not installed), the
readers fall back to the original live fetch — no flag day.

**These rows can never reach the firewall.** They are created with
`is_enabled=False`, the REST `enable` action rejects them with a 400, and
`sync()` is a hard no-op for them even if the flag is force-set — enabling an
ordinary `IPSet` puts it into the weekly `refresh_ipsets` firewall-sync path,
which for these lists would kernel-block every Tor exit node and
blocklist.de-listed IP fleet-wide. Detection remains policy-neutral — whether
Tor/blocklisted traffic is *blocked* stays a geofence-rule / firewall
decision.

The Tor row honors `source_url` (falling back to the `TOR_EXIT_NODE_LIST_URL`
setting); blocklist.de defaults to `https://lists.blocklist.de/lists/all.txt`.

### Threat Intelligence

`mojo.helpers.geoip.threat_intel.perform_threat_check(ip, skip_external=False)`
is the single entry point. It combines **internal** incident history with
**external** blocklists and returns:

```python
{
    'is_known_attacker': False,   # internal high-severity events
    'is_known_abuser': False,     # internal volume/mid-severity pattern
    'is_blocklisted': False,      # listed on any enabled external blocklist
    'threat_data': {
        'internal': {...},        # see "Internal analysis" below
        'blocklists': [...],      # per-source hit records
        'is_blocklisted': False,  # mirror of the top-level flag
    },
}
```

`threat_data` is the sub-dict consumers persist (`GeoLocatedIP.data['threat_data']`,
`geolocate_ip()`'s `data['threat_data']`), and `recalculate_threat_level()` reads
the blocklist flag from **inside** it — that is why the flag appears in both
places.

#### Internal analysis

`check_internal_threats(ip)` reads `incident.Event` rows with a matching
`source_ip`. It uses **two different windows**:

| Window | Setting | Default | Used for |
|---|---|---|---|
| Predicate | `GEOLOCATION_INTERNAL_THREAT_WINDOW_HOURS` | `24` | `is_known_attacker`, `is_known_abuser`, and every count that drives them |
| Display | `GEOLOCATION_INTERNAL_THREAT_LOOKBACK_DAYS` | `90` | `total_events`, `avg_level`, `top_categories`, `last_seen_event` |

The predicate answers "is this address hostile **right now**", and the
enforcement it feeds is measured in minutes. The 90-day count answered "has
anything ever gone wrong here", which on a shared egress is always yes. The long
window is kept for the admin UI only — it drives no boolean.

**`is_known_attacker` is a two-tier allowlist.** A category that is on neither
list counts toward **nothing**, whatever its level:

*Tier 1 — confirmed.* `GEOLOCATION_INTERNAL_ATTACKER_CONFIRMED_CATEGORIES`
(default `security:bouncer:honeypot_post`, `security:bouncer:campaign`,
`sensitive_field_probe`). No legitimate user produces these — a honeypot field
is invisible to a human. `GEOLOCATION_INTERNAL_ATTACKER_CONFIRMED_THRESHOLD`
(3) events at `level >= GEOLOCATION_INTERNAL_ATTACKER_LEVEL_THRESHOLD` (7) is
enough. Never suppressed.

*Tier 2 — suspect.* `GEOLOCATION_INTERNAL_ATTACKER_SUSPECT_CATEGORIES` (default
`login:unknown`, `reset:unknown`, `magic:unknown`, `totp:login_unknown`,
`sms:login_unknown`, `token:unknown`, `invalid_password`). A real user *can*
produce one of these by fumbling a credential, so volume alone proves nothing.
All three of these must hold:

1. at least `GEOLOCATION_INTERNAL_ATTACKER_SUSPECT_THRESHOLD` (25) events in the
   window, **and**
2. spread across at least `GEOLOCATION_INTERNAL_ATTACKER_MIN_TARGETS` (10)
   distinct user accounts, **and**
3. the address is not a shared egress (below).

The level floor does **not** apply to this tier — the category allowlist
replaces it, which is how `invalid_password` (reported at level 5, and the
single strongest credential-stuffing signal in the codebase) finally counts.

Breadth is measured as `COUNT(DISTINCT model_id)` over events whose `model_name`
names a user. Instance reporting (`user.report_incident(...)`, e.g.
`invalid_password`) stamps both; class-level reporting
(`User.class_report_incident`, e.g. `login:unknown` — the username does not
resolve to an account) leaves `model_id` NULL. **An event that names no account
is not a target**, so a burst of mistyped usernames from an office egress can
never satisfy the breadth gate on its own. That is deliberate: one person
fumbling their own password scores 1 target; a stuffing run scores many.

**Shared-egress suppressor.** If at least
`GEOLOCATION_INTERNAL_SHARED_EGRESS_MIN_DEVICES` (25) distinct
`BouncerSignal.muid` values were seen for the address inside the window, the
**suspect** tier is suppressed — that address fronts many independent browsers,
i.e. a NAT. `muid` is a server-set HttpOnly cookie present pre-auth, so page JS
cannot forge it. Two guards:

- Only muids whose `BouncerDevice.first_seen` **predates** the window count, so
  an attacker cycling fresh cookies on one address cannot fake a NAT.
- If the address has no `BouncerSignal` rows at all (deployment does not run the
  bouncer JS), the suppressor is skipped entirely rather than defaulting either
  way. `internal_stats['distinct_devices']` is `None` in that case — never `0`.

The confirmed tier is never suppressed: an attacker behind a corporate NAT is
still an attacker.

**`is_known_abuser` means sustained rate-limit tripping.** At least
`GEOLOCATION_INTERNAL_ABUSER_EVENT_THRESHOLD` (60) events whose category starts
with `GEOLOCATION_INTERNAL_ABUSER_CATEGORY_PREFIX` (`rate_limit:`) inside the
window. Each `rate_limit:*` event is deduped to one per key+IP per 60 seconds in
Redis, so 60 events is roughly an hour of the day spent over the limits. This is
a **semantic change to a federated field** — the old definition (≥10 events with
a mean level in `[4, 8)` over 90 days) effectively counted "has this IP ever used
the API", and `avg_level` no longer participates in any predicate.

**What counts for nothing.** Everything not named above: `rest_error`
(level **12**, emitted by `mojo/decorators/http.py` for *any* unhandled 500 and
attributed to the caller's IP), `assistant:error:*`, `system:health:*`,
`traffic:*`, `rate_limit:*` (abuse, not attack), every permission-denied
category, and — critically — `security:bouncer:block` and
`security:bouncer:session_*`. Excluding the bouncer's own decisions is what
breaks the self-confirming loop: a block emitted a level-8 event, which made
`is_known_attacker` true, which added +40 to the next risk score, which made the
next block more likely.

The tradeoff is deliberate: a new attack category next quarter counts for
nothing until someone adds it to a list. That fails toward missing an attacker
rather than toward blocking a real user, which is the direction these platforms
want. Add your own categories to either setting.

`internal_stats` carries the numbers behind the verdict:

```python
{
  'total_events': 412,           # 90d, display only
  'high_severity_events': 30,    # in-window attack evidence (confirmed+suspect)
  'confirmed_events': 0,
  'suspect_events': 30,
  'distinct_targets': 12,
  'distinct_devices': 3,         # None when the deployment has no bouncer data
  'shared_egress_suppressed': False,
  'abuse_events': 0,
  'avg_level': 5.4,              # 90d, display only
  'top_categories': [...],       # 90d, display only
  'last_seen_event': '...',      # 90d, display only
  'lookback_days': 90,
  'window_hours': 24,
}
```

#### Tuning and dry run

Every `GEOLOCATION_INTERNAL_*` threshold is re-read on **every call** through
`settings.get()`, so a DB-backed `Setting` row retunes detection with no code
change and no restart. (Before this they were captured at import via
`get_static()`, which reads file-based settings only.)

```bash
# Raise the suspect bar for one deployment, live.
curl -X POST https://api.example.com/api/account/setting \
  -H "Authorization: Bearer $TOKEN" \
  -d 'key=GEOLOCATION_INTERNAL_ATTACKER_SUSPECT_THRESHOLD' -d 'value=50'
```

Set `GEOLOCATION_INTERNAL_THREAT_DRY_RUN=True` to compute the verdict and log it
via `logit.info` (with the counts that drove it, to `mojo.log`) while returning
`False/False`. `internal_stats` still carries `dry_run: True` plus
`dry_run_is_known_attacker` / `dry_run_is_known_abuser`. On a busy deployment,
run a week in dry run before letting a retune act.

**Deprecated:** `GEOLOCATION_INTERNAL_ATTACKER_EXCLUDED_CATEGORIES` was the old
denylist. It is still honored if set — the named categories are subtracted from
both allowlists — and logs a deprecation warning. Its shape was the underlying
bug: every *new* level-8+ category silently became attack evidence, which is how
`magic:unknown` and then `rest_error` became "attacks". Remove it and edit the
allowlists instead.

#### Decay

`update_threat_from_incident()` and `block()` only ratchet `threat_level` up, so
a single level-8 event used to stamp an address `medium` forever. The incident
app's `recheck_active_threats` cron (daily, 04:20) re-runs `check_threats()` for
up to `GEOLOCATION_RECHECK_THREATS_MAX` (500) `GeoLocatedIP` rows whose
`last_seen` falls inside the predicate window, letting a recomputed **lower**
level replace the stored one. It runs with `skip_external=True` — the pass is
about decaying local evidence, and a daily outbound blocklist lookup per row is
not affordable. A previously recorded blocklist hit is carried forward rather
than erased: an unrun lookup is not a clean lookup.

Records with `provider == 'mojo'` are excluded. Their never-downgrade rule is the
federation contract with the upstream, not a local scoring decision.

**Threat level.** `geolocate_ip(check_threats=True)` folds threat intelligence
into `threat_level`, using the same `recalculate_threat_level()` weight table
that `GeoLocatedIP.check_threats()` uses, so the helper path and the model path
score identical evidence identically. The fold is **escalate-only** — it can
raise `threat_level` but never lower it, so provider-derived levels (Tor →
`high`) survive untouched, and `check_threats=False` behavior is unchanged.
A bare blocklist hit scores 30 → `medium`; a known attacker scores 50 → `high`;
both together → `critical`.

`geolocate_ip()` results also carry a top-level `is_blocklisted` bool on every
branch (private/reserved, `mojo` provider, and normal providers) so callers
don't have to reach into the raw blob. It is **not** persisted — `GeoLocatedIP`
has no such field, and `refresh()`'s `hasattr`-gated copy loop drops it. The
stored copy lives in `data['threat_data']['is_blocklisted']`.

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
| `GEOLOCATION_ENABLE_INTERNAL_THREAT_CHECK` | `True` | Run the internal incident-history analysis |
| `GEOLOCATION_ENABLE_BLOCKLIST_CHECK` | `True` | Run the external blocklist checks |
| `GEOLOCATION_INTERNAL_THREAT_LOOKBACK_DAYS` | `90` | **Display-only** stat window (`total_events`, `avg_level`, `top_categories`, `last_seen_event`). Drives no boolean. |
| `GEOLOCATION_INTERNAL_THREAT_WINDOW_HOURS` | `24` | **Predicate** window — every count that drives `is_known_attacker` / `is_known_abuser` |
| `GEOLOCATION_INTERNAL_ATTACKER_LEVEL_THRESHOLD` | `7` | Secondary severity floor, **confirmed tier only**. Was `8`; lowered because `sensitive_field_probe` is emitted at exactly 7. |
| `GEOLOCATION_INTERNAL_ATTACKER_CONFIRMED_CATEGORIES` | `['security:bouncer:honeypot_post', 'security:bouncer:campaign', 'sensitive_field_probe']` | Tier 1 — no legitimate user produces these |
| `GEOLOCATION_INTERNAL_ATTACKER_CONFIRMED_THRESHOLD` | `3` | Tier 1 events needed for `is_known_attacker` |
| `GEOLOCATION_INTERNAL_ATTACKER_SUSPECT_CATEGORIES` | `['login:unknown', 'reset:unknown', 'magic:unknown', 'totp:login_unknown', 'sms:login_unknown', 'token:unknown', 'invalid_password']` | Tier 2 — a real user can produce one; needs volume **and** breadth |
| `GEOLOCATION_INTERNAL_ATTACKER_SUSPECT_THRESHOLD` | `25` | Tier 2 events needed in the window |
| `GEOLOCATION_INTERNAL_ATTACKER_MIN_TARGETS` | `10` | Distinct user accounts tier 2 must hit |
| `GEOLOCATION_INTERNAL_SHARED_EGRESS_MIN_DEVICES` | `25` | Distinct pre-existing `BouncerSignal.muid`s that mark the address a NAT and suppress tier 2 |
| `GEOLOCATION_INTERNAL_ABUSER_CATEGORY_PREFIX` | `'rate_limit:'` | Category prefix that counts toward `is_known_abuser` |
| `GEOLOCATION_INTERNAL_ABUSER_EVENT_THRESHOLD` | `60` | Prefixed events in the window needed for `is_known_abuser` |
| `GEOLOCATION_INTERNAL_THREAT_DRY_RUN` | `False` | Compute and log the verdict but always return `False/False` |
| `GEOLOCATION_RECHECK_THREATS_MAX` | `500` | Max `GeoLocatedIP` rows the daily decay cron rechecks |
| `GEOLOCATION_INTERNAL_THREAT_EVENT_THRESHOLD` | `5` | **Deprecated / unused.** No predicate reads it. |
| `GEOLOCATION_INTERNAL_ATTACKER_EXCLUDED_CATEGORIES` | unset | **Deprecated denylist.** Still honored when set (subtracted from both allowlists) and logs a warning. See [Threat Intelligence](#threat-intelligence). |
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
