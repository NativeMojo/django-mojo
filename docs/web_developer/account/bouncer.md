# Bouncer — Web Developer Reference

Server-side risk screening for hosted auth/contact pages and a separate
embeddable SDK. Hosted pages use a Continue check.
Continue is not proof of a human; authentication,
token enforcement, permissions, and rate limits remain separate controls.

See also: [Auth Pages](auth_pages.md) for the login/registration page customization,
OAuth setup, branding settings, and URL parameters.

---

## How the Login Flow Works

The hosted login path defaults to `/auth` (`BOUNCER_LOGIN_PATH` can change it).
Registration and contact use the same gate:

```
1. User visits /{BOUNCER_LOGIN_PATH}
      ↓
   Django checks current risk, signatures, and retained restrictions
      ↓
   Valid matching _muid + mbp + mbs cookies → real page
   Low risk without a pass            → Continue check
   Recoverable uncertainty            → Continue check
   Current qualifying bot evidence    → operator recovery
   Existing blocked/frozen restriction → operator recovery

2. Challenge page (if shown)
      mojo-hosted-bouncer.js sends a render-issued descriptor to assess
      Server checks current policy and sets the pass cookie
      Separate confirm request verifies the returned cookie and current restrictions
      Only confirmed success navigates once to /{BOUNCER_LOGIN_PATH}
      Safe navigation/registration parameters are forwarded

3. Full login page (after passing challenge or on valid pass cookie)
      mojo-auth.js webapp loads — login form, OAuth, passkeys, magic link
      Each protected form submission acquires a fresh scoped bouncer_token
      Existing endpoint decorators validate and consume the token
```

### Retry and recovery

Continue accepts touch, mouse, and keyboard activation, with visible focus and
live status. A stationary touch-down and button activation count as interaction;
mouse movement is not required. Missing measurements are treated as unknown,
not measured zero. The hosted slider and its three-miss cooldown are removed;
in-flight slider state and cached misses do not prevent Continue after current
policy checks. An expired check asks for a reload. Connection failures, non-JSON or
invalid responses, and the 8-second request timeout show an error and manual
Retry. After three errors, use Restart or help. A 429 disables assessment Retry
until `Retry-After` (default 60 seconds, bounded to 1–3600 seconds).
None claims success or automatically navigates.

Site cookies must work. Missing cookies show cookie guidance; an embedded
contact page can offer opening the same page in a top-level tab. Unavailable
verification and existing device blocks/frozen sessions retain their restriction
and show recovery guidance. The current shell offers a review form requiring
only an email and optional note, without a pass, cookies, or JavaScript. An
operator-configured external support link provides a fallback during outages.
Old blocked records require operator review, even if their original verdict was
a false positive. Successful checks do not clear reputation or enforcement state.

### Scanner paths

High-risk and signature-matched visitors to hosted `/auth`, `/register`, and
`/contact` now get honest recovery guidance and the review form. They are not
asked to submit credentials to a fake login form.

Requests to `/login`, `/signin`, and any configured `BOUNCER_DECOY_PATHS` receive a
visually identical login page whose form POSTs to a dead endpoint. That endpoint always
returns a plausible-looking error with a realistic delay. Detection is never revealed.
These explicit scanner honeypots, including `/signup`, retain their existing
semantics.

---

## Hosted Assess Protocol

**POST** `/api/account/bouncer/assess`

No authentication required. Rate-limited per IP (60/minute) and returning
`_muid` cookie (30/minute). The hosted renderer supplies an opaque descriptor;
`mojo-hosted-bouncer.js` sends it with `credentials: 'same-origin'` to the page
origin, ignoring `BOUNCER_API_BASE`. This is client routing, not a new Origin
allowlist. The server validates descriptor/cookie/host scope and retains the
legacy CORS policy.

```json
{
  "hosted_gate": {
    "version": 1,
    "descriptor": "<opaque-render-issued-descriptor>",
    "operation": "check",
    "request_id": "<unique-request-id>"
  },
  "signals": {"behavior": {}, "gate_challenge": {}}
}
```

