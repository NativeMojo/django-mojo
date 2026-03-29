# Bouncer — Web Developer Reference

Server-gated bot detection for django-mojo. Bots never receive the login form, field
names, or auth API endpoint URLs. The challenge is server-rendered; all signals are
scored server-side before any auth surface is exposed.

See also: [Auth Pages](auth_pages.md) for the login/registration page customization,
OAuth setup, branding settings, and URL parameters.

---

## How the Login Flow Works

When `BOUNCER_LOGIN_PATH` is configured (e.g. `access`), the login flow has three stages:

```
1. User visits /{BOUNCER_LOGIN_PATH}
      ↓
   Django pre-screens (IP, headers, GeoIP, device cookie)
      ↓
   Known good device (pass cookie) → serve full login page immediately
   Suspicious / unknown           → serve challenge page
   Clearly bot                    → serve decoy honeypot page

2. Challenge page (if shown)
      mojo-bouncer.js collects behavioral signals
      User clicks the moving target button
      POST /api/account/bouncer/assess → decision + bouncer_token
      JS stores token in localStorage, redirects to /{BOUNCER_LOGIN_PATH}

3. Full login page (after passing challenge or on valid pass cookie)
      mojo-auth.js webapp loads — login form, OAuth, passkeys, magic link
      Every auth API call includes bouncer_token in the request body
      Server validates token before processing credentials
```

### Decoy Paths

Requests to `/login`, `/signin`, and any configured `BOUNCER_DECOY_PATHS` receive a
visually identical login page whose form POSTs to a dead endpoint. That endpoint always
returns a plausible-looking error with a realistic delay. Detection is never revealed.

---

## Assess Endpoint

The challenge page POSTs behavioral signals here. Called by `mojo-bouncer.js` — not
called directly by application code in the normal flow.

**POST** `/api/account/bouncer/assess`

No authentication required. Rate-limited per IP.

### Request

```json
{
  "duid": "browser-generated-device-uuid",
  "page_type": "login",
  "session_id": "client-generated-session-id",
  "signals": {
    "environment": {
      "webdriver_flag": false,
      "playwright_artifacts": false,
      "outer_size_zero": false,
      "languages_empty": false,
      "screen_zero": false,
      "chrome_runtime_missing": false
    },
    "behavior": {
      "mouse_move_count": 14,
      "first_interaction_ms": 820,
      "rapid_click": false,
      "mouse_straightness": 0.12,
      "document_focus_never": false
    },
    "gate_challenge": {
      "honeypot_filled": false,
      "time_to_click_ms": 1240,
      "had_mouse_movement": true,
      "is_touch_device": false
    }
  }
}
```

**Fields:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `duid` | string | yes | Device UUID, persisted in `localStorage('mojo_device_uid')` |
| `page_type` | string | yes | One of `login`, `registration`, `password_reset` |
| `session_id` | string | yes | Client-generated identifier for this challenge session |
| `signals.environment` | object | yes | Browser environment probe results |
| `signals.behavior` | object | yes | Mouse/keyboard behavioral signals |
| `signals.gate_challenge` | object | no | Present when user completed the gate challenge |

### Response — Allow / Monitor

```json
{
  "status": true,
  "data": {
    "decision": "allow",
    "risk_score": 8,
    "token": "eyJkdWlkIjoi...",
    "session_id": "client-generated-session-id"
  }
}
```

The `token` is a short-lived HMAC-signed string. Store it in `localStorage` and attach
it as `bouncer_token` in every subsequent auth API call (login, passkey complete, OAuth
complete, magic link, password reset).

Pass cookie `mbp` is set as an HttpOnly cookie on the response. The browser stores it
automatically when using `credentials: 'include'` on the fetch call. It allows the device
to skip the interactive challenge on subsequent visits within its TTL.

### Response — Block

```json
{
  "status": true,
  "data": {
    "decision": "block",
    "risk_score": 85
  }
}
```

No token is returned. The challenge page should display a neutral error state — do not
reveal that a bot was detected.

### Error Responses

**Rate limit exceeded:**

```json
{
  "status": false,
  "code": 429,
  "error": "Too many requests"
}
```

---

## Event Endpoint

Reports individual behavioral signals as they occur. Used by `mojo-bouncer.js` for
real-time signal streaming — not called directly in the normal flow.

**POST** `/api/account/bouncer/event`

No authentication required. Rate-limited per IP.

