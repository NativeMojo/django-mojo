# User Self-Management — REST API Reference

Everything a logged-in user can do for their own account. This is the
**"me" perspective** — not admin/user-management, but what you surface on
a profile page, settings screen, or account portal.

All endpoints require a valid `Authorization: Bearer <token>` header unless
noted otherwise.

---

## Table of Contents

1. [Profile](#1-profile)
2. [Avatar](#2-avatar)
3. [Password](#3-password)
4. [Email Address](#4-email-address)
5. [Phone Number](#5-phone-number)
6. [Passkeys](#6-passkeys)
7. [Two-Factor Authentication (TOTP)](#7-two-factor-authentication-totp)
8. [Sessions & Devices](#8-sessions--devices)
9. [API Keys](#9-api-keys)
10. [Notifications](#10-notifications)
11. [Notification Preferences](#11-notification-preferences)
12. [Username Change](#12-username-change)
13. [Linked OAuth Accounts](#13-linked-oauth-accounts)
14. [Account Deactivation](#14-account-deactivation)
15. [Security Events](#15-security-events)
16. [Files](#16-files)
17. [Activity Log](#17-activity-log)
18. [QR Codes](#18-qr-codes)
19. [Realtime Events Reference](#19-realtime-events-reference)

---

## 1. Profile

### Read own profile

**GET** `/api/user/me`

```json
{
  "status": true,
  "data": {
    "id": 42,
    "username": "alice@example.com",
    "email": "alice@example.com",
    "phone_number": "+15551234567",
    "display_name": "Alice Smith",
    "first_name": "Alice",
    "last_name": "Smith",
    "full_name": "Alice Smith",
    "avatar": {
      "id": 124,
      "url": "https://cdn.example.com/avatars/alice.jpg",
      "thumbnail": "https://cdn.example.com/avatars/alice_thumb.jpg"
    },
    "is_email_verified": true,
    "is_phone_verified": false,
    "is_dob_verified": false,
    "dob": "1990-05-15",
    "permissions": {},
    "metadata": {},
    "org": {"id": 5, "name": "Acme Corp"},
    "last_login": "2026-01-15T09:00:00Z",
    "last_activity": "2026-01-15T10:30:00Z",
    "is_active": true
  }
}
```

`full_name` is computed read-only: `first_name + last_name` if set, then
`display_name`, then a name derived from the username.

---

### Update own profile

**POST** `/api/user/me`

Users can update any of the following fields on their own record:

| Field | Notes |
|---|---|
| `display_name` | Checked for inappropriate content |
| `first_name` | Checked for inappropriate content |
| `last_name` | Checked for inappropriate content |
| `phone_number` | First-time set only — see [Phone Number](#5-phone-number) for replacing an existing number |
| `dob` | Date of birth (`YYYY-MM-DD`). Changing this resets `is_dob_verified` to `false` |
| `metadata` | Free-form JSON; app-defined |
| `avatar` | File ID from a completed upload — see [Avatar](#2-avatar) |

Fields **not** writable by the account owner:

| Field | Requires |
|---|---|
| `email` | Use the change flow — `POST /api/auth/email/change/request` |
| `username` | Use `POST /api/auth/username/change` — see [Username Change](#12-username-change) |
| `is_email_verified` | Internal token flows only |
| `is_phone_verified` | Internal token flows only |
| `is_dob_verified` | System-only — never REST-writable; reset automatically when `dob` changes |
| `is_active` | Manager / superuser |
| `permissions` | Manager with `manage_users` |
| `requires_mfa` | Manager / superuser |
| `auth_key` | Not writable via REST |
| `last_activity` | Not writable via REST |

```json
POST /api/user/me
{
  "display_name": "Alice J. Smith",
  "first_name": "Alice",
  "last_name": "Smith"
}
```

---

## 2. Avatar

The avatar is a `fileman.File` foreign key on the user record. The
recommended flow for all environments is the **initiated upload** — it
keeps large files off the API server.

### Option A — Initiated upload (recommended for all sizes)

**Step 1: Initiate**

```json
POST /api/fileman/upload/initiate
{
  "filename": "avatar.jpg",
  "content_type": "image/jpeg",
  "file_size": 204800
}
```

Response includes an `id` (file record) and an `upload_url`.

**Step 2: Upload**

- **S3/cloud** — `PUT` the raw file bytes to the presigned `upload_url` directly from the client.
- **Local** — `POST` multipart to the returned `/api/fileman/upload/<token>` URL.

**Step 3: Confirm**

```json
POST /api/fileman/file/<id>
{ "action": "mark_as_completed" }
```

**Step 4: Set on profile**

```json
POST /api/user/me
{ "avatar": <file_id> }
```

---

### Option B — Inline base64 (small images only)

For small thumbnails where a separate upload round-trip is disproportionate,
embed the image inline:

```json
POST /api/user/me
{
  "avatar": "data:image/jpeg;base64,/9j/4AAQSkZJRgAB..."
}
```

This passes through the API server, so keep it to avatars only — not
documents or large images.

---

### Remove avatar

```json
POST /api/user/me
{ "avatar": null }
```

---

### Serving the avatar

The `avatar` field in the user profile returns an object with `url` and
`thumbnail`. Use `thumbnail` in lists and `url` for full-size display.

```json
"avatar": {
  "id": 124,
  "url": "https://cdn.example.com/avatars/alice_full.jpg",
  "thumbnail": "https://cdn.example.com/avatars/alice_thumb.jpg"
}
```

---

## 3. Password

### Change password (authenticated)

The user is already logged in and knows their current password.

**POST** `/api/user/me`

```json
{
  "old_password": "currentpassword",
  "password": "newpassword123!"
}
```

`old_password` is required when changing password via the profile endpoint.
Omitting it returns a 400. An incorrect `old_password` returns a 401 and
logs a security incident.

---

### Reset password (forgot password)

Used when the user is not logged in. Supports two delivery methods.

**Step 1 — Request reset**

**POST** `/api/auth/forgot`

```json
{ "email": "alice@example.com", "method": "link" }
```

`method` can be `"link"` (email with clickable link, default) or `"code"`
(6-digit OTP sent to email). Always returns 200 regardless of whether the
account exists — prevents enumeration.

**Step 2a — Redeem via token (link flow)**

**POST** `/api/auth/password/reset/token`

```json
{
  "token": "pr:4e6f...",
  "new_password": "mynewpassword!"
}
```

**Step 2b — Redeem via code (code flow)**

**POST** `/api/auth/password/reset/code`

```json
{
  "email": "alice@example.com",
  "code": "847291",
  "new_password": "mynewpassword!"
}
```

Both paths log the user in and return a JWT on success — no separate login
step is needed after a password reset.

---

## 4. Email Address

### Check verification status

Read `is_email_verified` from `GET /api/user/me`.

---

### Verify email — code flow (portal / in-context)

Use when the user is already logged in and you don't want them to leave
the page.

**Step 1: Send code**

```json
POST /api/auth/verify/email/send
{ "method": "code" }
```

**Step 2: Confirm**

```json
POST /api/auth/verify/email/confirm
{ "code": "482916" }
```

On success: `is_email_verified` is set to `true`. No new JWT is issued —
the existing session continues. The `account:email:verified` WebSocket
event fires on all open tabs.

---

### Verify email — link flow

Use for post-registration flows or when the user is not in an active
browser session.

**Step 1: Send link**

```json
POST /api/auth/verify/email/send
```

(No body, or `{ "method": "link" }`)

**Step 2: User clicks the link**

The email contains a link to `GET /api/auth/verify/email/confirm?token=ev:...`.
The server renders a result page. Optionally append `&redirect=<url>` to
redirect the user to your app after 3 seconds on success.

---

### Change email address

The old address always receives a notification alerting the user that a
change was requested. Nothing is committed until the confirm step.

`current_password` is **optional**. If provided it is validated (wrong
password → 401). If omitted the request proceeds without a password check —
this supports OAuth-only and passkey-only users who have no usable password.

> **Security note:** A notification is always sent to the **current** email
> address when a change is requested. This gives the real account owner a
> window to react (revoke sessions, cancel the change) if the request was
> not initiated by them.

**Step 1: Request**

```json
POST /api/auth/email/change/request
{
  "email": "newemail@example.com",
  "current_password": "currentpassword",
  "method": "code"
}
```

`method` is optional — defaults to `"link"`. Use `"code"` for the
in-portal flow. `current_password` is optional — omit it for
OAuth/passkey-only users.

**Step 2: Confirm (code)**

```json
POST /api/auth/email/change/confirm
{ "code": "381924" }
```

**Step 2: Confirm (link)**

The confirmation email contains a link to
`GET /api/auth/email/change/confirm?token=ec:...`. Or have the user
submit the token via:

```json
POST /api/auth/email/change/confirm
{ "token": "ec:4e6f..." }
```

On success (all paths): the new email is committed, `is_email_verified` is
set to `true`, all other sessions are invalidated, and a fresh JWT is
returned. Store the new tokens immediately.

**Cancel a pending change**

```json
POST /api/auth/email/change/cancel
```

Immediately kills any outstanding confirmation link or code. Idempotent.

---

### Email change error cases

| Condition | Status | Error |
|---|---|---|
| `current_password` provided but incorrect | 401 | `"Incorrect password"` |
| `current_password` omitted | *(allowed — request proceeds)* | |
| New address already in use | 400 | `"Email already in use"` |
| New address same as current | 400 | `"New email must be different..."` |
| Invalid format | 400 | `"Invalid email address"` |
| Code expired (10 min) | 400 | `"Expired code"` |
| Token expired (1 hour) | 400 | `"Invalid or expired token"` |

---

## 5. Phone Number

### Add a phone number (first time)

If the account has no phone number, set it directly via the profile update.
This does **not** require OTP verification at write time, but the number
will be unverified until the verify flow is completed.

```json
POST /api/user/me
{ "phone_number": "+14155550123" }
```

Use E.164 format (`+` + country code + number) for maximum reliability.
Numbers are normalised server-side.

After setting the number, run the verification flow below.

---

### Verify phone number

**Step 1: Send SMS code**

```json
POST /api/auth/verify/phone/send
```

No body needed. Sends a 6-digit OTP to the number on the account.

**Step 2: Confirm**

```json
POST /api/auth/verify/phone/confirm
{ "code": "293847" }
```

On success: `is_phone_verified` is set to `true`. No new JWT. The
`account:phone:verified` WebSocket event fires on all open tabs.

| Error | Meaning |
|---|---|
| `"No phone number on account"` | Set a number first via `POST /api/user/me` |
| `"Invalid code"` | Wrong OTP — try again |
| `"Expired code"` | Code is older than 10 minutes — resend |

---

### Change phone number (replacing an existing verified number)

Direct REST replacement of an existing phone number is blocked. Use the
change flow, which proves ownership of the new number via OTP before
committing it.

`current_password` is **optional**. If provided it is validated (wrong
password → 401). If omitted the request proceeds without a password check —
this supports OAuth-only and passkey-only users who have no usable password.

> **Security note:** When the user already has a phone number on file, an
> SMS is sent to the **current** number alerting them that a change was
> requested. This gives the real account owner a chance to react if the
> request was not initiated by them.

**Step 1: Request**

```json
POST /api/auth/phone/change/request
{
  "phone_number": "+14155550199",
  "current_password": "currentpassword"
}
```

`current_password` is optional — omit it for OAuth/passkey-only users.
Response includes a `session_token` — keep it for Step 2.

**Step 2: Confirm**

```json
POST /api/auth/phone/change/confirm
{
  "session_token": "pc:3a1b2c4d...",
  "code": "847291"
}
```

On success: new number committed, `is_phone_verified` set to `true`, no
JWT rotation (phone is not a login credential by default).

**Cancel**

```json
POST /api/auth/phone/change/cancel
```

---

### Remove phone number

```json
POST /api/user/me
{ "phone_number": null }
```

Clearing is always permitted. `is_phone_verified` is automatically reset
to `false` whenever the phone number changes.

---

## 6. Passkeys

Passkeys (FIDO2/WebAuthn) let the user log in with Face ID, Touch ID, or
a hardware key — no password required. The user must be logged in to
register one.

### Register a passkey

**Step 1 — Begin**

```http
POST /api/account/passkeys/register/begin
Authorization: Bearer <token>
Origin: https://your-app.example.com
```

Returns a `challenge_id` and a `publicKey` options object to pass to
`navigator.credentials.create()`.

**Step 2 — Complete**

```json
POST /api/account/passkeys/register/complete
{
  "challenge_id": "abc123",
  "credential": { ...WebAuthn response object... },
  "friendly_name": "MacBook Touch ID"
}
```

`friendly_name` is a human-readable label shown in the passkeys list.

---

### List registered passkeys

```
GET /api/account/passkeys
```

Returns all passkeys registered by the current user.

---

### Rename a passkey

```json
POST /api/account/passkeys/<id>
{ "friendly_name": "iPhone Face ID" }
```

---

### Delete a passkey

```
DELETE /api/account/passkeys/<id>
```

---

## 7. Two-Factor Authentication (TOTP)

TOTP lets the user link an authenticator app (Google Authenticator, Authy,
1Password, etc.) as a second factor.

> **Two endpoint groups exist for TOTP.** The `account/totp/*` endpoints below
> are for managing TOTP on a settings page (requires auth). The `auth/totp/*`
> endpoints are for the login page and are covered in
> [TOTP Authentication](mfa_totp.md).

### Check TOTP status

Check `requires_mfa` on the user profile:

```
GET /api/user/me
Authorization: Bearer <access_token>
```

```json
{ "data": { "requires_mfa": true } }
```

`requires_mfa: true` means a second factor (TOTP or SMS) is active on this account.

---

### Set up TOTP

**Step 1 — Generate a secret**

```
POST /api/account/totp/setup
Authorization: Bearer <access_token>
```

Returns a `secret`, a `qr_code` (base64 PNG), and a `uri` for deep linking
into authenticator apps. Display the QR code for the user to scan. The secret
is saved server-side but TOTP is **not active yet**.

**Step 2 — Confirm with first code**

```
POST /api/account/totp/confirm
Authorization: Bearer <access_token>
```

```json
{ "code": "123456" }
```

The user enters the 6-digit code from their app. A valid code proves the app
is correctly linked and activates TOTP on the account. Sets `requires_mfa: true`.

**Response:**

```json
{
  "status": true,
  "data": {
    "is_enabled": true,
    "recovery_codes": [
      "a1b2-c3d4-e5f6",
      "1122-3344-5566",
      "dead-beef-cafe",
      "abcd-ef01-2345",
      "6789-abcd-ef01",
      "f0e1-d2c3-b4a5",
      "9876-5432-1fed",
      "0a1b-2c3d-4e5f"
    ]
  }
}
```

> **Important:** Display the recovery codes immediately and instruct the user to
> store them in a safe place. The plaintext codes are shown **only once** — they
> are stored as bcrypt hashes on the server and cannot be retrieved again in full.

---

### Disable TOTP

```
DELETE /api/account/totp
Authorization: Bearer <access_token>
```

No request body required. TOTP is deactivated immediately. The current session
remains valid; the next password login will return a JWT with no MFA challenge.

---

### View recovery codes (masked)

Show the user which recovery codes they have remaining. Safe to call on a
settings page — codes are masked so they cannot be stolen from a screenshot.

```
GET /api/account/totp/recovery-codes
Authorization: Bearer <access_token>
```

**Response:**

```json
{
  "status": true,
  "data": {
    "remaining": 7,
    "codes": [
      "a1b2-xxxx-xxxx",
      "1122-xxxx-xxxx",
      "dead-xxxx-xxxx",
      "abcd-xxxx-xxxx",
      "6789-xxxx-xxxx",
      "f0e1-xxxx-xxxx",
      "0a1b-xxxx-xxxx"
    ]
  }
}
```

`remaining` is the count of unused codes. Only the first 4 hex characters of
each code are shown — enough for the user to identify which ones they've used.

Returns `400` if TOTP is not enabled on the account.

---

### Regenerate recovery codes

Invalidates all existing recovery codes and generates a fresh set of 8. Requires
a **live TOTP code** from the authenticator app — this prevents an attacker with
a stolen session from silently rotating the codes.

```
POST /api/account/totp/recovery-codes/regenerate
Authorization: Bearer <access_token>
```

```json
{ "code": "482910" }
```

**Response:**

```json
{
  "status": true,
  "data": {
    "is_enabled": true,
    "recovery_codes": [
      "f6e5-d4c3-b2a1",
      "6655-4433-2211",
      "aabb-ccdd-eeff",
      "1234-5678-9abc",
      "def0-1234-5678",
      "9abc-def0-1234",
      "5678-9abc-def0",
      "cafe-babe-feed"
    ]
  }
}
```

Display the new codes and instruct the user to replace any previously saved
codes. Old codes are permanently invalidated.

| Condition | Status |
|---|---|
| Invalid TOTP code | 403 — existing codes unchanged |
| TOTP not enabled | 400 |

---

### Recovery login (lost authenticator device)

When the user can enter their password but **cannot provide a TOTP code**
(lost phone, factory reset, etc.), they can use a recovery code instead.

This is a **public endpoint** — no Bearer token required. The user must first
complete the normal password login to obtain an `mfa_token`.

```
POST /api/auth/totp/recover
```

```json
{
  "mfa_token": "a3f1c9d2...",
  "recovery_code": "a1b2-c3d4-e5f6"
}
```

**Response (success):**

```json
{
  "status": true,
  "data": {
    "access_token": "eyJhbGci...",
    "refresh_token": "eyJhbGci...",
    "expires_in": 21600,
    "user": { "id": 42, "username": "alice", "display_name": "Alice" }
  }
}
```

**Behaviour:**

- The recovery code is **consumed on use** — it cannot be used again.
- An incident is logged (`totp:recovery_used`).
- If the last remaining code is consumed, a notification warns the user to
  generate new codes.
- The `mfa_token` is consumed on use.

**Typical UI flow:**

1. User enters username + password → receives `mfa_token` + `mfa_required: true`
2. User clicks "Lost your authenticator? Use a recovery code"
3. User enters one of their saved recovery codes
4. Frontend calls `POST /api/auth/totp/recover` with `mfa_token` + `recovery_code`
5. On success, store the JWT and redirect to the app

| Condition | Status |
|---|---|
| Invalid `mfa_token` | 401 |
| Invalid or already-used recovery code | 403 |
| Account inactive | 403 |

> **Full TOTP reference:** See [TOTP Authentication](mfa_totp.md) for complete
> endpoint details including standalone TOTP login and all error cases.

---

## 8. Sessions & Devices

### List tracked devices

The framework tracks the devices that have logged into the account.

```
GET /api/user/device
Authorization: Bearer <access_token>
```

Returns device records including `user_agent`, `ip`, `created`, and
`last_seen`. Useful for showing the user "where you're logged in" on a
security settings page.

---

### Revoke all sessions (log out everywhere)

Invalidate every active JWT across all devices. The calling session receives a
fresh token so the user stays logged in — every **other** session is killed.

```
POST /api/auth/sessions/revoke
Authorization: Bearer <access_token>
```

```json
{ "current_password": "mysecretpassword" }
```

`current_password` is **required** — this prevents an attacker with a stolen
JWT from locking the real user out of all their sessions.

**Response (success):**

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

**What happens on the backend:**

1. `auth_key` is rotated — every outstanding JWT signed with the old key is
   immediately invalid.
2. A fresh JWT is issued with the new key and returned in the response.
3. An incident `sessions:revoked` is logged.

**Important:** Replace your stored access and refresh tokens with the ones
from this response. The old ones are dead.

| Condition | Status |
|---|---|
| Wrong password | 401 — no state changed, incident `sessions:revoke_failed` logged |
| Missing `current_password` | 400 |
| Unauthenticated | 401/403 |

Rate-limited: 5 requests per IP per 5 minutes.

> **Note:** Per-device revocation is not supported. This endpoint is
> all-or-nothing. Email change confirm and password change also rotate
> `auth_key` as a side effect, which has the same "log out everywhere" result.

---

### Register device for push notifications

```
POST /api/account/devices/push/register
Authorization: Bearer <access_token>
```

```json
{
  "device_token": "...",
  "device_id": "unique-device-id",
  "platform": "ios"
}
```

```
POST /api/account/devices/push/unregister
Authorization: Bearer <access_token>
```

```json
{
  "device_token": "...",
  "device_id": "unique-device-id",
  "platform": "ios"
}
```

---

## 9. API Keys

A user can generate a long-lived JWT for programmatic access to the API
on their own behalf (e.g. from a script or automation). This token carries
the user's full permissions and is IP-restricted.

### Generate a personal API key

```json
POST /api/auth/generate_api_key
{
  "allowed_ips": ["203.0.113.0/24"],
  "expire_days": 90
}
```

`allowed_ips` is required. At least one IP or CIDR range must be specified.
`expire_days` defaults to 360, maximum 360.

**Response:**

```json
{
  "status": true,
  "data": {
    "token": "eyJhbGci...",
    "jti": "abc123",
    "expires": 1760000000
  }
}
```

> **Security:** Treat this token like a password. It carries full account
> permissions. Store it securely; do not expose it in client-side code.

---

## 10. Notifications

The user has an inbox of server-generated notifications. These are also
delivered in real-time via WebSocket and device push.

### List unread notifications

```
GET /api/account/notification
```

Returns unread notifications by default.

### List all notifications (including read)

```
GET /api/account/notification?is_unread=false
```

### Mark as read

```json
POST /api/account/notification/<id>
{ "mark_read": true }
```

### Filter by kind

```
GET /api/account/notification?kind=message
```

### Notification shape

```json
{
  "id": 101,
  "created": "2026-03-11T10:00:00Z",
  "title": "Your order shipped",
  "body": "Order #123 is on its way.",
  "kind": "general",
  "data": {},
  "action_url": "/orders/123",
  "is_unread": true
}
```

Real-time delivery comes via WebSocket — listen for `type === "notification"`
in your message handler to show banners without polling.

---

## 11. Notification Preferences

Users can control which notification types they receive and on which channels
(in-app inbox, email, push). Default is **allow** — only suppress when the
user explicitly opts out.

### Get preferences

```
GET /api/account/notification/preferences
Authorization: Bearer <access_token>
```

```json
{
  "status": true,
  "data": {
    "preferences": {
      "message":   { "in_app": true, "email": true,  "push": true  },
      "marketing": { "in_app": true, "email": false, "push": false }
    }
  }
}
```

An empty `preferences` dict means everything is on (default).

### Update preferences

Partial update — only the keys present in the request body are changed.

```
POST /api/account/notification/preferences
Authorization: Bearer <access_token>
```

```json
{
  "preferences": {
    "marketing": { "email": false, "push": false }
  }
}
```

Response returns the full current preferences after merging.

**Validation rules:**
- `preferences` must be a dict — 400 if not
- Each kind value must be a dict of channel booleans — 400 if not
- Valid channels: `in_app`, `email`, `push` — unknown channels are ignored
- Kind keys are free-form strings (projects define their own kinds, max 64 chars)

### Enforcement

Preferences are enforced in all three delivery paths:

| Channel | Enforcement point |
|---|---|
| In-app inbox | `Notification.send()` checks before creating the record |
| Email | `send_template_email()` checks when caller passes `kind=` |
| Push | `push_notification()` checks when caller passes `kind=` |

System / transactional emails (password reset, email verification, magic login,
deactivation confirmation) never pass a `kind` and are therefore **never
suppressed** by preferences.

---

## 12. Username Change

Change the authenticated user's username. Requires `current_password` as
proof of ownership.

```
POST /api/auth/username/change
Authorization: Bearer <access_token>
```

```json
{
  "username": "new_username",
  "current_password": "currentpassword"
}
```

```json
{
  "status": true,
  "data": { "username": "new_username" }
}
```

**Behaviour:**
- Username is lowercased and trimmed before any check
- Validates via `content_guard` (blocked content rejected)
- Uniqueness checked (400 if taken)
- Same-as-current rejected (400)
- No `auth_key` rotation — existing session continues

**Error cases:**

| Condition | Status |
|---|---|
| Wrong password | 401 |
| OAuth-only account (no usable password) | 400 |
| Username taken | 400 |
| Same as current | 400 |
| Invalid content (content guard) | 400 |
| `ALLOW_USERNAME_CHANGE = False` | 403 |
| Unauthenticated | 401/403 |

---

## 13. Linked OAuth Accounts

View and manage OAuth provider connections linked to the account. Useful for a
"Connected accounts" section on a settings page.

### List connections

```
GET /api/account/oauth_connection
Authorization: Bearer <access_token>
```

```json
{
  "status": true,
  "count": 2,
  "results": [
    {
      "id": 7,
      "provider": "google",
      "email": "alice@gmail.com",
      "is_active": true,
      "created": "2026-01-10T09:00:00Z"
    },
    {
      "id": 12,
      "provider": "github",
      "email": "alice@users.noreply.github.com",
      "is_active": true,
      "created": "2026-02-05T14:30:00Z"
    }
  ]
}
```

Only the authenticated user's own connections are returned — the `owner` filter
is enforced server-side.

### Unlink a connection

```
DELETE /api/account/oauth_connection/<id>
Authorization: Bearer <access_token>
```

**Success:**

```json
{ "status": true }
```

**Lockout guard:** If the user has no usable password and this is their only
active OAuth connection, the unlink is blocked — the user would have no way to
log in:

```json
{
  "status": false,
  "code": 400,
  "error": "Cannot unlink your only login method. Set a password first."
}
```

| Condition | Status |
|---|---|
| Success | 200 |
| Last connection + no password | 400 |
| Connection not found or not owned | 404 |

`manage_users` admins bypass the lockout guard.

> **Linking a new provider** does not require a separate endpoint — the user
> goes through the normal OAuth login flow (`GET /api/account/oauth/<provider>/begin`)
> while already logged in and the connection is created automatically.

---

## 14. Account Deactivation

Self-service account deactivation via a two-step email confirmation flow. Uses
the existing `pii_anonymize()` method which anonymises all PII (username, email,
phone, display name, DOB, metadata), rotates `auth_key` (invalidating all JWTs),
and sets `is_active = False`. The user row is preserved for FK integrity and
audit trail — this is not a hard delete.

OAuth-only users (no password set) are fully supported — the email confirmation
link is sufficient proof of ownership.

### Step 1 — Request deactivation

```
POST /api/account/deactivate
Authorization: Bearer <access_token>
```

No request body required. Returns 200 and sends a confirmation email containing
a `dv:` token (15-minute TTL, single-use).

```json
{
  "status": true,
  "message": "A confirmation email has been sent. Follow the link to complete deactivation."
}
```

Rate-limited: 5 requests per IP per 5 minutes.

### Step 2 — Confirm deactivation

```
POST /api/account/deactivate/confirm
```

```json
{ "token": "dv:4e6f..." }
```

Public endpoint — the token is the credential (no Bearer token required).

```json
{
  "status": true,
  "message": "Your account has been deactivated."
}
```

**What happens on the backend:**

1. Token is validated (single-use, expiry, correct kind)
2. An `account:deactivated` incident is logged while the username is still readable
3. `pii_anonymize()` runs — all PII cleared, `auth_key` rotated, account disabled
4. All active JWTs are immediately invalid

**Important:** After a successful confirm, clear all stored tokens on the client.
Any subsequent API call with the old JWT will return 401.

**Error cases:**

| Condition | Status |
|---|---|
| Already inactive | 200 (idempotent, no double-anonymise) |
| Token expired (>15 min) | 400 |
| Token already used | 400 |
| Wrong token kind (e.g. `pr:` or `ml:`) | 400 |
| Missing token | 400 |
| `ALLOW_SELF_DEACTIVATION = False` | 403 |
| Unauthenticated request to Step 1 | 401/403 |

**Settings:**

| Setting | Default | Purpose |
|---|---|---|
| `DEACTIVATE_TOKEN_TTL` | `900` | Seconds until confirmation token expires |
| `ALLOW_SELF_DEACTIVATION` | `True` | Feature flag — set `False` to disable entirely |

**Email template:** The downstream project must provide an
`account_deactivate_confirm` email template. Context variables: `token` (the
raw `dv:` string), `user` (the user object).

---

## 15. Security Events

A lightweight, user-scoped feed of auth-relevant audit events. No special
permission required — a user can only see their own events. Useful for a
"Recent security activity" card on a settings or dashboard page.

```
GET /api/account/security-events
Authorization: Bearer <access_token>
```

### Query parameters

| Param | Default | Notes |
|---|---|---|
| `size` | 25 | Max results; hard cap 100 |
| `dr_start` | — | ISO date range start (inclusive) |
| `dr_end` | — | ISO date range end (inclusive) |

### Response

```json
{
  "status": true,
  "count": 4,
  "results": [
    {
      "created": "2026-04-01T10:00:00Z",
      "kind": "invalid_password",
      "summary": "Failed login — incorrect password",
      "ip": "203.0.113.5"
    },
    {
      "created": "2026-04-01T09:55:00Z",
      "kind": "login",
      "summary": "Successful login",
      "ip": "203.0.113.5"
    }
  ]
}
```

Only `created`, `kind`, `summary`, and `ip` are returned. Internal fields
(`details`, `title`, `metadata`, `level`, etc.) are **never** exposed.

### Event kind → summary mapping

The `summary` field is a human-readable label derived from `kind`. Use these to
build icons, colours, or groupings in your UI.

| `kind` | `summary` |
|---|---|
| `login` | Successful login |
| `login:unknown` | Login attempt with unknown account |
| `invalid_password` | Failed login — incorrect password |
| `password_reset` | Password reset requested |
| `totp:confirm_failed` | TOTP setup — invalid confirmation code |
| `totp:login_failed` | Failed login — incorrect TOTP code |
| `totp:login_unknown` | TOTP login attempt with unknown account |
| `totp:recovery_used` | TOTP recovery code used |
| `email_change:requested` | Email change requested |
| `email_change:requested_code` | Email change requested (code flow) |
| `email_change:cancelled` | Email change cancelled |
| `email_change:invalid` | Email change — invalid token |
| `email_change:expired` | Email change — expired token |
| `email_verify:confirmed` | Email address verified |
| `email_verify:confirmed_code` | Email address verified via code |
| `phone_change:requested` | Phone number change requested |
| `phone_change:confirmed` | Phone number changed |
| `phone_change:cancelled` | Phone number change cancelled |
| `phone_verify:confirmed` | Phone number verified |
| `username:changed` | Username changed |
| `oauth` | Signed in with social account |
| `passkey:login_failed` | Failed passkey login |
| `account:deactivated` | Account deactivated |
| `account:deactivate_requested` | Account deactivation requested |
| `sessions:revoked` | All sessions revoked |
| `sessions:revoke_failed` | Session revoke — incorrect password |

Unknown `kind` values fall back to the `kind` string itself as the summary
(forward-compatible — new event types work without a client update).

### UI tips

- **Red / warning** — `invalid_password`, `totp:login_failed`, `passkey:login_failed`, `sessions:revoke_failed`
- **Green / success** — `login`, `oauth`, `email_verify:confirmed`, `phone_verify:confirmed`
- **Neutral / info** — everything else (changes, requests, resets)
- Show `ip` with a "not you?" prompt linking to the session revoke flow

---

## 16. Files

Users can upload and manage their own files — documents, images, and other
attachments.

### List own files

```
GET /api/fileman/file?sort=-created
```

Filter by type, status, or search term:

```
GET /api/fileman/file?content_type=image/jpeg
GET /api/fileman/file?upload_status=completed
GET /api/fileman/file?search=report
```

---

### Upload a file

Follow the initiated upload flow described in [Avatar](#2-avatar) but
without the final profile-update step:

1. `POST /api/fileman/upload/initiate` — get file record + upload URL
2. Upload to the returned URL (S3 presigned PUT or direct token POST)
3. `POST /api/fileman/file/<id>` with `{ "action": "mark_as_completed" }`

---

### View a file

```
GET /api/fileman/file/<id>
```

Returns the file record including `url`, `thumbnail` (for images), and
`renditions`.

---

### Update file metadata

```json
POST /api/fileman/file/<id>
{
  "is_public": true,
  "metadata": {"tags": ["invoice", "2026"]}
}
```

---

### Delete a file

```
DELETE /api/fileman/file/<id>
```

Deletes the database record and the underlying file from storage, including
all renditions (thumbnails, previews, etc.).

---

### File upload status values

| Status | Meaning |
|---|---|
| `pending` | Record created, upload not started |
| `uploading` | Transfer in progress |
| `completed` | Stored successfully |
| `failed` | Upload failed |
| `expired` | Upload token expired — initiate a new upload |

---

### Encrypted / sensitive files (FileVault)

For files that need AES-256-GCM encryption at rest — contracts, ID
documents, financial records — use the FileVault API.

```json
POST /api/filevault/file/upload   (multipart/form-data)
  file=@document.pdf
  name=Q4 Contract
  description=Signed copy
  password=optional-extra-password
```

Download via a time-limited, IP-bound token:

```json
POST /api/filevault/file/<id>/unlock
{ "ttl": 300 }
```

Then `GET /api/filevault/file/download/<token>`.

See [FileVault docs](../../filevault/README.md) for the full reference.

---

## 17. Activity Log

The framework writes an audit log entry for significant account events.
These are readable by the user themselves as a "recent activity" feed.

> **Note:** Log access requires the `view_logs` permission. Whether your
> deployment grants this to ordinary users is a product decision. The
> endpoints below are available when it is granted.

### List own activity

```
GET /api/logit/log?uid=<me>&sort=-created&size=50
```

Filter by kind prefix to show specific categories:

```
GET /api/logit/log?uid=<me>&kind__startswith=email:
GET /api/logit/log?uid=<me>&kind__startswith=password
GET /api/logit/log?uid=<me>&kind__startswith=login
```

### Common audit kinds for user self-management

| `kind` | Meaning |
|---|---|
| `email:changed` | Email address was changed |
| `email_verify:confirmed` | Email address was verified |
| `email_verify:confirmed_code` | Email verified via OTP code |
| `email_change:requested` | Email change was requested |
| `email_change:cancelled` | Pending email change was cancelled |
| `phone:changed` | Phone number was changed |
| `phone_verify:confirmed` | Phone number was verified |
| `phone_change:requested` | Phone change was requested |
| `username:changed` | Username was updated |
| `invalid_password` | Failed login or bad current-password attempt |
| `password_reset` | Password reset was completed |

### Date range

```
GET /api/logit/log?uid=<me>&dr_start=2026-01-01&dr_end=2026-01-31
```

---

## 18. QR Codes

Generate a QR code for any string — useful for sharing profile links,
transfer codes, tickets, or MFA setup (see TOTP above).

```
GET /api/qrcode?data=https://app.example.com/profile/42&format=png&size=256
```

Or via POST for more options:

```json
POST /api/qrcode
{
  "data": "https://app.example.com/profile/42",
  "format": "base64",
  "base64_format": "svg",
  "size": 256,
  "color": "#0D9488",
  "background": "#FFFFFF"
}
```

**Formats:**

| `format` | Response |
|---|---|
| `png` (default) | Raw PNG image bytes |
| `svg` | Raw SVG text |
| `base64` | JSON with `data` field containing base64-encoded image |

Use `format=base64` when you need to embed the QR in a JSON API response
or store it server-side. Use `png` or `svg` when serving it directly to a
browser `<img>` tag or file download.

**Logo overlay:**

Pass a base64-encoded image in `logo` to center it over the QR code (e.g.
your app icon). `logo_scale` controls how much of the QR the logo covers
(default `0.2` = 20%).

```json
{
  "data": "https://app.example.com/profile/42",
  "format": "base64",
  "logo": "<base64-encoded-png>",
  "logo_scale": 0.2
}
```

---

## 19. Realtime Events Reference

Subscribe to these WebSocket events to update the UI without polling when
account state changes — including when the change happens in another tab
or on another device.

| Event | Fires after |
|---|---|
| `account:email:verified` | Email verified (code or link path) |
| `account:email:changed` | Email change committed |
| `account:phone:verified` | Phone number verified |
| `notification` | New notification created server-side |

### Handling `account:email:changed`

This event signals that `auth_key` was rotated — all existing JWTs are now
invalid. On receiving it, redirect to login with a message:

```
"Your email address was updated. Please sign in again."
```

### Handling `account:email:verified` and `account:phone:verified`

These signal that a verification step completed in another tab or device.
Refresh the user profile to pick up the updated `is_email_verified` or
`is_phone_verified` flag and dismiss any verification prompts.

---

## Quick Reference — Self-Service Endpoint Summary

| Action | Method | Endpoint | Auth |
|---|---|---|---|
| Get own profile | GET | `/api/user/me` | Required |
| Update own profile | POST | `/api/user/me` | Required |
| Upload avatar (initiate) | POST | `/api/fileman/upload/initiate` | Required |
| Confirm upload | POST | `/api/fileman/file/<id>` | Required |
| Set avatar on profile | POST | `/api/user/me` | Required |
| Change password | POST | `/api/user/me` | Required |
| Forgot password (send) | POST | `/api/auth/forgot` | Public |
| Reset password (code) | POST | `/api/auth/password/reset/code` | Public |
| Reset password (token) | POST | `/api/auth/password/reset/token` | Public |
| Send email verify (code) | POST | `/api/auth/verify/email/send` | Required |
| Confirm email verify (code) | POST | `/api/auth/verify/email/confirm` | Required |
| Confirm email verify (link) | GET | `/api/auth/verify/email/confirm` | Public |
| Request email change | POST | `/api/auth/email/change/request` | Required |
| Confirm email change (code) | POST | `/api/auth/email/change/confirm` | Required |
| Confirm email change (link) | POST/GET | `/api/auth/email/change/confirm` | Public |
| Cancel email change | POST | `/api/auth/email/change/cancel` | Required |
| Set phone number (first time) | POST | `/api/user/me` | Required |
| Send phone verify | POST | `/api/auth/verify/phone/send` | Required |
| Confirm phone verify | POST | `/api/auth/verify/phone/confirm` | Required |
| Request phone change | POST | `/api/auth/phone/change/request` | Required |
| Confirm phone change | POST | `/api/auth/phone/change/confirm` | Required |
| Cancel phone change | POST | `/api/auth/phone/change/cancel` | Required |
| Begin passkey registration | POST | `/api/account/passkeys/register/begin` | Required |
| Complete passkey registration | POST | `/api/account/passkeys/register/complete` | Required |
| List passkeys | GET | `/api/account/passkeys` | Required |
| Rename a passkey | POST | `/api/account/passkeys/<id>` | Required |
| Delete passkey | DELETE | `/api/account/passkeys/<id>` | Required |
| Setup TOTP (get QR code) | POST | `/api/account/totp/setup` | Required |
| Confirm TOTP (activate) | POST | `/api/account/totp/confirm` | Required |
| View TOTP recovery codes | GET | `/api/account/totp/recovery-codes` | Required |
| Regenerate recovery codes | POST | `/api/account/totp/recovery-codes/regenerate` | Required |
| Recovery login (lost device) | POST | `/api/auth/totp/recover` | Public |
| Disable TOTP | DELETE | `/api/account/totp` | Required |
| Revoke all sessions | POST | `/api/auth/sessions/revoke` | Required |
| List devices | GET | `/api/user/device` | Required |
| Register push device | POST | `/api/account/devices/push/register` | Required |
| Unregister push device | POST | `/api/account/devices/push/unregister` | Required |
| Generate personal API key | POST | `/api/auth/generate_api_key` | Required |
| List notifications | GET | `/api/account/notification` | Required |
| Mark notification read | POST | `/api/account/notification/<id>` | Required |
| Get notification preferences | GET | `/api/account/notification/preferences` | Required |
| Update notification preferences | POST | `/api/account/notification/preferences` | Required |
| Change username | POST | `/api/auth/username/change` | Required |
| List OAuth connections | GET | `/api/account/oauth_connection` | Required |
| Unlink OAuth connection | DELETE | `/api/account/oauth_connection/<id>` | Required |
| Request account deactivation | POST | `/api/account/deactivate` | Required |
| Confirm account deactivation | POST | `/api/account/deactivate/confirm` | Public |
| View security events | GET | `/api/account/security-events` | Required |
| List own files | GET | `/api/fileman/file` | Required |
| Delete own file | DELETE | `/api/fileman/file/<id>` | Required |
| Upload encrypted file | POST | `/api/filevault/file/upload` | Required |
| Download encrypted file (get token) | POST | `/api/filevault/file/<id>/unlock` | Required |
| View own activity log | GET | `/api/logit/log` | Required + `view_logs` |
| Generate QR code | GET/POST | `/api/qrcode` | Public |