| Field | Contract |
|---|---|
| `version` | Required; supported version is `1` |
| `descriptor` | Required; 32 URL-safe alphanumeric/underscore/hyphen characters, issued by the renderer |
| `operation` | Required; `check`, `confirm`, or `token`; legacy `submit` is accepted as Continue after current policy checks |
| `request_id` | Required; 8–64 alphanumeric/underscore/hyphen characters; reuse for a transport retry of the same submission |
| `answer` | Retired; ignored on legacy `submit` requests |
| `signals` | Optional top-level object; each section value must also be an object |

Challenge descriptors last **5 minutes** and real-page form descriptors last
**30 minutes**. The renderer binds each to the host, returning `_muid`, resolved
group, and purpose (`login`, `registration`, or `public_message`). Request-body
purpose/group fields cannot change that scope. At most eight descriptors are
retained per identity; a new one can evict the oldest. A descriptor is not an
auth token and is not stored in localStorage.

Responses have an explicit `next_action`:

```json
{
  "status": true,
  "data": {
    "decision": "allow",
    "next_action": "check_cookie"
  }
}
```

| `next_action` | Client behavior |
|---|---|
| `check_cookie` | Cookie grant only; send `confirm` on a separate request |
| `allow` | Confirmed pass; navigate once |
| `token` | Use the returned `token` for one protected form request |
| `decoy` | Legacy denied state; current client presents operator recovery |
| `recovery` | Stay on the page and show the reason's recovery guidance |
| `error` | Stay on the page and offer a useful retry |

Initial render configuration may also use `check` for the Continue button.
`check_cookie`, `allow`, and `token` carry `decision='allow'`; unresolved actions
carry `decision='block'`. Branch on the known action, not just `status` or
`decision`. Granting a cookie is insufficient: `confirm` checks the exact
issued pass and current restrictions before returning `allow`. Retries preserve
the original grant timestamp. New responses do not issue slider targets or
cooldowns; the hosted client treats legacy render actions as Continue.

Reasons are `cookies`, `expired`, `restart`, `operator`, `unavailable`, or
`invalid`. Invalid protocol input returns 400; missing cookies/expired state
return 409; inactive group or invalid form scope can return 403; unavailable
state returns 503. The shared rate limiter can return 429. Validate HTTP status,
content type, JSON shape, and action; no failure permits an automatic redirect.
Malformed `hosted_gate` input remains a hosted error and never selects legacy
assessment.
Responses may include a `reference` for the diagnostic row. Recovery/error
responses may also include a signed `review_ticket` for the intake below.
Show the reference with the message. Neither grants access.

### Fresh form tokens

The hosted templates install an optional async provider:

```javascript
MojoAuth.init({
  baseURL: window.location.origin,
  bouncerTokenProvider: MojoHostedBouncer.tokenProvider(formDescriptorConfig),
});

const token = await MojoAuth.getBouncerToken('public_message');
```

`formDescriptorConfig` is the real page's server-rendered config. The hosted
provider sends `operation='token'` and uses its server-bound purpose; the
function argument cannot request another scope. `getBouncerToken(purpose, context)`
always returns a Promise. The optional provider receives `(purpose, context)`;
`context.duid`, when present, carries the protected request's device ID into
token issuance. `login`, `register`, and `startPhoneRegister` await the
provider automatically with `login`, `registration`, and `registration`
respectively. Contact awaits it explicitly with `public_message`. Each
submission, including a credential retry or phone-start followed by register,
gets a fresh single-use token. Provider failure stops the form request.

With this provider, `login`, `register`, and `startPhoneRegister` retry once with
a fresh token only for the exact `403` error `Invalid bouncer token`. That
rejection occurs before credentials/actions are processed. Network failures,
uncertain outcomes, and credential errors are never automatically replayed.
Contact retains its explicit token/submission flow.

Other MojoAuth methods retain their current behavior. Without a provider,
existing clients keep the legacy lookup/request path, and `getBouncerToken()`
resolves the legacy lookup result. Hosted token transport does not use
localStorage. See [Auth Pages](auth_pages.md) for template integration.

### Cookies and deployment compatibility