```json
{
  "duid": "browser-generated-device-uuid",
  "session_id": "client-generated-session-id",
  "event_type": "mouse_pattern",
  "signals": {
    "mouse_straightness": 0.98,
    "rapid_click": true
  }
}
```

Response:

```json
{
  "status": true,
  "data": {}
}
```

---

## Attaching the Bouncer Token to Auth Calls

Once a token is obtained from the assess endpoint, include it in every auth API call:

**POST** `/api/login`

```json
{
  "username": "alice@example.com",
  "password": "mysecretpassword",
  "bouncer_token": "<token-from-assess>",
  "duid": "browser-generated-device-uuid"
}
```

The same field applies to all auth endpoints that carry `@md.requires_bouncer_token`:

```json
{
  "bouncer_token": "<token-from-assess>",
  "duid": "..."
}
```

If `BOUNCER_REQUIRE_TOKEN` is `False` (default), missing or invalid tokens are logged
and the request proceeds normally. If `True`, a missing or invalid token returns 403.

### Token Constraints

- **Single-use** — consumed on first use; replay returns 403
- **IP-bound** — token issued from one IP is rejected from another
- **Short-lived** — default 15 minutes (configurable via `BOUNCER_TOKEN_TTL`)
- **Scoped** — a `login` token cannot be used on a `registration` endpoint

---

## Device UUID (duid)

The `duid` is a persistent device identifier shared across all mojo JS:

```javascript
// Read (or generate) the duid
const duid = localStorage.getItem('mojo_device_uid') || generateUUID();
localStorage.setItem('mojo_device_uid', duid);
```

Include it in:
- Every `POST /api/account/bouncer/assess` call
- Every `POST /api/account/bouncer/event` call
- Every auth API call (login, registration, password reset, OAuth, passkeys, magic link)

A missing `duid` is treated as `unknown` tier — all signals still run, nothing is skipped.

---

## Pass Cookie

On an allow/monitor decision, the backend sets an `mbp` HttpOnly cookie alongside the
JSON response. To receive it, the fetch call must include credentials:

```javascript
const resp = await fetch('/api/account/bouncer/assess', {
  method: 'POST',
  credentials: 'include',     // required for the pass cookie to be stored
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(payload),
});
```

The browser sends this cookie automatically on subsequent requests to the same origin.
When a valid pass cookie is present, the device skips the interactive challenge and
receives the full login page directly.

---

## Implementing Your Own Client (Non-mojo-auth.js)

If you are building a custom login flow rather than using `mojo-auth.js`:

1. Generate a `duid` on first load and persist it in `localStorage('mojo_device_uid')`.
2. Collect environment and behavioral signals.
3. `POST /api/account/bouncer/assess` with `credentials: 'include'`.
4. On `allow` or `monitor`: store the returned `token` in `localStorage`.
5. Include `bouncer_token` and `duid` in your login POST.
6. On `block`: show a neutral error; do not retry automatically.

The assess endpoint is designed to be called once per user session, just before the
login attempt. Calling it multiple times per session is rate-limited.

---

## Error Handling

| Scenario | Behavior |
|----------|----------|
| Assess endpoint down | Call login directly without a token; server logs and allows through (fail-open) if `BOUNCER_REQUIRE_TOKEN=False` |
| Token expired before login | Login returns 403 `bouncer_token_invalid`; re-run assess to get a fresh token |
| Token replayed | Login returns 403 `bouncer_token_consumed` |
| IP changed between assess and login | Login returns 403 `bouncer_token_ip_mismatch` |
| No token, enforce mode | Login returns 403 `bouncer_token_required` |

---

## Bouncer Token Error Codes

When `BOUNCER_REQUIRE_TOKEN=True` and token validation fails:

```json
{
  "status": false,
  "code": 403,
  "error": "bouncer_token_invalid"
}
```

| `error` value | Cause |
|---------------|-------|
| `bouncer_token_required` | No `bouncer_token` field in request |
| `bouncer_token_invalid` | Token failed signature or structure validation |
| `bouncer_token_expired` | Token TTL elapsed |
| `bouncer_token_ip_mismatch` | Request IP differs from token issue IP |
| `bouncer_token_consumed` | Nonce already used (replay attempt) |
| `bouncer_token_scope` | Token `page_type` does not match this endpoint |

---

## Admin Visibility APIs

Three REST endpoints provide full admin visibility into bouncer activity. Use these to build security dashboards, investigate bot attacks, and manage bot signatures.

