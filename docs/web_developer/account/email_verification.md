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

## Sending a Verification Email

**POST** `/api/auth/email/verify/send`

Public endpoint — no authentication required. Accepts either `username` or `email`. Always returns 200 regardless of whether the account exists (prevents account enumeration).

**Request:**

```json
{ "email": "alice@example.com" }
```

**Response:**

```json
{
  "status": true,
  "message": "If the account exists, a verification email was sent."
}
```

If the email address is already verified, no email is sent and the response indicates that immediately:

```json
{
  "status": true,
  "message": "Email is already verified."
}
```

---

## Verifying an Email (Clicking the Link)

The verification email contains a link with a `token` query parameter. Your frontend should extract that token and exchange it with the API.

**POST** `/api/auth/email/verify`

```json
{ "token": "ev:4e6f74546f6b656e..." }
```

On success, the server marks `is_email_verified = true` on the account and **logs the user in**, returning a full JWT — no separate login step is needed.

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

Store both tokens and proceed as a normal authenticated session.

Tokens are **single-use** and expire after 24 hours by default (configurable via `EMAIL_VERIFY_TOKEN_TTL`). An invalid or expired token returns:

```json
{
  "status": false,
  "code": 400,
  "error": "Invalid token"
}
```

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

## Recommended UI Flow

1. Attempt login via `POST /api/login`.
2. If the response is a 403 with `error: "email_not_verified"`, do **not** show a generic error. Instead show: *"Your email isn't verified. [Resend verification email]"*
3. When the user taps the resend button, call `POST /api/auth/email/verify/send` with their email address.
4. When the user clicks the link in the email, your frontend receives the token (typically via a route like `/verify-email?token=ev:...`). Extract the token and call `POST /api/auth/email/verify`.
5. Store the returned JWT and redirect the user into the app as a normal authenticated session.

For invite flows, the same pattern applies using `POST /api/auth/invite/accept` in place of step 4.

---

## Settings Reference

| Setting | Default | Description |
|---|---|---|
| `REQUIRE_VERIFIED_EMAIL` | `False` | Block logins where the identifier is an email address until the user's email is verified. Username-based logins are not gated. |
| `REQUIRE_VERIFIED_PHONE` | `False` | Block phone (SMS) logins until the user's phone is verified |
| `EMAIL_VERIFY_TOKEN_TTL` | `86400` (24 h) | Expiry time for email verification tokens, in seconds |
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
| `POST /api/auth/email/verify` (token redemption) | `is_email_verified = true` |
| `POST /api/auth/invite/accept` (invite token redemption) | `is_email_verified = true` |
| `POST /api/auth/verify/phone/confirm` (authenticated code confirmation) | `is_phone_verified = true` |
| `POST /api/auth/sms/verify` without `mfa_token` (standalone SMS login) | `is_phone_verified = true` |
| Superuser `POST /api/user/<id>` | either field, either value |

Superusers can also **revoke** verification (set back to `false`) — for example, after a suspected account takeover where the email address was changed.

This protection applies to both create and update requests. A non-superuser cannot create a new user record with `is_email_verified: true` pre-set in the payload.

> To allow users to change their email address, see [Email Change](email_change.md). The change uses a dedicated verify-then-commit flow that bypasses the write protection guard safely.