New hosted passes use `mbp` with a v2 signature and a separate `mbs` HttpOnly
session cookie. Both share domain/path, `SameSite=Lax`, and `Secure` outside
DEBUG. The signature binds the host or configured cookie domain, `_muid`,
`mbs`, and issue time. The pair survives IP changes within the same browser
session; current restrictions still apply. `mbp` defaults to 24 hours, while
`mbs` is a browser-session cookie. Legacy passes keep their original IP-prefix
binding; form tokens still bind to the exact request IP.

Deploy the Python workers, templates, and `mobile-1` scripts to a coherent pool:
old workers cannot validate v2 passes. Preserve the scripts' version query
strings in cache keys and do not cache descriptor-bearing HTML (`no-store`,
`no-referrer`). No migration is needed; descriptor protocol version 1 and
third-party legacy SDK calls remain supported.

---

## Recovery Review API

### Submit a request

**POST `/api/auth/bouncer/recovery`** — public; no authentication, Bouncer pass,
cookies, or JavaScript required. Its independent limit is **30 requests per IP
per 300 seconds**. Use the signed ticket supplied by the hosted page/response;
it is bound to that host and expires 30 minutes after its diagnostic was created.

```json
{
  "review_ticket": "<signed-ticket-from-hosted-page>",
  "email": "visitor@example.com",
  "note": "I cannot continue from the mobile app."
}
```

`email` is required and must be valid, at most 254 characters. `note` is optional,
at most 500 characters; do not include passwords or verification codes. Optional
`reported_failure` accepts `cookies`, `expired`, `restart`, `operator`,
`unavailable`, `invalid`, `limited`, or `network` as client-reported context.

JSON success:

```json
{
  "status": true,
  "data": {
    "reference": "501-0123456789abcdef01234567",
    "review_state": "pending",
    "message": "Your request is recorded for review. This does not grant access. Keep your reference for support."
  }
}
```

Form-encoded POSTs receive an HTML receipt/error page, so the provided form works
without JavaScript. Repeating the ticket preserves the first request and its
current review state. The response uses `Cache-Control: no-store` and
`Referrer-Policy: no-referrer`. Invalid/expired tickets and contact fields return
400, the independent rate limit returns 429, and intake failure returns 503.
JSON failures use `status: false`, `data: {reason, message}` (the rate limiter
uses its standard error envelope). An unavailable diagnostic store provides a
nonce-only log reference without a usable ticket; use external support then.

### Read the operator queue

**GET `/api/auth/bouncer/recovery`** — requires a global User permission of
`view_security`, `manage_security`, or `security`. Group/member grants and API
keys are insufficient.

```text
GET /api/auth/bouncer/recovery
GET /api/auth/bouncer/recovery?before=501
GET /api/auth/bouncer/recovery?reference=501-0123456789abcdef01234567
```

Response: `{"status": true, "data": {"items": [...], "before": 501}}`.
Each request scans at most 2000 rows by descending primary key and returns at
most 50 pending reviews. Follow `before` until null, even if `items` is empty.
The cursor is a positive integer. Exact reference lookup returns one diagnostic
regardless of review state, with `before: null`; malformed/missing references
or invalid cursors return 400.

Items contain `reference`, `signal_id`, `muid`, `msid`, `page_type`, `ip`,
`risk_score`, `triggered_signals`, `created`, and `details`. Restricted `details`
include host/group, user agent, bounded interaction counts, score components,
and `restriction` (reason, signature type/value, device tier, session risk).
`details.review` includes state, email, note, and request time, plus reviewer,
resolution, and review time after closure. Keep this evidence in the operator
interface; do not copy it into public recovery messages.

### Resolve a review

**POST `/api/auth/bouncer/recovery/resolve`** — requires global User
`manage_security` or `security` (no group grants or API keys).

```json
{
  "reference": "501-0123456789abcdef01234567",
  "resolution": "Reviewed the reported visit; recorded the evidence and any targeted corrections."
}
```

`resolution` must be nonblank and at most 1000 characters. Response:
`{"status": true, "data": {"reference": "501-0123456789abcdef01234567", "review_state": "reviewed"}}`.
Invalid reviews/resolutions return 400. The first resolution records the
operator's user ID and time; repeated resolution preserves it. **Reviewed does
not mean unblocked.** This endpoint clears no device, signature, session, or
other restriction and sends no automatic follow-up email.

