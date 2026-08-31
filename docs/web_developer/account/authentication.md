# Authentication — REST API Reference

## Login

**POST** `/api/login`

```json
{
  "username": "alice@example.com",
  "password": "mysecretpassword"
}
```

| Field | Required | Description |
|---|---|---|
| `username` | yes | Email, phone number, or username. |
| `password` | yes | User-supplied password. |
| `group_uuid` | optional | Operator/group UUID. When supplied, `request.group` middleware uses it to resolve multi-tenant context and `USER_LOGIN_HANDLER` receives the group. Best-effort: an unrecognized uuid falls back to other resolvers instead of returning an error. |

Phone-number identity is accepted in the `username` field as well — the server resolves the channel automatically and `USER_LOGIN_HANDLER` receives `source="password"` regardless of which identifier was used.

From JavaScript, pass `group_uuid` via the third `options` arg on the SDK:

```javascript
MojoAuth.login(username, password, { group_uuid: '<uuid>' });
```

Two-arg `MojoAuth.login(username, password)` callers omit the key entirely and behave identically to single-tenant deployments. The bouncer-hosted login page (`/auth`) forwards `group_uuid` automatically when the page resolves a group via custom auth domain or `?group_uuid=` — see [Auth Pages § Per-Group Branding](auth_pages.md#per-group-branding).

**Response:**

```json
{
  "status": true,
  "data": {
    "access_token": "eyJhbGci...",
    "refresh_token": "eyJhbGci...",
    "user": {
      "id": 42,
      "username": "alice@example.com",
      "display_name": "Alice"
    }
  }
}
```

Store both tokens. Use `access_token` in subsequent requests.

## Login Rate Limiting

`POST /api/login` applies layered throttling. A 429 with `Retry-After` can arrive from several independent tiers:

- **IP limit** — 100 attempts per 60 seconds per source IP. Shared across all clients on the same network.
- **Per-client limit** — 10 attempts per 5 minutes keyed on a server-set cookie. Cannot be bypassed by changing the `duid` parameter.
- **Per-account limit** — 10 failed attempts per 15 minutes for a single account (resolved by username). Rotates the IP or device to a new address does not reset this counter.

When the per-account limit is hit, the response is:

```json
{
  "status": false,
  "code": 429,
  "error": "Rate limit exceeded"
}
```

With a `Retry-After` header indicating seconds until the window resets.

**Client guidance:**

- Always read `Retry-After` and surface a generic "Too many attempts, try again in X minutes" message. Do not say which specific tier triggered the block.
- Do not retry automatically on 429 — wait for the indicated interval.
- The per-account counter is cleared on a successful login, so one legitimately mistyped password does not cause a prolonged lockout.

MFA verify endpoints (`POST /api/auth/totp/verify`, `POST /api/auth/passkeys/login/complete`, etc.) have their own separate IP-level rate limit (10 requests per 60 seconds by default).

## Token Storage (UI Guidance)

For this Bearer-token API, a practical default is:

- Store `access_token` in `localStorage`
- Store `refresh_token` in `localStorage`

Example keys:

```text
mojo_access_token
mojo_refresh_token
```

Recommended for higher-security deployments: move refresh token handling to secure `HttpOnly` cookies. That requires backend/session design changes; the flow below assumes token storage in `localStorage`.

## Authenticating Requests

Include the access token in every authenticated request:

```
Authorization: Bearer <access_token>
```

## Group-Scoped Tokens (`grouptoken`)

Some deployments hand a page a **group-scoped token** instead of a JWT — a
credential that authenticates as you but is capped at **one group**:

```
Authorization: grouptoken gt1.<payload>.<signature>
```

It is opaque: do not parse it, and do not feed it to any JWT helper — it is not
a JWT and `atob`/`jwt-decode` will fail on it.

**What works**

- `GET /api/user/me` — read-only self, so the usual boot check still works.
- Any group-scoped resource in the token's own group, subject to your
  membership permissions **in that group**.

**What is refused (403)** — this is by design, not a bug to work around:

- anything in another group, including a **child** of the token's group, and any
  `group=` / `group_uuid=` parameter naming a different group;
- `GET /api/group` (always an empty list) and `GET /api/group/<pk>` (always
  403 — including the token's own group). Read tenant branding from the auth
  config endpoints instead;
- every write to the account: `POST /api/user/me`, password change, email or
  phone change, `revoke_sessions`;
- every credential endpoint: passkeys, TOTP setup/confirm/recovery codes, user
  API keys;
- `POST /api/auth/handoff` — a group token cannot be traded up.

**No refresh.** `POST /api/refresh_token` does not accept one, and neither does
`Authorization: Bearer`. When the token expires, obtain a new one from whatever
flow issued it — do not run your JWT refresh loop against it.

**401 semantics.** Every rejection is
`401 {"error": "Invalid group token"}` — expiry, a bad signature, a revoked
group, a removed membership and a deactivated account are deliberately
indistinguishable. The single exception is
`401 {"error": "Group token expired"}`, which is your cue to re-mint rather than
send the user through a full re-authentication.

A deployment that has not registered the scheme answers
`401 {"error": "Invalid token type: grouptoken"}`.

### Where one comes from: a gated handoff destination

A deployment can declare your app's host **gated**. When it does,
`POST /api/auth/exchange` answers with a **different shape** — no
`refresh_token` key at all:

```json
{
  "status": true,
  "data": {
    "access_token": "gt1.…",
    "token_type": "grouptoken",
    "expires_in": 3600,
    "group": { "id": 7, "uuid": "…", "name": "Tenant A" },
    "user": { "...": "the same basic user object a JWT login returns" }
  }
}
```

Branch on the token itself, not on a stored flag: **a token starting `gt1.` is
always a group token.** `mojo-auth.js` does this for you —
`MojoAuth.getTokenType()` returns `"grouptoken"` or `"bearer"`,
`getAuthHeader()` emits the right scheme per call, `saveTokens()` **clears any
stored refresh token when a response carries none**, and `refreshToken()` /
`requestHandoffCode()` reject immediately under a group session.

**Expiry: re-bounce, do not refresh.** A `gt1.` payload carries no `exp` claim,
so `expires_in` at exchange time is the only lifetime you get — store it as an
absolute deadline (`mojo-auth.js` writes `token_expires_at`, and
`MojoAuth.isTokenExpired()` reads it; a missing or unparsable value counts as
expired). On expiry, navigate back to the auth origin with
`?redirect=<current url>` and let the handoff issue a fresh one. There is no
refresh path and adding one would defeat the point of the credential.

**`403` at handoff or exchange.** Two cases, both deliberate and neither
retryable by the client:

- `403` from `POST /api/auth/handoff` — *"You are not a member of the group
  that owns this destination"*. The visitor authenticated fine; they simply
  are not a member of your tenant. **Superusers land here too** — a superuser
  can never hold a group token, so they cannot sign into a gated destination
  through this flow at all.
- `403` from `POST /api/auth/exchange` — membership, group state or account
  state changed between the mint and the exchange. Every guard is re-run at
  exchange, and a refusal **never** falls back to issuing a JWT.

**OAuth is refused for gated destinations.** If your app runs its **own** OAuth
callback page, `GET /api/auth/oauth/<provider>/begin` and
`POST /api/auth/oauth/<provider>/complete` answer
`400 {"error": "redirect_uri is not on the allowlist"}` once gating enforces —
the completion endpoint can only produce a full JWT pair, so it refuses rather
than hand one to a gated origin. Move that flow to the hosted auth pages, which
already offer Google/Apple/GitHub and already route back through the gated
handoff.

## Refreshing a Token

**POST** `/api/refresh_token`

```json
{
  "refresh_token": "eyJhbGci..."
}
```

**Response:**

```json
{
  "status": true,
  "data": {
    "access_token": "eyJhbGci...",
    "refresh_token": "eyJhbGci..."
  }
}
```

Refresh before the access token expires. The refresh token itself has a longer TTL (typically 7 days).

## App Boot / Page Reload Session Check

On every app load:

1. Read `mojo_access_token` and `mojo_refresh_token` from `localStorage`.
2. If no access token exists, treat as logged out.
3. If access token exists, call `GET /api/user/me` with `Authorization: Bearer <access_token>`.
4. If `/api/user/me` succeeds, user session is active.
5. If `/api/user/me` fails with auth error and refresh token exists, call `POST /api/refresh_token`.
6. If refresh succeeds, save new tokens and retry `/api/user/me`.
7. If refresh fails, clear stored tokens and send user to login.

Pseudo-flow:

```javascript
const access = localStorage.getItem("mojo_access_token");
const refresh = localStorage.getItem("mojo_refresh_token");

if (!access) return loggedOut();

let me = await api.get("/api/user/me", access);
if (me.ok) return loggedIn(me.data);

if (!refresh) {
  clearTokens();
  return loggedOut();
}

const refreshed = await api.post("/api/refresh_token", { refresh_token: refresh });
if (!refreshed.ok) {
  clearTokens();
  return loggedOut();
}

localStorage.setItem("mojo_access_token", refreshed.data.access_token);
localStorage.setItem("mojo_refresh_token", refreshed.data.refresh_token);
me = await api.get("/api/user/me", refreshed.data.access_token);
if (!me.ok) {
  clearTokens();
  return loggedOut();
}
return loggedIn(me.data);
```

## Cross-Origin Auth Handoff

Used when the auth page and the consuming app live on **different origins** —
`localStorage` is partitioned by origin, so the destination can't read tokens
minted at the auth origin. The handoff is an authorization-code flow:

1. Auth page mints a one-time code and appends it to the redirect URL.
2. App page reads the code from the URL and exchanges it for tokens.

**Step 1 — Mint a code (auth origin, authenticated)**

**POST** `/api/auth/handoff`

```json
{
  "redirect_uri": "https://app.example.com/dashboard"
}
```

| Field | Required | Notes |
|---|---|---|
| `redirect_uri` | **optional by default, required where the server enforces** | The absolute URL the code will be handed to. **Always send it.** |

**Response:**

```json
{
  "status": true,
  "data": {
    "code": "f9a4...e2",
    "expires_in": 60
  }
}
```

Whether the destination is checked is a server-side opt-in — see
[below](#destination-enforcement-is-a-server-side-opt-in). The auth-page JS
calls this automatically when `?redirect=` points to a different origin, so apps
usually don't call it directly. When the mint is refused, the auth page shows an
error and **does not navigate**. Rate-limited to 30 requests/IP.

**Step 2 — Exchange the code (app origin, public)**

**POST** `/api/auth/exchange`

```json
{
  "code": "f9a4...e2"
}
```

Returns the same `data` shape as `/api/login` (access/refresh tokens, user
dict). Codes are single-use and expire after `AUTH_HANDOFF_CODE_TTL` seconds
(default 60). Rate-limited to 20 attempts/min/IP.

**Bootstrap helper**

```javascript
MojoAuth.init({ baseURL: 'https://auth.example.com' });
MojoAuth.handleAuthCodeFromURL().then(function (data) {
  if (data) {
    // tokens stored, URL scrubbed of ?auth_code=
  }
});
```

`handleAuthCodeFromURL()` reads `?auth_code=` from `location.search`, calls
`/api/auth/exchange`, stores the tokens, and replaces the URL with the param
removed. Resolves to `null` if no `auth_code` is present.

### Destination enforcement is a server-side opt-in

Whether `/api/auth/handoff` checks `redirect_uri` against an allowlist depends
on the deployment, and it is **off unless the operator turned it on**
(`AUTH_HANDOFF_ALLOWED_URLS` or `AUTH_HANDOFF_RESOLVER` in the server settings):

| Server state | `redirect_uri` | Destination not allowed |
|---|---|---|
| Neither setting configured — **the default** | optional | the code is **minted anyway**; the server files an internal incident naming the destination |
| Either setting configured | **required** (`400` when missing) | `400`, **no code minted** |

**Send `redirect_uri` on every call regardless of what the server does today.**
It is forward-compatible — the same client keeps working when the deployment
turns enforcement on — and it is what makes the server's audit trail and its
monitor-mode incidents useful. A client that omits it works now and breaks the
day an operator adds the setting.

The destination is decided at mint time, before a credential exists, and is
never re-checked at exchange time. `/api/auth/exchange` is unaffected by any of
this.

**The two `400` shapes**, both only reachable on a deployment that enforces:

```json
{
  "status": false,
  "code": 400,
  "error": "redirect_uri is required for auth handoff"
}
```

```json
{
  "status": false,
  "code": 400,
  "error": "redirect_uri is not permitted for auth handoff"
}
```

Treat either as "do not navigate to the destination" — there is no code, and
sending the user on without one just lands them logged out. If your origin is a
legitimate destination that gets refused, the operator has to add it to the
server's allowlist; there is nothing a client can do.

See [Auth Pages — Cross-Origin Redirect Handoff](auth_pages.md#cross-origin-redirect-handoff)
for end-to-end flow and security trade-offs.

## Logout

On logout, always remove every token key you store. The example key names in
[Token Storage](#token-storage-ui-guidance) are generic UI guidance — pick your
own:

```javascript
localStorage.removeItem("mojo_access_token");
localStorage.removeItem("mojo_refresh_token");
```

If you use `mojo-auth.js`, call `MojoAuth.logout()`. It clears the **four** keys
that library actually uses — `access_token`, `refresh_token`, `token_type` and
`token_expires_at`. The last two carry the kind and deadline of a
[group-scoped token](#group-scoped-tokens-grouptoken); a stale marker or
deadline left behind outlives the session it described.

### Optional: make the sign-out show up in security history

Clearing local storage leaves no server-side record, so an ordinary sign-out
never appears on the user's Security page. A client may report it:

```
POST /api/account/security-events/logout
Authorization: Bearer <access_token>
```

**This is a record, not a revocation.** It writes one `sessions:logout` row and
nothing else — no token is invalidated, `auth_key` is not rotated, and no other
device is affected. Use `POST /api/auth/sessions/revoke` when you actually need
to invalidate other sessions.

Fire it *before* clearing storage (it needs the token), ignore the result, and
clear regardless — the local sign-out must never depend on the call succeeding.
See [User Self-Management § Record a browser sign-out](user_self_management.md#record-a-browser-sign-out).

## Security Notes

- Do not store `mfa_token` long-term; it is short-lived and only for MFA completion.
- Any XSS issue can expose tokens in `localStorage`, so enforce strong frontend security:
  - strict CSP
  - output escaping/sanitization
  - dependency hygiene
- **Revoke all sessions:** `POST /api/auth/sessions/revoke` rotates `auth_key`, immediately invalidating every outstanding JWT. No `current_password` required — ownership is the authenticated session; when `FRESH_AUTH_WINDOW` is enabled a recent login is required instead (see [Step-Up Auth](step_up_auth.md)). Returns a fresh JWT for the calling session so the user stays logged in. See [User Self-Management § Sessions & Devices](user_self_management.md#8-sessions--devices).
- **Email change also rotates `auth_key`** — after a successful email change confirm, all other sessions are invalidated as a side effect.
- **Security events feed:** `GET /api/account/security-events` returns auth-relevant audit events (logins, failed passwords, MFA events, email/phone changes, session revokes, etc.) scoped to the authenticated user. No special permission required. See [User Self-Management § Security Events](user_self_management.md#15-security-events).
- **`login`** — written for every real authentication, for the account whose credentials were verified. A blocked, unfinished or refused attempt writes nothing, and neither does a silent refresh or a re-issue from session revoke / email change.
- **`sessions:logout`** — written only when a client posts `/api/account/security-events/logout`. It is a note in the history, not a revocation.

## Get Current User

**GET** `/api/user/me`

Returns the profile of the authenticated user.

```json
{
  "status": true,
  "data": {
    "id": 42,
    "username": "alice@example.com",
    "email": "alice@example.com",
    "display_name": "Alice",
    "permissions": {"manage_reports": true},
    "is_active": true,
    "requires_mfa": false,
    "has_passkey": false
  }
}
```

## Password Reset — Code Method

**Step 1: Request reset code**

**POST** `/api/auth/forgot`

```json
{
  "email": "alice@example.com",
  "method": "code"
}
```

The identifier can be supplied as `email`, `phone`, or `username`. A 6-digit code is dispatched via email by default. Pass `"channel": "sms"` to route the code via SMS instead; the server also routes via SMS automatically when the matched account has no email on file. Response always returns success (to prevent account enumeration).

**Step 2: Submit code and new password**

**POST** `/api/auth/password/reset/code`

```json
{
  "email": "alice@example.com",
  "code": "483921",
  "new_password": "newpassword123"
}
```

Returns a JWT on success (automatically logs the user in).

## Password Reset — Link Method

**Step 1: Request reset link**

**POST** `/api/auth/forgot`

```json
{
  "email": "alice@example.com",
  "method": "link"
}
```

A reset link with a signed token is emailed.

**Step 2: Submit token and new password**

**POST** `/api/auth/password/reset/token`

```json
{
  "token": "<token-from-email>",
  "new_password": "newpassword123"
}
```

Returns a JWT on success.

## Admin Password Reset (for another user)

Admins with `manage_users` can set any user's password directly:

**POST** `/api/user/<target_id>`

```json
{
  "new_password": "NewPass##123"
}
```

No `current_password` needed. No forgot-password email is sent — the password is changed immediately. Password strength validation still applies.

See also [User API — Admin Password Reset](user.md#admin-password-reset-for-another-user).

---

## Login with MFA Enabled

If the account has TOTP or SMS MFA enabled, the login response is different — a short-lived `mfa_token` is returned instead of a JWT:

```json
{
  "status": true,
  "data": {
    "mfa_required": true,
    "mfa_token": "a3f1c9d2...",
    "mfa_methods": ["totp", "sms"],
    "expires_in": 300
  }
}
```

`mfa_methods` lists the available second factors for this account. Complete the login using the relevant endpoint:

- TOTP (authenticator app) → see [TOTP / Authenticator App](mfa_totp.md)
- SMS OTP → see [SMS OTP](mfa_sms.md)

## Registration

**POST** `/api/auth/register`

Creates a new user account. No authentication required. Rate-limited to 5 requests per IP per 5 minutes.

**Prerequisite:** The server setting `ALLOW_USER_REGISTRATION` must be `True` (default is `False`). If disabled, this endpoint returns 403.

### Request

```json
{
  "email": "alice@example.com",
  "password": "mysecretpassword",
  "first_name": "Alice",
  "last_name": "Smith",
  "group_uuid": "abc123",
  "bouncer_token": "<token-from-assess>",
  "duid": "browser-generated-device-uuid"
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `email` | conditional | Required when `email` is in the server's `AUTH_REGISTER_FIELDS` schema (default config). Unique across all accounts. |
| `phone` | conditional | Required when `phone` is in the server's `AUTH_REGISTER_FIELDS` schema. Normalized to E.164 server-side. Unique across all accounts. |
| `password` | yes | Must meet password strength requirements |
| `first_name` | conditional | Required when configured as such in `AUTH_REGISTER_FIELDS` |
| `last_name` | conditional | Required when configured as such in `AUTH_REGISTER_FIELDS` |
| `dob` | conditional | ISO `yyyy-mm-dd`. Required when configured. Age-gated against `AUTH_MIN_AGE_YEARS` if that setting is set. |
| `verified_phone_token` | conditional | Required when the schema marks `phone` with `verify: "sms"`. Obtain via the two-step phone verify endpoints below. |
| `group_uuid` | conditional | UUID of a group to associate the new user with. Required when the server has `REQUIRE_GROUP_ON_REGISTRATION = True` |
| `bouncer_token` | conditional | Required when bouncer is active. Must be a token issued with `page_type: "registration"` — a login-scoped token will be rejected |
| `duid` | conditional | Device UUID, required when bouncer is active |

Additional keys may be included in the payload. The server silently drops any key not allowlisted (the global `REGISTRATION_EXTRA_FIELDS` setting, plus any names the group declares in `registration.extra_fields`), so it's safe to forward extra fields without causing errors. `MojoAuth.register()` forwards the full payload as-is.

**Extra registration fields (promo / referral / tracking).** A group can configure extra fields via `registration.extra_fields` (see the backend Auth Pages doc). On the bouncer-hosted register page these are captured automatically: a matching URL query param (e.g. `/register?promo=WELCOME100`) is captured silently; otherwise the page asks for the value as a plain text input. SPAs implementing their own form just include the key in the register payload. Captured values are stored on the new user under `metadata.registration` (a `name → value` map) and passed to the server's registration handler.

### Phone-Based Registration (verify-then-register)

When the server's `AUTH_REGISTER_FIELDS` schema marks `phone` with `verify: "sms"`, the client must complete a two-step phone verification before calling `/api/auth/register`. The bouncer-hosted register page does this automatically; SPAs implementing their own form follow the same pattern.

**Step 1 — Start verification**

**POST** `/api/auth/phone/register/start`

```json
{ "phone": "+14155550123" }
```

Response:

```json
{ "status": true, "data": { "session_token": "<32-hex>", "expires_in": 600 } }
```

The server sends a 6-digit code via SMS. Rate-limited per IP (5 requests / 300s).

If the transport did not accept the message the call returns **503** with `{"status": false, "code": 503, "error": "Unable to send the text message right now. Please try again in a few minutes."}` (retryable); if the provider rejected the number itself it returns **400** with `{"status": false, "code": 400, "error": "This phone number cannot receive text messages."}` (retrying the same number will not help). Neither returns a `session_token` — restart at step 1. Provider error text and codes never reach the client.

**Step 2 — Verify**

**POST** `/api/auth/phone/register/verify`

```json
{ "session_token": "<32-hex>", "code": "123456" }
```

Response:

```json
{ "status": true, "data": { "verified_phone_token": "<32-hex>", "expires_in": 600 } }
```

A wrong code returns **400** but does **not** invalidate the session — resubmit the correct code on the **same** `session_token` until it succeeds or the session expires (`expires_in`). Only a successful verification consumes the session; repeated attempts are bounded by the per-IP rate limit.

The returned `verified_phone_token` is single-use on a successful registration. Include it (and the same `phone`) in the subsequent `/api/auth/register` POST. The server consumes the token, marks `is_phone_verified=True`, and creates the User row in a single transaction.

**Retry behavior on failure:** if `/api/auth/register` fails (e.g. a server-side registration handler raises an error), the token is automatically restored and remains valid. You can retry the same `/api/auth/register` call with the same `verified_phone_token` — there is no need to re-verify the phone. The token is consumed for good only when registration succeeds.

**Dev bypass** — when the server has `AUTH_PHONE_VERIFY_DEV_BYPASS_CODE` set (development environments only), that fixed code is accepted in place of the real SMS code. This setting must never be set in production. It is file-only: it can only be armed from the server's static config, never through the settings API.

From JavaScript:

```javascript
const { session_token } = await MojoAuth.startPhoneRegister(phone);
// user enters the 6-digit code from SMS
const { verified_phone_token } = await MojoAuth.verifyPhoneRegister(session_token, code);
await MojoAuth.register({
  first_name, last_name, phone, dob, password,
  verified_phone_token,
});
```

### Response — Auto-Login (default)

When `REQUIRE_VERIFIED_EMAIL` is `False` (default), the user is logged in immediately. A verification email is sent as a nudge when the user has an email address on file (phone-only registrations skip this).

```json
{
  "status": true,
  "data": {
    "access_token": "eyJhbGci...",
    "refresh_token": "eyJhbGci...",
    "user": {
      "id": 43,
      "username": "alice@example.com",
      "display_name": "Alice"
    }
  }
}
```

### Response — Verification Required

When `REQUIRE_VERIFIED_EMAIL` is `True`, no JWT is issued. The user must verify their email before they can log in.

```json
{
  "status": true,
  "requires_verification": true,
  "message": "Account created. Please check your email to verify your account before logging in."
}
```

Show a "check your email" screen — **not** a logged-in state. After the user clicks the verification link, they must go through the normal [login flow](#login).

If your frontend handles the verification token (SPA flow), `POST /api/auth/email/verify` both verifies the email and returns a JWT in one step. See [Email Verification § Link Flow — Option B](email_verification.md#confirm--link-flow).

### Error Responses

| Status | `error` | Cause |
|--------|---------|-------|
| 403 | `Permission denied` | `ALLOW_USER_REGISTRATION` is `False` |
| 400 | `An account with this email already exists` | Duplicate email |
| 400 | (password error) | Password fails strength validation |
| 403 | `bouncer_token_*` | Invalid, expired, or wrong-scope bouncer token (see [Bouncer § Token Error Codes](bouncer.md#bouncer-token-error-codes)) |
| 429 | `Too many requests` | Rate limit exceeded |

### Bouncer Integration

When the bouncer is active, the registration page at `/register` is gated by the same challenge flow as login. The bouncer token must use `page_type: "registration"`:

1. User visits `/register` → bouncer challenge (or skip if pass cookie exists)
2. `POST /api/account/bouncer/assess` with `page_type: "registration"` → token
3. `POST /api/auth/register` with `bouncer_token` and `duid`

See [Bouncer](bouncer.md) for the full challenge flow and [Auth Pages](auth_pages.md) for the built-in registration page.

---

## Error Responses

**Invalid credentials:**

```json
{
  "status": false,
  "code": 401,
  "error": "Permission denied"
}
```

**Unauthenticated request to protected endpoint:**

```json
{
  "status": false,
  "code": 403,
  "is_authenticated": false
}
```

## Email Verification Gate

When the server has `REQUIRE_VERIFIED_EMAIL` or `REQUIRE_VERIFIED_PHONE` enabled, a login attempt by an unverified user returns a structured 403 with a machine-readable `error` field instead of a generic failure.

**Important:** `REQUIRE_VERIFIED_EMAIL` only gates logins where the identifier submitted is an **email address** (i.e. the user typed their email into the username field, or used the `email` parameter). Logging in with a plain username is never blocked by this gate, regardless of the user's email verification status.

```json
{
  "status": false,
  "code": 403,
  "error": "email_not_verified",
  "message": "Please verify your email before logging in."
}
```

Detect this error and show a **Resend verification email** prompt rather than a generic error message. The user does not need to re-enter their password — they only need to click the link.

See [Email & Phone Verification](email_verification.md) for the full send/verify flow, invite link handling, phone verification, and settings reference.

## Geofence Block

When the deployment has geofencing rules configured, login-type endpoints can
return a geofence 403 **after the credentials have been verified** — the
credential check always runs first, so invalid credentials return the normal
401 regardless of location:

```json
{
  "error": "geofence_blocked",
  "code": 403,
  "reason": "country_not_allowed",
  "detail": "Service is not available in your country."
}
```

The body carries only `reason` + `detail` (no country/region/connection
signals — intentional). Users holding the `bypass_geofence` permission are
exempt and log in normally from anywhere.

Notes for clients:

- MFA users are blocked before the challenge — a geofenced login never
  returns an `mfa_token`.
- Token-proven actions still complete: a password reset / email verify /
  invite accept from a blocked region **applies the action** but returns this
  403 instead of a session — show "done, but you can't sign in from this
  location", not "failed".
- `POST /api/auth/sessions/revoke` and the email-change confirm are never
  geofenced (an already-authenticated user can always take those security
  actions).
- Identity-less steps (register, forgot-password, magic-link/OTP send, OAuth
  begin) are still checked up front and can return the same 403 before any
  processing.

Use `GET /api/geo/check` as a pre-flight to render a "not available in your
region" page before showing the login form — see [GeoIP & Geofencing](geoip.md).