**Permissions required:** `manage_users` OR `admin_security`

### Devices — `/api/account/bouncer/device`

Every unique browser/device that interacts with the bouncer gets a `BouncerDevice` record. This is the device reputation database.

#### List Devices

```
GET /api/account/bouncer/device?sort=-last_seen&graph=list&size=50
```

Response:

```json
{
  "status": true,
  "data": [
    {
      "id": 1,
      "muid": "m_abc123def456",
      "duid": "browser-uuid-here",
      "risk_tier": "blocked",
      "event_count": 47,
      "block_count": 12,
      "last_seen_ip": "203.0.113.50",
      "last_seen": "2026-03-28T14:22:00Z"
    }
  ]
}
```

#### Device Detail

```
GET /api/account/bouncer/device/1
```

Returns full record including `msid` (session ID), `fingerprint_id`, `linked_muids` (cross-session identity stitching), and `first_seen`.

#### Key Fields

| Field | Description |
|-------|-------------|
| `muid` | Mojo unique device identifier (persistent across sessions) |
| `duid` | Browser-generated device UUID from `localStorage` |
| `fingerprint_id` | Browser fingerprint hash (canvas, WebGL, fonts, etc.) |
| `risk_tier` | `unknown`, `low`, `medium`, `high`, `blocked` |
| `event_count` | Total bouncer assessments for this device |
| `block_count` | Times this device was blocked |
| `last_seen_ip` | Most recent IP address |
| `linked_muids` | Other muid values linked to this device (fingerprint stitching) |

#### Useful Queries

```
# Blocked devices
GET /api/account/bouncer/device?risk_tier=blocked&sort=-block_count

# High-risk devices
GET /api/account/bouncer/device?risk_tier=high&sort=-last_seen

# Devices by IP
GET /api/account/bouncer/device?search=203.0.113.50

# Most active devices
GET /api/account/bouncer/device?sort=-event_count&size=20
```

---

### Signals — `/api/account/bouncer/signal`

Every bouncer assessment is recorded as a `BouncerSignal`. This is a **read-only** audit trail — every challenge attempt, every scoring decision, with full signal payloads.

#### List Signals

```
GET /api/account/bouncer/signal?sort=-created&graph=list&size=50
```

Response:

```json
{
  "status": true,
  "data": [
    {
      "id": 501,
      "muid": "m_abc123def456",
      "msid": "session_xyz",
      "stage": "assess",
      "ip_address": "203.0.113.50",
      "page_type": "login",
      "risk_score": 85,
      "decision": "block",
      "created": "2026-03-28T14:22:00Z"
    }
  ]
}
```

#### Signal Detail

```
GET /api/account/bouncer/signal/501?graph=detail
```

The `detail` graph includes the full signal payloads and linked records:

```json
{
  "status": true,
  "data": {
    "id": 501,
    "muid": "m_abc123def456",
    "duid": "browser-uuid-here",
    "msid": "session_xyz",
    "mtab": "tab_id_here",
    "session_id": "client-session-id",
    "stage": "assess",
    "ip_address": "203.0.113.50",
    "page_type": "login",
    "risk_score": 85,
    "decision": "block",
    "triggered_signals": ["webdriver_flag", "playwright_artifacts", "rapid_click"],
    "raw_signals": {
      "environment": {"webdriver_flag": true, "playwright_artifacts": true},
      "behavior": {"mouse_move_count": 0, "rapid_click": true},
      "gate_challenge": {"honeypot_filled": false, "time_to_click_ms": 12}
    },
    "server_signals": {
      "ip_reputation": "high_risk",
      "geo_risk": 0.7,
      "header_anomalies": ["missing_accept_language"]
    },
    "token_nonce": "abc123",
    "created": "2026-03-28T14:22:00Z",
    "device": {
      "id": 1, "muid": "m_abc123def456", "duid": "browser-uuid-here",
      "risk_tier": "blocked", "event_count": 47, "block_count": 12,
      "last_seen_ip": "203.0.113.50", "last_seen": "2026-03-28T14:22:00Z"
    },
    "geo_ip": {
      "id": 42, "ip_address": "203.0.113.50", "country_code": "CN",
      "country_name": "China", "city": "Beijing", "is_blocked": true
    }
  }
}
```

#### Key Fields

