# Email & Phone Verification — REST API Reference

## Overview

By default, users can log in regardless of whether their email or phone number has been verified. Two server-side settings enable stricter behavior:

- `REQUIRE_VERIFIED_EMAIL` (default: `False`) — when `True`, logins where the identifier is an email address are blocked until the user's email is verified. Logins via plain username are not affected.
- `REQUIRE_VERIFIED_PHONE` (default: `False`) — when `True`, phone-based (SMS) logins are blocked until the user's phone number is verified.

When a login attempt is blocked by one of these gates, the API returns a structured error response rather than a generic 403, so your client can prompt the user to verify instead of showing an unexplained failure:

```json
{
  "status": false,
  "code": 403,
  "error": "email_not_verified",
  "message": "Please verify your email before logging in."
}
```

Detect `error: "email_not_verified"` on a 403 and show a **Resend verification email** prompt. The user does not need to re-enter their password — they just need to click the link in the email.

The equivalent response when phone verification is required:

```json
{
  "status": false,
  "code": 403,
  "error": "phone_not_verified",
  "message": "Please verify your phone number before logging in."
}
```

---

## Email Verification

Email verification supports two flows depending on your integration context:

- **Link flow** (default) — sends a verification link to the user's inbox. Suitable for post-registration flows or unauthenticated resend scenarios.
- **Code flow** — sends a 6-digit OTP to the user's inbox. Suitable for in-portal verification where you don't want the user to leave the page to click a link.

Both flows use the same send endpoint with an optional `method` parameter.

---

### Send a Verification Email

**POST** `/api/auth/verify/email/send`

Requires authentication (Bearer token). Sends a verification message to the logged-in user's email address.

**Request (link flow — default):**

```json
{ "method": "link" }
```

The `method` field is optional. Omitting it is equivalent to `"link"`.

**Request (code flow):**

```json
{ "method": "code" }
```

**Response (link flow):**

```json
{
  "status": true,
  "message": "Verification email sent"
}
```

**Response (code flow):**

```json
{
  "status": true,
  "message": "Verification code sent"
}
```

If the email address is already verified, no message is sent regardless of `method`:

```json
{
  "status": true,
  "message": "Email is already verified"
}
```

> **Note:** There is also a public (unauthenticated) endpoint at `POST /api/auth/email/verify/send` that accepts a `username` or `email` field and always returns 200 regardless of account existence (prevents enumeration). That endpoint is intended for post-registration nudges and does not support the `method` parameter — it always sends a link.

---

### Confirm — Code Flow

**POST** `/api/auth/verify/email/confirm`

Requires authentication (Bearer token). Submits the 6-digit code received via email. On success, sets `is_email_verified = true` on the account. Does **not** issue a new JWT — the user's existing session remains active.

**Request:**

```json
{ "code": "123456" }
```

**Response:**

```json
{ "status": true, "message": "Email verified" }
```

**Error responses:**

| Status | `error` | Meaning |
|---|---|---|
| 400 | `Value Error` | Invalid or expired code |

Codes expire after `EMAIL_VERIFY_CODE_TTL` seconds (default 10 minutes) and are single-use. Codes and links are mutually exclusive — generating one via `/send` clears any outstanding token of the other type.

---

### Confirm — Link Flow

The verification email contains a link with a `token` query parameter. There are two ways to handle link clicks depending on your setup.

**Option A — Link clicks directly to the API (recommended for simple setups)**

The email link points to `GET /api/auth/verify/email/confirm?token=ev:...`. The server validates the token, marks the email verified, and renders a clean HTML page confirming success or describing the error. No frontend JavaScript is required.

Append `&redirect=https://yourapp.com/dashboard` to the link in the email. On success the page shows a **Continue** button and automatically navigates there after 3 seconds. On error the redirect URL is shown as a **Go back** button only.

**Option B — Frontend handles the token (SPA / mobile apps)**

The email link points to a frontend route (e.g. `/verify-email?token=ev:...`). The frontend extracts the token and submits it via API.

**POST** `/api/auth/email/verify`

```json
{ "token": "ev:4e6f74546f6b656e..." }
```

On success, the server marks `is_email_verified = true` and **logs the user in**, returning a full JWT — no separate login step is needed.

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

