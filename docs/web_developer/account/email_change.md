# Email Change — REST API Reference

## Overview

Self-service email change is a two-step flow: **request → confirm.**

1. The authenticated user submits their desired new address and current password.
2. Either a **confirmation link** or a **6-digit OTP code** is sent to the **new** address (your choice). The current email is not changed yet.
3. The user confirms — by clicking the link or submitting the code — and the new address is committed.

`current_password` is always required in the request body. A valid Bearer token alone is not sufficient — this prevents an attacker who has stolen a session token from silently redirecting account communications.

The feature is controlled by the `ALLOW_EMAIL_CHANGE` setting (default `True`). When set to `False`, all requests to `POST /api/auth/email/change/request` return 403.

---

## Step 1 — Request the Change

**POST** `/api/auth/email/change/request`

Requires authentication (Bearer token). Rate limited.

**Request (link flow — default):**

```json
{
  "email": "newemail@example.com",
  "current_password": "mysecretpassword"
}
```

**Request (code flow — portal use):**

```json
{
  "email": "newemail@example.com",
  "current_password": "mysecretpassword",
  "method": "code"
}
```

The `method` field is optional and defaults to `"link"`. Pass `"code"` to receive a 6-digit OTP at the new address instead of a confirmation link — this is the recommended approach when the user is already in an authenticated portal context and should not have to leave to click a link.

**Error cases (both methods):**

| Condition | Status | Response |
|---|---|---|
| `current_password` missing | 400 | `"error": "current_password is required to change your email"` |
| `current_password` incorrect | 401 | `"error": "Incorrect password"` |
| `email` has an invalid format | 400 | `"error": "Invalid email address"` |
| `email` is the same as the current address | 400 | `"error": "New email must be different from current email"` |
| `email` is already in use by another account | 400 | `"error": "Email already in use"` |
| Feature disabled via `ALLOW_EMAIL_CHANGE` | 403 | `"error": "Email change is not allowed"` |

In both cases, a **notification** is sent to the **old** address informing the real owner so they can react if the request was not made by them. Nothing is committed yet.

**Response (link flow):**

```json
{
  "status": true,
  "message": "A confirmation link has been sent to your new email address."
}
```

**Response (code flow):**

```json
{
  "status": true,
  "message": "A verification code has been sent to your new email address."
}
```

> **Note — only one pending change at a time.** Calling `/request` again (regardless of method) automatically invalidates any previously issued link or code before issuing a new one.

---

## Step 2 — Confirm the Change

### Option A — Code confirm (portal / in-context, authenticated)

Use this when the request was made with `method: "code"`. The user stays in the portal and types the code they received.

**POST** `/api/auth/email/change/confirm`

Requires authentication (Bearer token). Rate limited.

**Request:**

```json
{ "code": "847291" }
```

On success:

- The new email address is committed to the account.
- `is_email_verified` is set to `true`.
- **All other active sessions are invalidated** — the account's `auth_key` is rotated.
- A fresh JWT is issued for the current session.
- If the account uses email as its username, `username` is updated automatically.

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

Replace all stored tokens with the new JWT immediately. Previously stored tokens are no longer valid.

**Error responses:**

| Response | Meaning |
|---|---|
| `"error": "Invalid code"` | Code does not match |
| `"error": "Expired code"` | Code is older than 10 minutes |
| `"error": "No pending email change"` | No code-flow change was initiated |
| `"error": "Email address is no longer available"` | Another account claimed the address in the window |
| 401 | No valid Bearer token — authentication is required for the code path |

Codes expire after `EMAIL_CHANGE_CODE_TTL` seconds (default 10 minutes) and are **single-use**.

---

### Option B — Link confirm via API page (simple setups)

Use this when the request was made with the default `method: "link"`. The confirmation email contains a link pointing to the API. The server renders a result page — no frontend JavaScript required.

**GET** `/api/auth/email/change/confirm?token=ec:...`

Public endpoint. No authentication header required. Rate limited.