### Operator remediation

Assign a queue owner and a process for regular review and visitor follow-up.
For each request:

1. Look up the exact reference and inspect its restricted evidence. Read the
   linked `/api/account/bouncer/signal/<signal_id>?graph=detail` and
   `/api/account/bouncer/device?muid=<muid>` history before deciding.
2. Correct only an adjudicated false positive using the existing device or
   signature endpoint: POST `/api/account/bouncer/device/<id>` with the reviewed
   `risk_tier`, or POST `/api/account/bouncer/signature/<id>` with the specific
   `is_active`/`sig_type`/`value` correction and notes. These writes require `manage_users`,
   `manage_security`, `security`, or `users`; preserve valid blocks and history.
3. Have the backend operator call
   `mojo.apps.account.services.bouncer.learner.refresh_sig_cache()` after any
   signature change. A REST edit alone can leave the old cache active for one
   hour.
4. Have the backend operator inspect and explicitly correct only an adjudicated
   Redis `bouncer:session_risk:<muid>` freeze and any application freeze-handler
   effects. Never blanket-flush Redis or all session-risk keys. Device/signature
   edits do not clear this state.
5. Record the resolution, including whether restrictions remain, then have the
   visitor restart for a new descriptor. Old denied descriptors stay denied;
   all current checks still apply. Closing a review is not an access override.

