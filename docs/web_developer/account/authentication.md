# Authentication — REST API Reference

## Login

**POST** `/api/login`

```json
{
  "username": "alice@example.com",
  "password": "mysecretpassword"
}
```

**Response:**

```json
{
  "status": true,
  "data": {
    "access_token": "eyJhbGci...",
    "refresh_token": "eyJhbGci...",
    "expires_in": 21600,
    "user": {
      "id": 42,
      "username": "alice@example.com",
      "display_name": "Alice"
    }
  }
}
```

Store both tokens. Use `access_token` in subsequent requests.

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
    "refresh_token": "eyJhbGci...",
    "expires_in": 21600
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

## Logout

On logout, always remove both tokens:

```javascript
localStorage.removeItem("mojo_access_token");
localStorage.removeItem("mojo_refresh_token");
```

## Security Notes

- Do not store `mfa_token` long-term; it is short-lived and only for MFA completion.
- Any XSS issue can expose tokens in `localStorage`, so enforce strong frontend security:
  - strict CSP
  - output escaping/sanitization
  - dependency hygiene
- **Revoke all sessions:** `POST /api/auth/sessions/revoke` rotates `auth_key`, immediately invalidating every outstanding JWT. Requires `current_password`. Returns a fresh JWT for the calling session so the user stays logged in. See [User Self-Management § Sessions & Devices](user_self_management.md#8-sessions--devices).
- **Email change also rotates `auth_key`** — after a successful email change confirm, all other sessions are invalidated as a side effect.
- **Security events feed:** `GET /api/account/security-events` returns auth-relevant audit events (logins, failed passwords, MFA events, email/phone changes, session revokes, etc.) scoped to the authenticated user. No special permission required. See [User Self-Management § Security Events](user_self_management.md#15-security-events).

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
    "is_active": true
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

A 6-digit code is sent to the email. Response always returns success (to prevent email enumeration).

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