| Field | Description |
|-------|-------------|
| `stage` | `assess` (challenge completion), `submit` (form submit), `event` (client event) |
| `risk_score` | 0–100 composite score from all analyzers |
| `decision` | `allow`, `monitor`, `block`, `log` |
| `triggered_signals` | Array of signal names that contributed to the score |
| `raw_signals` | Client-side signals as submitted by mojo-bouncer.js |
| `server_signals` | Server-side enrichment (IP reputation, geo risk, header analysis) |
| `page_type` | `login`, `registration`, `password_reset` |

#### Useful Queries

```
# Recent blocks
GET /api/account/bouncer/signal?decision=block&sort=-created&size=50

# All signals for a specific device
GET /api/account/bouncer/signal?search=m_abc123def456&sort=-created

# Signals from a specific IP
GET /api/account/bouncer/signal?search=203.0.113.50&sort=-created

# High-score assessments (potential bots that were allowed)
GET /api/account/bouncer/signal?decision=monitor&sort=-risk_score

# Signals by stage
GET /api/account/bouncer/signal?stage=assess&sort=-created
```

---

### Bot Signatures — `/api/account/bouncer/signature`

Bot signatures are patterns the bouncer uses for **pre-screening** — matching known bots before running the full scoring pipeline. Signatures are auto-learned from confirmed blocks and can also be created manually.

Pre-screen matches serve the honeypot decoy page immediately, with zero scoring overhead.

#### List Signatures

```
GET /api/account/bouncer/signature?sort=-modified&graph=list
```

Response:

```json
{
  "status": true,
  "data": [
    {
      "id": 10,
      "sig_type": "subnet_24",
      "value": "203.0.113.0/24",
      "source": "auto",
      "confidence": 95,
      "hit_count": 234,
      "is_active": true,
      "expires_at": "2026-03-29T14:00:00Z",
      "modified": "2026-03-28T14:22:00Z"
    }
  ]
}
```

#### Signature Detail

```
GET /api/account/bouncer/signature/10
```

Returns full record including `block_count`, `notes`, and `created`.

#### Create a Manual Signature

```
POST /api/account/bouncer/signature
```

```json
{
  "sig_type": "ip",
  "value": "198.51.100.5",
  "source": "manual",
  "confidence": 100,
  "is_active": true,
  "notes": "Known scanner — reported by hosting provider"
}
```

#### Update a Signature

```
POST /api/account/bouncer/signature/10
```

```json
{
  "is_active": false,
  "notes": "Disabled — false positive on corporate proxy"
}
```

#### Delete a Signature

```
DELETE /api/account/bouncer/signature/10
```

#### Key Fields

| Field | Description |
|-------|-------------|
| `sig_type` | `ip`, `subnet_24`, `subnet_16`, `user_agent`, `fingerprint`, `signal_set` |
| `value` | The pattern to match (IP, subnet CIDR, UA string, fingerprint hash, signal set hash) |
| `source` | `auto` (learned from blocks) or `manual` (admin-created) |
| `confidence` | 0–100 confidence score |
| `hit_count` | Pre-screen cache hits (how many times this signature matched) |
| `block_count` | How many of those hits resulted in blocks |
| `is_active` | Active signatures are loaded into the pre-screen cache |
| `expires_at` | Auto-learned signatures expire (null = permanent) |

#### Signature Types

| Type | What it matches | Auto-learn trigger |
|------|----------------|-------------------|
| `ip` | Exact IP address | Direct match |
| `subnet_24` | /24 subnet (e.g. `203.0.113.0/24`) | 5+ blocks from same /24 |
| `subnet_16` | /16 subnet | Manual only |
| `user_agent` | Exact User-Agent string | 5+ blocks with same UA |
| `fingerprint` | Browser fingerprint hash | 3+ blocks with same fingerprint |
| `signal_set` | Hash of triggered signal combination | 5+ blocks with same signal pattern (campaign) |

#### Useful Queries

```
# Active signatures by type
GET /api/account/bouncer/signature?sig_type=subnet_24&is_active=true&sort=-hit_count

# Auto-learned signatures
GET /api/account/bouncer/signature?source=auto&sort=-modified

# Most effective signatures (highest hit count)
GET /api/account/bouncer/signature?is_active=true&sort=-hit_count&size=20

# Expiring soon
GET /api/account/bouncer/signature?is_active=true&sort=expires_at

# Manual overrides
GET /api/account/bouncer/signature?source=manual
```

---

## Bouncer Events in the Incident System