Before production, configure `BOUNCER_RECOVERY_SUPPORT_URL` to a reachable
external support channel for database/intake outages and exempt recovery/static
routes from nginx `auth_request`; Django still protects GET and resolve. See
the [backend runbook](../../django_developer/account/bouncer.md#operator-remediation-runbook).

**Physical iPhone Safari, physical Android Chrome, and the actual Maestro app
WebView still require validation before production.** Check stationary taps,
Wi-Fi/cellular changes, refused cookies/storage, no-JavaScript review, 429 and
timeouts, and retained restrictions after review. Emulated touch is not proof
of physical-device behavior. This milestone adds no camera or dot challenge,
passkey waiver, or automatic restriction override.

---

## Legacy Assess Endpoint

The public `mojo-bouncer.js` SDK and custom clients send behavioral signals
without `hosted_gate`. Their existing API, scoring, learning, and cross-origin
token behavior remain unchanged. The following request/response contract is
separate from the hosted descriptor flow above.

**POST** `/api/account/bouncer/assess`

No authentication required. Rate-limited per IP (60/min) and per session (`_muid` cookie, 30/min) — the muid limit bounds a client that rotates IP but keeps its session (DM-042).

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

No token is returned. A legacy client should display a neutral error state — do not
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

No authentication required. Rate-limited per IP (60/min) and per session (`_muid` cookie, 30/min) — the muid limit bounds a client that rotates IP but keeps its session (DM-042).

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

## Attaching Legacy Bouncer Tokens to Auth Calls

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

Hosted checks set the cookie on `check_cookie` and verify it with `confirm`
before navigating. Hosted pages also recheck restrictions and require its muid
to match the returning `_muid`; a valid cookie does not override a new restriction.
The existing cookie format and TTL (default 24 hours) are unchanged.

For the legacy API:

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

## Implementing a Legacy API Client (Non-mojo-auth.js)

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

## Legacy Token Error Handling

| Scenario | Behavior |
|----------|----------|
| Assess endpoint down | Call login directly without a token; server logs and allows through (fail-open) if `BOUNCER_REQUIRE_TOKEN=False` |
| Token expired before login | Login returns 403 `bouncer_token_invalid`; re-run assess to get a fresh token |
| Token replayed | Login returns 403 `bouncer_token_consumed` |
| IP changed between assess and login | Login returns 403 `bouncer_token_ip_mismatch` |
| No token, enforce mode | Login returns 403 `bouncer_token_required` |

---

## Per-Group Branding (Custom Domain Deployments)

When the platform hosts multiple groups on separate custom domains — or uses
`?group_uuid=<uuid>` to identify a group on a shared domain — the bouncer
automatically applies that group's branding and settings to the auth pages.

### How group detection works

1. **Custom auth domain** — if `auth.clientbrand.com` maps to a group, visiting
   that hostname shows the group's logo, brand name, OAuth buttons, and success
   redirect without any extra URL params.
2. **`?group_uuid=<uuid>`** — for deployments where all groups share a domain,
   pass the group UUID as a query param. The bouncer applies the group's
   settings.

> **Use `group_uuid`, not `group`.** The framework's URL dispatcher reserves
> `?group=` for integer-ID lookup and rejects any non-integer value with
> `400 Invalid group ID` before the bouncer page runs. The bouncer's public
> UUID slot is `group_uuid`.

### OAuth round-trip

The `group_uuid` is embedded in OAuth state so branding survives the provider
redirect. After the provider callback, the page receives
`?code=...&state=...&group_uuid=<uuid>`, which `mojo-auth.js` picks up
automatically via `window._matConfig.groupUuid`.

### Custom auth domain — client-side behavior

When operating on a custom auth domain, no special client-side code is required.
The server resolves the group from the hostname. The `?group_uuid=` param is
preserved in all navigation links (login → register → back) so branding stays
consistent across page transitions even when the hostname is shared.

### Query params preserved through the challenge

When the bouncer serves the challenge page, the following query params from the
original request are forwarded to the post-challenge login redirect:

| Param | Purpose |
|-------|---------|
| `redirect` / `next` / `returnTo` | Post-login redirect URL (relative or absolute) |
| `back` | Override for the "Back to website" hero link |
| `group_uuid` | Group UUID for per-group branding |
| `force_reauth` | Force the credential form instead of accepting an existing session |
| `auth_theme` | Valid hosted-auth layout override |
| `auth_appearance` | Valid hosted-auth appearance override |
| `kind` | Valid contact/support kind on `/contact` only |
| Declared `registration.extra_fields` name | Registration attribution on `/auth` and `/register` only |

An attribution name is eligible only when the resolved auth config declares it;
the legacy server capture setting alone is not enough. Both visible and
`capture_only` extras use the same rule. Values must be a single non-empty
string, at most 512 characters, with no ASCII control character. Empty,
repeated/list-shaped, control-bearing, and oversize values are dropped, never
truncated. Undeclared keys such as `utm_*` are dropped.

Canonical/identity names (`first_name`, `last_name`, `email`, `phone`, `dob`,
`password`, `username`, `phone_number`); tenancy, navigation, and display names
(`group`, `group_uuid`, `redirect`, `next`, `returnTo`, `back`,
`webapp_base_url`, `redirect_uri`, `force_reauth`, `auth_theme`,
`auth_appearance`); credential/device names (`token`, `code`, `state`,
`auth_code`, `bouncer_token`, `verified_phone_token`, `session_token`,
`mfa_token`, `access_token`, `refresh_token`, `recovery_code`,
`current_password`, `new_password`, `duid`, `muid`, `fp`); and OAuth/passkey
names (`client_id`, `response_type`, `scope`, `code_challenge`,
`code_challenge_method`, `code_verifier`, `grant_type`, `resource`,
`challenge_id`, `credential`) cannot be declared as extras. Values are
URL-encoded and cannot change the fixed `/auth` or `/register` destination.
Registration extras do not propagate to `/passkey`, `/contact`, or
OAuth-consent destinations.

---

## Bouncer Token Error Codes

When `BOUNCER_REQUIRE_TOKEN=True` and token validation fails:

```json
{
  "status": false,
  "code": 403,
  "error": "Invalid bouncer token"
}
```

| `error` value | Cause |
|---------------|-------|
| `Bouncer token required` | No `bouncer_token` field in request |
| `Invalid bouncer token` | Invalid signature/structure, expired or consumed token, wrong IP/device, or wrong endpoint scope |

Detailed reasons remain in security events. The hosted provider's single retry
matches only the exact invalid-token error and `code: 403`; do not infer a
credential failure or automatically replay other errors.

---

## Admin Visibility APIs

Three REST endpoints provide full admin visibility into bouncer activity. Use these to build security dashboards, investigate bot attacks, and manage bot signatures.

**Read permissions:** `manage_users`, `view_security`, `manage_security`,
`security`, or `users`. Device/signature writes require `manage_users`,
`manage_security`, `security`, or `users`. These groupless models require global
grants and deny API keys. Recovery queue/resolve use the narrower permissions
documented above.

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

`BouncerSignal` is a **read-only** audit trail. Legacy assessments include signal
payloads; hosted check/submission outcomes use `decision='log'`, empty
`raw_signals`, and `server_signals.hosted_gate.action`. Hosted outcomes do not
promote incidents, train signatures, or increase the device's risk tier.
Generic signal graphs omit `server_signals.hosted_gate.review`; contact email,
notes, and resolution are available only from the global-security recovery
queue. Raw `server_signals` cannot be used for filtering, ordering, or
aggregation to infer that private data.

Hosted diagnostic writes are bounded independently of page access (default
300 per IP and 3000 globally per five minutes, configurable by the operator).
If that budget or storage is unavailable, the page still renders a reference
and configured support link, but has no review ticket/form. A budget-exhausted
reference is display-only, not a stored review. Previously issued tickets are
still usable.

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

Active signature matches and existing blocked devices produce honest
operator-recovery guidance on hosted pages. Device history alone is not evidence
that a visitor has just triggered a current signature.

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
This is the legacy assessment/event pipeline. Hosted recovery outcomes use the
neutral audit path described above and do not feed these rules.

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
| `security:bouncer:block` | 8 | Yes | Score >= 80, **3 in 30min**: block IP 1hr |
| `security:bouncer:honeypot_post` | 9 | Yes | Block IP 1hr on the first event |
| `security:bouncer:campaign` | 10 | Yes | Block IP 24hr + notify admin, on the first event |
| `security:bouncer:token_invalid` | 4 or 7 | Yes | **10 level-7 events in 30min**: block IP 30min |
| `security:bouncer:session_freeze` | 9 | Yes | **3 in 60min**: block IP 24hr + notify admin |
| `security:bouncer:monitor` | 5 | No | — |
| `security:bouncer:event` | 5–7 | Conditional | — |
| `security:bouncer:token_missing` | 6 | No | — |

`security:bouncer:token_invalid` is reported at **level 4** when the failure is a normal part of the token lifecycle — `expired` (the 15-minute TTL ran out while the user read the page), `nonce_consumed` (a double-submitted form), or `ip_mismatch` (a cellular or CGNAT handoff changed the egress IP). Only tampering — `invalid_format`, `invalid_signature`, `page_type_mismatch`, `duid_mismatch` — reports at level 7 and can reach the blocking rule. While the deployment is in log-only mode (`BOUNCER_REQUIRE_TOKEN` off) every cause is capped at level 4, so nothing gets blocked.

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
| `bouncer:hosted:<action>` | Neutral hosted outcomes, such as `check_cookie`, `allow`, and `recovery`; historical slider/cooldown metrics may remain |

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

---

## Embedding on Static Pages or Separate-Origin SPAs

The bouncer ships two embeddable JS files. Both are served from the bouncer
host at `/account/static/`.
These keep the legacy API/SDK contract, including existing failure behavior.
The hosted-only `mojo-hosted-bouncer.js` descriptor client is separate and is
not a replacement for cross-origin embeds.

### mojo-bouncer.js — one-shot gate

For pages with a sensitive action (form submit, deposit, RG-limit change).
Pops a brief verification gate, collects environment + behavior signals,
calls `/api/account/bouncer/assess`, and issues a `bouncer_token` for the
next request.

```html
<!-- Same-origin (page served from the bouncer host) -->
<script src="/account/static/mojo-bouncer.js"
        data-page-type="login"
        defer></script>

<!-- Cross-origin (page served elsewhere, bouncer at auth.example.com) -->
<script src="https://auth.example.com/account/static/mojo-bouncer.js"
        data-api-base="https://auth.example.com"
        data-page-type="login"
        data-logo-url="/logo.svg"
        data-brand="MyApp"
        defer></script>
```

After the gate completes:

```js
var token = window._mojoBouncerInstance.getToken();
var duid  = window._mojoBouncerInstance.getDuid();

await fetch('/api/login', {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password, bouncer_token: token, duid }),
});
```

### mojo-sentinel.js — continuous monitoring

For long-lived pages (lobbies, dashboards, in-game wrappers). No UI; runs
in the background streaming behavioral signals to
`/api/account/bouncer/event`.

```html
<script src="https://auth.example.com/account/static/mojo-sentinel.js"
        data-api-base="https://auth.example.com"
        data-page-type="gameplay"
        data-context="game:roulette"
        defer></script>
```

| Attribute | Default | Purpose |
|---|---|---|
| `data-api-base` | same-origin | Bouncer host (no trailing slash) |
| `data-page-type` | `"embed"` | Stored on every event; used by server-side analyzers to scope |
| `data-context` | `""` | Free-form app-side correlation string |
| `data-flush-interval-ms` | `15000` | Periodic batch flush interval |
| `data-flush-size` | `25` | Force flush when buffer hits this size |

Both scripts include `credentials: 'include'` on every fetch so the HttpOnly
`mbp` pass cookie sets cross-origin. **This works from any origin by default** —
the server echoes any well-formed `http(s)` origin back with credentials on
`/assess`, `/event` and `/message`, so you do not need to be listed anywhere and
there is nothing to request from the backend team.

Two standing exceptions, in every configuration: `/verify_pass` and the
permission-gated admin endpoints are not covered (no browser client calls them),
and `Origin: null` — a sandboxed iframe, `data:` or `file://` page — is refused.

The one case that needs backend coordination is a server that has **opted out**
with `BOUNCER_ALLOW_ANY_ORIGIN = False`. There, only origins listed in
`BOUNCER_ALLOWED_ORIGINS` get credentialed headers, and yours must be added.

If your origin is not covered, the server answers with
`Access-Control-Allow-Origin: *`, and the browser rejects your
`credentials: 'include'` fetch **before your page sees the response**.
`mojo-bouncer.js` treats that as a failure and calls `_allowThrough()` — so the
gate silently becomes a no-op rather than throwing. If your gate appears to pass
instantly on a tenant domain, this is the first thing to check.

Two things to know either way. A change to this setting is not immediate for
clients that have already preflighted: `Access-Control-Max-Age: 86400` means a
browser may act on a cached preflight decision for up to 24 hours. And the `mbp`
pass cookie is `SameSite=Lax`, so a page on a *different registrable domain* from
the bouncer host never stores it — the token flow works cross-site; the
repeat-visit challenge skip does not.

**Identity continuity** — both scripts share the `mojo_device_uid`
localStorage key with `mojo-auth.js`, so a user authenticated via the gate
keeps the same device identity throughout the session. Cross-origin embeds
are best-effort: localStorage is per-origin, so a SPA at `client.com`
calling sentinel from `auth.example.com` generates its own duid; the server
stitches by fingerprint when available.

---

## MojoSentinel.observe API

The host page pushes app-specific events into the telemetry stream:

```js
MojoSentinel.observe('deposit_open', {
    amount: 50.00,
    method: 'card',
});

MojoSentinel.observe('game_action', {
    action: 'spin',
    reaction_ms: 142,
    bet: 25,
});

MojoSentinel.observe('rg_limit_change_attempt', {
    field: 'daily_loss_limit',
    new_value: 500,
});
```

| Argument | Type | Notes |
|---|---|---|
| `category` | string | Short event-type slug. Becomes `raw_signals.event_type` on the `BouncerSignal` row server-side. |
| `payload` | object | Flat dict of primitives — int/float/string/bool. The framework's universal analyzers read certain keys (e.g. `target_tag`, `reaction_ms`); apps can include any extras they want. |

Additional public methods:

| Method | Notes |
|---|---|
| `MojoSentinel.flush()` | Force an immediate batch flush outside the normal interval. Useful before a navigation or form submit. |
| `MojoSentinel.getDuid()` | Read the shared `mojo_device_uid` value that sentinel is using. |

The event is buffered with the next periodic batch. Failures are silent —
the host page is never affected by a slow or unreachable bouncer endpoint.

Recommended categories:
- `game_action` — single in-game action (bet, click, move)
- `nav_event` — page-level navigation a router does
- `form_event` — form-level interaction (open, validate-fail, abandon)
- `api_call` — outbound API call from the SPA
- `error` — caught JS error

The framework doesn't enforce a category vocabulary — pick names that match
how your stream analyzers look up data.

---

## nginx Drop-in Protection

Any nginx-served location can be gated through the bouncer in three lines
of config plus one include. The mojo repo ships the shared include +
worked example as commitable artifacts:

```
docs/web_developer/account/nginx/mojo-bouncer.conf
docs/web_developer/account/nginx/example-protected-site.conf
```

### Install

Copy `mojo-bouncer.conf` into your nginx `conf.d/` directory (or somewhere
on the include path).

On the auth host, `/api/auth/bouncer/recovery` (including `/resolve`) and needed
`/api/account/static/` assets must bypass nginx `auth_request`. Django still
requires global security permissions for queue GETs and resolve POSTs; public
intake validates its signed host-bound ticket. Keep the hosted challenge pages
and assessment route reachable as well. The supplied include gates only the
locations where it is explicitly enabled; if your auth host inherits a server
gate, add `auth_request off` exceptions using its normal Django upstream. See
the [auth-host example](../../django_developer/account/auth_pages.md#2-nginx--static-assets--favicon).

### Use

In any `server { }` block:

```nginx
server {
    listen 443 ssl;
    server_name app.example.com;

    set $mojo_bouncer_host  "auth.example.com";
    set $mojo_bouncer_login "/auth";
    include conf.d/mojo-bouncer.conf;

    location /vip/ {
        auth_request /_mojo_bouncer_check;
        error_page 401 = @mojo_bouncer_redirect;
        try_files $uri $uri/ /vip/index.html;
    }
}
```

### What happens

1. Request to `/vip/foo` triggers an internal subrequest to
   `GET /api/account/bouncer/verify_pass` on the bouncer host.
2. verify_pass first consults the Redis signature cache — known-bot IPs/UAs
   get 401 with `X-Bouncer-Reason: signature` at the edge.
3. Otherwise, the pass is validated (`mbp` plus `mbs` for v2; legacy IP binding
   for old passes). 200 if valid, 401 if not.
4. nginx serves `/vip/foo` on 200; redirects to the bouncer challenge page
   (with `?redirect=` set to the original URL) on 401. After the user
   passes the challenge, the bouncer redirects them back, this time
   carrying the valid pass cookies, and the next nginx pass succeeds if no
   signature restriction remains.

### Required deployment shape

Both `mbp` and the hosted `mbs` session cookie must reach the bouncer host AND
the protected nginx host. `BOUNCER_PASS_COOKIE_DOMAIN` applies to both, and the
include forwards the full Cookie header. Two shapes work:

| Shape | Static / app host | Bouncer host | Configuration |
|---|---|---|---|
| A | `example.com/protected` | `example.com/auth` | natural — same host |
| B | `app.example.com` | `auth.example.com` | set `BOUNCER_PASS_COOKIE_DOMAIN='.example.com'` on the bouncer's Django settings |

Cross-domain deployments (e.g. `marketing.com` gated by `auth.example.com`)
are not supported for nginx `auth_request` gating — browser cookie policy
blocks the `mbp` share, and the permissive cross-origin default does not change
that. `BOUNCER_ALLOW_ANY_ORIGIN` only affects CORS response headers on the public
JSON endpoints; the `mbp` cookie is `SameSite=Lax` and `verify_pass` is never
covered by the any-origin echo (nginx `auth_request` is server-to-server and
ignores CORS anyway). A tenant-owned domain can therefore use the **token** flow
cross-origin, but not the cookie-gated one.

### Capacity & latency

`verify_pass` is sub-50ms (Redis lookup + HMAC verify). The 2s proxy
timeouts in the shipped include are conservative defaults — sized so a
slow bouncer host fails closed (returns 401 → redirect to challenge)
rather than serving uncatchable 504s.