Tokens are **single-use** and expire after 24 hours by default (configurable via `EMAIL_VERIFY_TOKEN_TTL`). An invalid or expired token returns:

```json
{
  "status": false,
  "code": 400,
  "error": "Invalid token"
}
```

---

### Recommended UI Flow — Email Verification

**Portal / in-context (code flow):**

1. Call `POST /api/auth/verify/email/send` with `{ "method": "code" }`.
2. Show an inline OTP input: *"We sent a 6-digit code to alice@example.com. Enter it below."*
3. Submit the code to `POST /api/auth/verify/email/confirm`.
4. On success, dismiss the prompt. The `account:email:verified` realtime event will also fire — use it to update any other open views.

**Standard / link flow:**

1. Call `POST /api/auth/verify/email/send` (no body, or `{ "method": "link" }`).
2. Display: *"A verification link has been sent to alice@example.com. Click it to verify."*
3. When the user clicks the link, handle it via Option A (API renders the result page) or Option B (frontend extracts the token and calls `POST /api/auth/email/verify`).

---

## Invite Links

When a user is invited to the system or to a group, they receive an invite email containing a token. This token serves two purposes at once:

1. It verifies their email address.
2. It logs them in immediately — no password is required to complete verification.

**POST** `/api/auth/invite/accept`

```json
{ "token": "iv:4e6f74546f6b656e..." }
```

The response is identical to the email verify response — a full JWT on success:

```json
{
  "status": true,
  "data": {
    "access_token": "eyJhbGci...",
    "refresh_token": "eyJhbGci...",
    "expires_in": 21600,
    "user": {
      "id": 17,
      "username": "bob@example.com",
      "display_name": "Bob"
    }
  }
}
```

Invite tokens expire after 7 days by default (configurable via `INVITE_TOKEN_TTL`).

If the invited user has not yet set a password, they will be logged in but passwordless. After accepting the invite, prompt them to set a password using the standard password reset flow — they are already authenticated, so the reset can be performed directly from the authenticated session.

An invalid or expired invite token returns:

```json
{
  "status": false,
  "code": 400,
  "error": "Invalid token"
}
```

---

## Phone Verification

Phone verification uses a 6-digit SMS code rather than a link.

### Send verification code

**POST** `/api/auth/verify/phone/send`

Requires authentication. Sends a 6-digit OTP to the user's `phone_number` on file.

Returns 200 immediately if the phone is already verified. Returns 400 if no phone number is on the account.

**Request:** No body required.

**Response:**
```json
{ "status": true, "message": "Verification code sent" }
```

**Error responses:**

| Status | `error` | Meaning |
|---|---|---|
| 400 | `Value Error` | No phone number on account, or number is invalid |

---

### Confirm verification code

**POST** `/api/auth/verify/phone/confirm`

Requires authentication. Submits the 6-digit code received via SMS. On success, sets `is_phone_verified = true` on the account. Does **not** issue a new JWT — the user's existing session remains active.

**Request:**

```json
{ "code": "123456" }
```

**Response:**
```json
{ "status": true, "message": "Phone verified" }
```

**Error responses:**

| Status | `error` | Meaning |
|---|---|---|
| 400 | `Value Error` | Invalid or expired code |

Codes expire after `PHONE_VERIFY_CODE_TTL` seconds (default 10 minutes) and are single-use.

---

### Automatic phone verification via SMS login

When a user completes a standalone SMS login — `POST /api/auth/sms/login` followed by `POST /api/auth/sms/verify` — successfully entering the OTP code is also treated as proof of phone ownership. The server automatically sets `is_phone_verified = true` on the first successful standalone verify. This path is equivalent to the dedicated verify flow above, but combines login and verification into one step.

See [SMS OTP](mfa_sms.md) for the full SMS login flow.

---

## Verification State in the User Profile

**GET** `/api/user/me`

The authenticated user's profile includes verification flags:

```json
{
  "status": true,
  "data": {
    "id": 42,
    "username": "alice@example.com",
    "display_name": "Alice",
    "is_email_verified": true,
    "is_phone_verified": false
  }
}
```

Use these fields to decide whether to surface a verification prompt in your UI — for example, a banner encouraging the user to verify their email even when `REQUIRE_VERIFIED_EMAIL` is not enabled.