Bouncer events flow into the incident system automatically. High-confidence detections trigger firewall blocks via default rules.

### Event Flow

```
Bouncer scores request → block decision
  → Creates BouncerSignal (audit trail)
  → Fires incident event (security:bouncer:block, level 8)
    → Incident created (level >= threshold)
      → Default rule matches → block:// handler → IP blocked fleet-wide
```

### Event Categories

| Category | Level | Creates Incident | Default Rule Action |
|----------|-------|-----------------|-------------------|
| `security:bouncer:block` | 8 | Yes | Score >= 80: block IP 1hr |
| `security:bouncer:honeypot_post` | 9 | Yes | Block IP 1hr |
| `security:bouncer:campaign` | 10 | Yes | Block IP 24hr + notify admin |
| `security:bouncer:token_invalid` | 7 | Yes | Block IP 30min |
| `security:bouncer:monitor` | 5 | No | — |
| `security:bouncer:event` | 5–7 | Conditional | — |
| `security:bouncer:token_missing` | 6 | No | — |

### Querying Bouncer Incidents

```
# All bouncer incidents
GET /api/incident/incident?category__startswith=security:bouncer&sort=-created

# Bouncer events (lower level, not incidents)
GET /api/incident/event?category__startswith=security:bouncer&sort=-created
```

---

## Bouncer Metrics

Time-series metrics for bouncer activity are recorded under the `bouncer` category.

### Available Metrics

| Slug | Description |
|------|-------------|
| `bouncer:assessments` | Total scoring runs (volume indicator) |
| `bouncer:blocks` | Full-scoring blocks |
| `bouncer:blocks:country:{CC}` | Blocks by country code (e.g. `bouncer:blocks:country:CN`) |
| `bouncer:monitors` | Suspicious but allowed (monitor decision) |
| `bouncer:pre_screen_blocks` | Signature cache hits (served decoy without scoring) |
| `bouncer:honeypot_catches` | Credential attempts on decoy pages |
| `bouncer:signatures_learned` | Auto-created bot signatures |
| `bouncer:campaigns` | Coordinated bot campaign detections |

### Query Examples

```
# Blocks per hour over the last 24 hours
GET /api/metrics/fetch?slug=bouncer:blocks&granularity=hours&dr_start=2026-03-27

# All bouncer metrics for the last 7 days
GET /api/metrics/fetch?category=bouncer&granularity=days&dr_start=2026-03-21

# Pre-screen effectiveness (are signatures catching bots before scoring?)
GET /api/metrics/fetch?slug=bouncer:pre_screen_blocks&granularity=hours&dr_start=2026-03-27

# Assessment volume trend (is bot traffic increasing?)
GET /api/metrics/fetch?slug=bouncer:assessments&granularity=hours&dr_start=2026-03-27
```

---

## Dashboard Patterns

### Bouncer Overview Card

Poll these queries to build a summary card:

```
GET /api/account/bouncer/device?risk_tier=blocked&size=0    → count of blocked devices
GET /api/account/bouncer/signature?is_active=true&size=0    → count of active signatures
GET /api/account/bouncer/signal?decision=block&dr_start=2026-03-28&size=0  → blocks today
```

Use `size=0` to get just the count without fetching records.

### Recent Block Feed

```
GET /api/account/bouncer/signal?decision=block&sort=-created&graph=list&size=20
```

### Device Investigation View

For a single device, fetch in parallel:

```
GET /api/account/bouncer/device/{id}
GET /api/account/bouncer/signal?search={muid}&sort=-created&graph=list
GET /api/incident/event?category__startswith=security:bouncer&search={muid}&sort=-created
```

### Chart Ideas

- **Block rate** — line chart of `bouncer:blocks` at hourly granularity
- **Pre-screen vs full scoring** — stacked chart comparing `bouncer:pre_screen_blocks` and `bouncer:blocks`
- **Assessment volume** — area chart of `bouncer:assessments` to show traffic patterns
- **Top blocked countries** — query `bouncer:blocks:country:*` slugs and rank
- **Signature effectiveness** — table of signatures sorted by `hit_count`
- **Decision breakdown** — pie chart from signal list grouped by `decision`

### Graphs

| Graph | Use for |
|-------|---------|
| `list` | Compact list views — core fields only |
| `default` | Standard views — all fields, linked device on signals |
| `detail` | Investigation views — full signal payloads, linked device + GeoIP (signals only) |
