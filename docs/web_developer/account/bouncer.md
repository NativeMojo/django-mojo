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