On success the server renders `account/email_change_confirm.html` — a minimal, self-contained page. Downstream projects can override this template (see [Template Customisation](#template-customisation)).

**Optional redirect parameter:**

Append `&redirect=https://yourapp.com/login` to the link in the confirmation email. On success the page shows a **Continue** button pointing to the redirect URL and automatically navigates there after 3 seconds. On error the redirect URL is shown as a **Go back** button only — no automatic redirect.

```
GET /api/auth/email/change/confirm?token=ec:4e6f...&redirect=https://app.example.com/login
```

---

### Option C — Link confirm via frontend (SPA / mobile)

Use this when the request was made with the default `method: "link"` and your frontend handles the token directly.

**POST** `/api/auth/email/change/confirm`

Public endpoint. No authentication header required. Rate limited.

**Request:**

```json
{ "token": "ec:4e6f74546f6b656e..." }
```

On success: same behavior as the code path — new email committed, `auth_key` rotated, fresh JWT returned.

---

### Token / code behaviour summary

| | Code (method: code) | Link (method: link) |
|---|---|---|
| Confirm endpoint | `POST /confirm` with `{ "code": "..." }` | `GET /confirm?token=ec:...` or `POST /confirm` with `{ "token": "ec:..." }` |
| Auth required on confirm | Yes — Bearer token | No — token/link is the credential |
| TTL | 10 minutes (configurable) | 1 hour (configurable) |
| Single-use | Yes | Yes |

If another account claimed the new email address in the window between the request and the confirm step, the confirmation is rejected on all paths.

**Error responses for invalid or expired tokens (link paths):**

```json
{
  "status": false,
  "code": 400,
  "error": "Invalid or expired token"
}
```

---

## Cancelling a Pending Change

**POST** `/api/auth/email/change/cancel`

Requires authentication (Bearer token).

Immediately invalidates any outstanding confirmation link **or** OTP code — even before their TTL expires. The account is unchanged.

**Response:**

```json
{
  "status": true,
  "message": "Pending email change has been cancelled."
}
```

This endpoint is **idempotent**: if there is no pending change, it still returns 200 with the same response body.

---

## Realtime Events

After a successful email change (on any confirm path), the server emits a WebSocket event to all of the user's active connections:

```json
{
  "event": "account:email:changed",
  "data": { "email": "newemail@example.com" }
}
```

Because `auth_key` is rotated on confirm, any open sessions will find their JWTs immediately invalid. This event gives them a clean signal to re-prompt login rather than silently failing on the next API call. Listen for it and redirect to your login screen with a message like *"Your email address was changed. Please sign in again."*

---

## Recommended UI Flow

### Portal / in-context (code flow)

1. Show the user a form with fields for `email` (new address) and `current_password`.
2. Call `POST /api/auth/email/change/request` with `method: "code"`. On success, display an OTP entry prompt: *"A 6-digit code has been sent to newemail@example.com. Enter it below to confirm."*
3. Optionally show a **Cancel** button that calls `POST /api/auth/email/change/cancel`.
4. When the user submits the code, call `POST /api/auth/email/change/confirm` with `{ "code": "..." }` and a valid Bearer token.
5. Replace all stored tokens with the new JWT and continue the session.

### Simple setup (link → API page)

1. Show the user a form with fields for `email` and `current_password`.
2. Call `POST /api/auth/email/change/request` (no `method` param). On success, display: *"A confirmation link has been sent to newemail@example.com. Check your inbox and click the link to confirm."*
3. Optionally show a **Cancel pending change** button that calls `POST /api/auth/email/change/cancel`.
4. The link in the email points directly to `GET /api/auth/email/change/confirm?token=ec:...&redirect=https://yourapp.com/login`. The server renders the result page; no frontend route needed.

### SPA / mobile setup (frontend handles link)

1–3. Same as the simple setup.
4. The link in the email points to a frontend route like `/email-change?token=ec:...`. Your frontend extracts the token and calls `POST /api/auth/email/change/confirm` with `{ "token": "ec:..." }`.
5. Replace all stored tokens with the new JWT and continue the session.

---

## Security Notes

- **`current_password` is always required.** A stolen JWT alone is not enough to redirect account communications to an attacker-controlled address.
- **The old address always receives a notification.** If the real owner did not request the change, they should call `POST /api/auth/email/change/cancel` immediately and change their password.
- **All existing sessions are invalidated on confirm.** `auth_key` is rotated regardless of whether the link or code path was used.
- **Cancellation covers both paths.** `POST /api/auth/email/change/cancel` clears the `pending_email`, the outstanding `ec:` JTI, and any OTP code simultaneously — no matter which method was used to initiate the change.
- **Only one pending change at a time.** Issuing a new request (link or code) immediately invalidates the previous one before generating the new credentials.
- **The code path requires authentication.** The Bearer token is the session guard; the OTP proves ownership of the new address. Both must be correct.
- **Email availability is re-checked at confirm time.** Another account may have registered the target address in the window between request and confirm. All confirm paths reject the request if this has occurred.
- **Username is kept in sync.** If the account uses email as its username, the `username` field is updated automatically on confirm so login with the new address works immediately.

---

## Template Customisation

The `GET /api/auth/email/change/confirm` endpoint renders `account/email_change_confirm.html`. The default template is a minimal, self-contained page with no external dependencies. To customise it, create your own version at a path that takes priority in Django's `TEMPLATES` settings:

```
yourproject/templates/account/email_change_confirm.html
```

Template context variables:

| Variable | Type | Description |
|---|---|---|
| `success` | bool | `True` if the change was committed successfully |
| `new_email` | string | The newly committed email address (on success) |
| `error_title` | string | Short error heading (on failure) |
| `error_message` | string | Descriptive error text (on failure) |
| `redirect_url` | string | Value of the `?redirect=` param (may be empty) |
| `redirect_delay` | int | Seconds before automatic redirect (3 on success, 0 on error) |

Two email templates must also be defined in your project's email template system:

### `email_change_confirm` (link flow)

Sent to the **new** address when `method: "link"`. Must contain a confirmation link embedding the `ec:` token:

```
https://yourapp.com/email-change?token={{ token }}
```

Context: `token`, `new_email`, `user`.

### `email_change_code` (code flow)

Sent to the **new** address when `method: "code"`. Must display the 6-digit code prominently:

```
Your email change code is: {{ code }}
This code expires in 10 minutes.
```

Context: `code`, `new_email`, `user`.

### `email_change_notify`

Sent to the **old** address for both flows. Context: `new_email`. Should tell the owner what happened and direct them to cancel via `POST /api/auth/email/change/cancel` if they did not request this change.

---

## Settings Reference

| Setting | Default | Description |
|---|---|---|
| `ALLOW_EMAIL_CHANGE` | `True` | Set to `False` to disable self-service email change entirely. The request endpoint returns 403 when disabled. |
| `EMAIL_CHANGE_TOKEN_TTL` | `3600` (1 h) | Expiry time for link-flow email change tokens, in seconds |
| `EMAIL_CHANGE_CODE_TTL` | `600` (10 min) | Expiry time for code-flow OTP codes, in seconds |