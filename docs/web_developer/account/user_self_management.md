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
11. [Files](#11-files)
12. [Activity Log](#12-activity-log)
13. [QR Codes](#13-qr-codes)
14. [Realtime Events Reference](#14-realtime-events-reference)

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
| `metadata` | Free-form JSON; app-defined |
| `avatar` | File ID from a completed upload — see [Avatar](#2-avatar) |

Fields **not** writable by the account owner:

| Field | Requires |
|---|---|
| `email` | Use the change flow — `POST /api/auth/email/change/request` |
| `username` | Superuser only |
| `is_email_verified` | Internal token flows only |
| `is_phone_verified` | Internal token flows only |
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

Requires `current_password`. The old address always receives a notification
of the request. Nothing is committed until the confirm step.

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
in-portal flow.

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
| `current_password` incorrect | 401 | `"Incorrect password"` |
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

**Step 1: Request**

```json
POST /api/auth/phone/change/request
{
  "phone_number": "+14155550199",
  "current_password": "currentpassword"
}
```

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
  "name": "MacBook Touch ID"
}
```

`name` is a human-readable label shown in the passkeys list.

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
{ "name": "iPhone Face ID" }
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

### Check TOTP status

Read the user profile — the `full` graph includes TOTP status, or check
via:

```
GET /api/auth/totp/status
```

---

### Set up TOTP

**Step 1 — Generate a secret**

```
POST /api/auth/totp/setup
```

Returns a `secret`, a `qr_code` (base64 PNG), and an `otpauth_url` for
deep linking into authenticator apps.

Display the QR code to the user. Most users will scan it with their
authenticator app.

**Step 2 — Verify and enable**

```json
POST /api/auth/totp/verify
{ "code": "123456" }
```

Confirms the user has successfully linked their app. TOTP is now active
for this account.

---

### Disable TOTP

```json
POST /api/auth/totp/disable
{ "code": "123456" }
```

A current TOTP code is required to disable — prevents an attacker with a
stolen session from quietly removing 2FA.

---

## 8. Sessions & Devices

### List tracked devices

The framework tracks the devices that have logged into the account.

```
GET /api/user/device
```

Returns device records including `user_agent`, `ip`, `created`, and
`last_seen`. Useful for showing the user "where you're logged in" on a
security settings page.

---

### Invalidate all sessions

Rotating the user's `auth_key` immediately invalidates every outstanding
JWT signed with the old key. The cleanest way to do this is a password
change (which rotates `auth_key` automatically) or via a supported
logout-all endpoint if your deployment implements one.

Email change confirm also rotates `auth_key` as a side effect — so after a
successful email change the user is effectively logged out everywhere except
the session that performed the confirm.

---

### Register device for push notifications

```json
POST /api/account/devices/push/register
{
  "device_token": "...",
  "device_id": "unique-device-id",
  "platform": "ios"
}
```

```json
POST /api/account/devices/push/unregister
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

## 11. Files

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

## 12. Activity Log

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

## 13. QR Codes

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

## 14. Realtime Events Reference

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
| Delete passkey | DELETE | `/api/account/passkeys/<id>` | Required |
| Setup TOTP | POST | `/api/auth/totp/setup` | Required |
| Verify & enable TOTP | POST | `/api/auth/totp/verify` | Required |
| Disable TOTP | POST | `/api/auth/totp/disable` | Required |
| List devices | GET | `/api/user/device` | Required |
| Register push device | POST | `/api/account/devices/push/register` | Required |
| Unregister push device | POST | `/api/account/devices/push/unregister` | Required |
| Generate personal API key | POST | `/api/auth/generate_api_key` | Required |
| List notifications | GET | `/api/account/notification` | Required |
| Mark notification read | POST | `/api/account/notification/<id>` | Required |
| List own files | GET | `/api/fileman/file` | Required |
| Delete own file | DELETE | `/api/fileman/file/<id>` | Required |
| Upload encrypted file | POST | `/api/filevault/file/upload` | Required |
| Download encrypted file (get token) | POST | `/api/filevault/file/<id>/unlock` | Required |
| View own activity log | GET | `/api/logit/log` | Required + `view_logs` |
| Generate QR code | GET/POST | `/api/qrcode` | Public |