# Email Change — REST API Reference

## Overview

Self-service email change is a three-step flow: **request → confirm → done.**

1. The authenticated user submits their desired new address and current password.
2. A confirmation link is sent to the **new** address. The current email is not changed yet.
3. The user clicks the link, the new address is committed, and a fresh JWT is issued.

`current_password` is always required in the request body. A valid Bearer token alone is not sufficient — this prevents an attacker who has stolen a session token from silently redirecting account communications.

The feature is controlled by the `ALLOW_EMAIL_CHANGE` setting (default `True`). When set to `False`, all requests to `POST /api/auth/email/change/request` return 403.

---

## Step 1 — Request the Change

**POST** `/api/auth/email/change/request`

Requires authentication (Bearer token). Rate limited.

**Request:**

```json
{
  "email": "newemail@example.com",
  "current_password": "mysecretpassword"
}
```

**Error cases:**

| Condition | Status | Response |
|---|---|---|
| `current_password` missing | 400 | `"error": "current_password is required to change your email"` |
| `current_password` incorrect | 401 | `"error": "Incorrect password"` |
| `email` has an invalid format | 400 | `"error": "Invalid email address"` |
| `email` is the same as the current address | 400 | `"error": "New email must be different from current email"` |
| `email` is already in use by another account | 400 | `"error": "Email already in use"` |
| Feature disabled via `ALLOW_EMAIL_CHANGE` | 403 | `"error": "Email change is not allowed"` |

On success, the server sends two emails:

1. **Confirmation link** to the **new** address — contains an `ec:` token valid for 1 hour.
2. **Notification** to the **old** address — informs the real owner so they can react if the request was not made by them.

Nothing is committed yet. The current email address and `is_email_verified` flag are untouched until Step 2 is completed.

**Response:**

```json
{
  "status": true,
  "message": "A confirmation link has been sent to your new email address."
}
```

---

## Step 2 — Confirm the Change (Clicking the Link)

The confirmation email contains a link with a `token` query parameter. Your frontend should extract that token and exchange it with the API.

**POST** `/api/auth/email/change/confirm`

Public endpoint — no authentication header is required. The token itself is the credential. Rate limited.

**Request:**

```json
{ "token": "ec:4e6f74546f6b656e..." }
```

On success:

- The new email address is committed to the account.
- `is_email_verified` is set to `true`.
- **All other active sessions are invalidated** — the account's `auth_key` is rotated, making every previously issued JWT immediately invalid.
- A fresh JWT is issued for this session.
- If the account uses email as its username, `username` is updated to the new address automatically.

**Response** (identical to a normal login):

```json
{
  "status": true,
  "data": {
    "access_token": "eyJhbGci...",
    "refresh_token": "eyJhbGci...",
    "expires_in": 21600,
    "user": {
      "id": 42,
      "username": "newemail@example.com",
      "display_name": "Alice"
    }
  }
}
```

Store both tokens and proceed as a normal authenticated session. Previously stored tokens are no longer valid and must be replaced.

Tokens are **single-use** and expire after 1 hour by default (configurable via `EMAIL_CHANGE_TOKEN_TTL`). Error responses for invalid or expired tokens:

```json
{
  "status": false,
  "code": 400,
  "error": "Invalid or expired token"
}
```

If another account claimed the new email address in the window between the request and the confirm step, the confirmation is rejected:

```json
{
  "status": false,
  "code": 400,
  "error": "Email address is no longer available"
}
```

---

## Cancelling a Pending Change

**POST** `/api/auth/email/change/cancel`

Requires authentication (Bearer token).

Immediately invalidates the outstanding confirmation token so the link in the new inbox stops working — even before the 1-hour TTL expires. The account is unchanged.

**Response:**

```json
{
  "status": true,
  "message": "Pending email change has been cancelled."
}
```

This endpoint is **idempotent**: if there is no pending change, it still returns 200 with the same response body.

---

## Recommended UI Flow

1. Show the user a form with fields for `email` (new address) and `current_password`.
2. Call `POST /api/auth/email/change/request`. On success, display a message: *"A confirmation link has been sent to newemail@example.com. Check your inbox and click the link to confirm."*
3. Optionally show a **Cancel pending change** button that calls `POST /api/auth/email/change/cancel`.
4. When the user clicks the link in the confirmation email, your frontend receives the token (typically via a route like `/email-change?token=ec:...`). Extract the token and call `POST /api/auth/email/change/confirm`.
5. Replace all stored tokens with the new JWT returned from step 4 and continue the session normally.

If the user ignores the confirmation email, the old address remains in effect. The pending request expires automatically after 1 hour with no further action required.

---

## Security Notes

- **`current_password` is always required.** A stolen JWT alone is not enough to redirect account communications to an attacker-controlled address.
- **The old address always receives a notification.** If the real owner did not request the change, they should call `POST /api/auth/email/change/cancel` immediately and change their password.
- **All existing sessions are invalidated on confirm.** If an attacker initiated the change, they are logged out the moment the real owner (or anyone else) confirms or the flow otherwise completes.
- **The `ec:` token is single-use and short-lived** (1 hour by default). Simply letting it expire is functionally equivalent to cancelling — the old address remains in effect.
- **Email availability is re-checked at confirm time.** Another account may have registered the target address in the 1-hour window. The confirm step will reject the token if this has occurred.
- **Username is kept in sync.** If the account uses email as its username, the `username` field is updated automatically on confirm so login with the new address works immediately.

---

## Settings Reference

| Setting | Default | Description |
|---|---|---|
| `ALLOW_EMAIL_CHANGE` | `True` | Set to `False` to disable self-service email change entirely. The request endpoint returns 403 when disabled. |
| `EMAIL_CHANGE_TOKEN_TTL` | `3600` (1 h) | Expiry time for email change confirmation tokens, in seconds |