---

## Realtime Events

After successful verification, the server emits a WebSocket event to all of the user's active connections. Listen for these events to update the UI in real-time without polling or page reloads.

### Email verified

Emitted after either confirm path (`POST /api/auth/verify/email/confirm` or `GET /api/auth/verify/email/confirm`) succeeds:

```json
{
  "event": "account:email:verified",
  "data": { "email": "alice@example.com" }
}
```

Use this to dismiss a "please verify your email" banner, update the profile icon, or unlock features gated on `is_email_verified` without requiring a page reload.

### Phone verified

Emitted after `POST /api/auth/verify/phone/confirm` succeeds:

```json
{
  "event": "account:phone:verified",
  "data": { "phone_number": "+14155550123" }
}
```

Use this to dismiss a phone verification prompt or enable SMS-dependent features without requiring a page reload.

---

## Template Customisation

The `GET /api/auth/verify/email/confirm` endpoint renders `account/email_verify_confirm.html`. The default template is a minimal, self-contained page with no external dependencies. To customise it, create your own version at a path that takes priority in Django's `TEMPLATES` settings:

```
yourproject/templates/account/email_verify_confirm.html
```

Template context variables:

| Variable | Type | Description |
|---|---|---|
| `success` | bool | `True` if the address was verified successfully |
| `email` | string | The verified email address (on success) |
| `error_title` | string | Short error heading (on failure) |
| `error_message` | string | Descriptive error text (on failure) |
| `redirect_url` | string | Value of the `?redirect=` param (may be empty) |
| `redirect_delay` | int | Seconds before automatic redirect (3 on success, 0 on error) |

Append `?redirect=https://yourapp.com/dashboard` to the verification link in the email to add a **Continue** button and an automatic 3-second redirect on success.

---

## Settings Reference

| Setting | Default | Description |
|---|---|---|
| `REQUIRE_VERIFIED_EMAIL` | `False` | Block logins where the identifier is an email address until the user's email is verified. Username-based logins are not gated. |
| `REQUIRE_VERIFIED_PHONE` | `False` | Block phone (SMS) logins until the user's phone is verified |
| `EMAIL_VERIFY_TOKEN_TTL` | `86400` (24 h) | Expiry time for email verification link tokens, in seconds |
| `EMAIL_VERIFY_CODE_TTL` | `600` (10 min) | Expiry time for email verification OTP codes, in seconds |
| `INVITE_TOKEN_TTL` | `604800` (7 d) | Expiry time for invite tokens, in seconds |
| `PHONE_VERIFY_CODE_TTL` | `600` (10 min) | Expiry time for SMS phone verification codes, in seconds |

---

## Write Protection on Verification Fields

`is_email_verified` and `is_phone_verified` are **read-only** from the REST API for all non-superuser actors — including the account owner. Attempting to set them directly via `POST /api/user/<id>` will return a 403:

```json
{
  "status": false,
  "code": 403,
  "error": "Permission denied"
}
```

The only legitimate paths that set these fields are:

| Action | Sets |
|---|---|
| `POST /api/auth/email/verify` (link token redemption) | `is_email_verified = true` |
| `GET /api/auth/verify/email/confirm` (link click) | `is_email_verified = true` |
| `POST /api/auth/verify/email/confirm` (OTP code) | `is_email_verified = true` |
| `POST /api/auth/invite/accept` (invite token redemption) | `is_email_verified = true` |
| `POST /api/auth/verify/phone/confirm` (OTP code) | `is_phone_verified = true` |
| `POST /api/auth/sms/verify` without `mfa_token` (standalone SMS login) | `is_phone_verified = true` |
| Superuser `POST /api/user/<id>` | either field, either value |

Superusers can also **revoke** verification (set back to `false`) — for example, after a suspected account takeover where the email address was changed.

This protection applies to both create and update requests. A non-superuser cannot create a new user record with `is_email_verified: true` pre-set in the payload.

> To allow users to change their email address, see [Email Change](email_change.md). The change uses a dedicated verify-then-commit flow that bypasses the write protection guard safely.
>
> To allow users to change their phone number, see [Phone Number Change](phone_change.md). Replacing an existing phone number requires OTP confirmation to the new number before the change is committed.