# User API — REST API Reference

## Endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/api/user` | required | List users |
| POST | `/api/user` | required | Create user |
| GET | `/api/user/<id>` | required | Get user |
| POST/PUT | `/api/user/<id>` | required | Update user |
| DELETE | `/api/user/<id>` | required | Delete user |
| GET | `/api/user/me` | required | Get current user |
| POST | `/api/user/<id>` body `{"disable": {...}}` | `manage_users` | Disable user (block) — see [Disable Lifecycle](#disable-lifecycle) |
| POST | `/api/user/<id>` body `{"reactivate": {...}}` | `manage_users` | Reactivate a disabled user |
| POST | `/api/user/me` body `{"change_username": {...}}` | self | Self-service username change (recommended over `/api/auth/username/change`) |
| POST | `/api/user/me` body `{"revoke_sessions": {...}}` | self | Self-service global logout (recommended over `/api/auth/sessions/revoke`) |
| POST | `/api/user/me` body `{"confirm_totp": {"code":"..."}}` | self | TOTP enrolment confirm (recommended over `/api/account/totp/confirm`) |
| POST | `/api/user/me` body `{"regenerate_totp_codes": {"code":"..."}}` | self | Regenerate TOTP recovery codes (recommended over `/api/account/totp/recovery-codes/regenerate`) |
| POST | `/api/user/me` body `{"disable_totp": true}` | self | Disable TOTP (recommended over `DELETE /api/account/totp`) |
| GET | `/api/auth/manage/throttle?user_id=N` | `manage_users` | Read login attempt counter |
| POST | `/api/auth/manage/clear_rate_limit` | `manage_users` | Clear login throttle for a user |
| POST | `/api/auth/verify/email/send` | required | Send email verification link |
| GET | `/api/auth/verify/email/confirm` | public | Confirmation **page** the emailed link opens — renders only, verifies nothing |
| POST | `/api/auth/email/verify/confirm` | public (token) | Verify-only confirm — sets `is_email_verified`, issues no session |
| POST | `/api/auth/verify/phone/send` | required | Send SMS verification code |
| POST | `/api/auth/verify/phone/confirm` | required | Confirm phone via code |
| POST | `/api/account/admin/user/password/reset` | global `users`/`manage_users`; fresh interactive auth | Send a dedicated audited reset link for a selected user |
| POST | `/api/account/admin/user/password/temporary` | same | Set and reveal a real temporary password once |
| POST | `/api/auth/password/forced` | single-use `tp:` credential | Replace a temporary password before any normal session can be issued |

---

## Get Current User

**GET** `/api/user/me`

```bash
curl -H "Authorization: Bearer <token>" https://api.example.com/api/user/me
```

**Response (default graph):**

```json
{
  "status": true,
  "data": {
    "id": 42,
    "display_name": "Alice Smith",
    "full_name": "Alice Smith",
    "first_name": "Alice",
    "last_name": "Smith",
    "username": "alice@example.com",
    "email": "alice@example.com",
    "phone_number": "+15551234567",
    "is_email_verified": true,
    "is_phone_verified": false,
    "requires_mfa": false,
    "has_passkey": false,
    "permissions": {"manage_reports": true},
    "metadata": {},
    "is_active": true,
    "last_login": "2024-01-15T09:00:00Z",
    "last_activity": "2024-01-15T10:30:00Z",
    "avatar": null,
    "org": {"id": 5, "name": "Acme Corp"}
  }
}
```

### `full_name`

A read-only computed field. Returns the best available name in priority order:

1. `first_name` + `last_name` (if either is set)
2. `display_name`
3. A name derived via priority chain: email local-part → friendly random placeholder (e.g. `Brave Tiger`) → username. Phone numbers are intentionally NEVER used here to avoid PII leakage.

---

## Update Own Profile

**POST** `/api/user/<id>` or **POST** `/api/user/me`

Users can update their own record (owner permission). Admins with `manage_users` can update any user.

```json
{
  "display_name": "Alice J. Smith",
  "first_name": "Alice",
  "last_name": "Smith",
  "phone_number": "+15551234567"
}
```

`avatar` accepts a positive numeric File id, a valid inline base64/data URL, or
`null`. Numeric strings and other ambiguous shapes return 400. An avatar File
must be active, completed, groupless, image-classified, and owned by the actor
who uploaded it. Admin-on-behalf updates use an admin-owned File and retain
that ownership; they do not transfer the File to the target user. `null`
detaches the relation without deleting the File.

### Protected Fields

`users` and `manage_users` are treated as equivalent for User admin operations — anywhere "admin tier" is listed, either perm is sufficient (and superusers always qualify).

| Field | Who can set it |
|---|---|
| `is_superuser` | Superusers only |
| `is_staff` | Superusers only |
| `is_dob_verified` | Superusers only |
| `is_email_verified`, `is_phone_verified` | Admin tier (force-verify / unverify) |
| `requires_mfa` | Admin tier |
| `email`, `username`, `phone_number` (replace) | Admin tier |
| `phone_number` (clear or first-set) | Anyone with edit access |
| `is_active`, `org`, `org_id` | Admin tier |
| `permissions` (most keys) | Admin tier (matching `USER_PERMS_PROTECTION` rules) |
| `new_password` (admin reset) | Admin tier — no `current_password` needed |
| `requires_password_change` | Server only; clients cannot set or clear it directly |

### Forced password response

When `requires_password_change` is set, every successful login or token-reissue
path returns `data.requires_password_change=true`, a `tp:`
`forced_password_token`, and the basic User graph. It returns no access,
refresh, MFA, or group token. Submit the one-time credential to
`POST /api/auth/password/forced` as
`{"token":"tp:...","new_password":"a new strong password"}`. The route is
public but requires that short-lived credential, is geofence checked, and is
limited to 10 attempts per IP per 600 seconds. On success the response is the
ordinary JWT login package. Do not persist the temporary password or the `tp:`
credential in browser storage, telemetry, downloads, or logs.

Attempts to set these fields without the required permission return `403`.

For credentials: self-service users without an admin perm cannot direct-write `email`/`username`/`phone_number` (replace). They must use the dedicated change flows (`POST /api/auth/{email,phone}/change/request` → `/confirm`, or `POST /api/auth/username/change`) which verify ownership of the new channel via OTP/link.

### Name Validation

`display_name`, `first_name`, and `last_name` are checked for inappropriate content on save. Changed fields only are re-checked on updates.

Name checks are **advisory**: a flagged value is logged and the save is still allowed. Name fields will not return a `400` for content reasons — the server's substring-based profanity matching over-blocks legitimate names (e.g. names containing common South-Asian or place-name substrings). Comment and chat moderation are unaffected and continue to hard-block flagged content.

---

## List Users

Requires `view_users` or `manage_users` permission.

**GET** `/api/user`

```
GET /api/user?search=alice&is_active=true&sort=-created&start=0&size=20
```

**Response:**

```json
{
  "status": true,
  "count": 3,
  "start": 0,
  "size": 20,
  "data": [
    {
      "id": 42,
      "display_name": "Alice",
      "username": "alice@example.com",
      "is_active": true
    }
  ]
}
```

---

## Available Graphs

| Graph | Fields |
|---|---|
| `basic` | id, uuid, display_name, username, last_login, last_activity, is_active, is_email_verified, is_phone_verified, is_dob_verified, requires_password_change, avatar |
| `default` | id, uuid, first_name, last_name, display_name, username, email, phone_number, last_login, last_activity, permissions, metadata, is_active, is_superuser, is_email_verified, is_phone_verified, is_dob_verified, dob, requires_mfa, requires_password_change, has_passkey, avatar, org |
| `full` | All fields |

```
GET /api/user/42?graph=basic
GET /api/user?graph=basic&size=50
```

---

## Update Permissions (Admin Only)

Requires `manage_users` permission.

```json
{
  "permissions": {
    "manage_reports": true,
    "view_analytics": true
  }
}
```

Permissions are merged (not replaced).

---

## Email Verification

### Send Verification Email

**POST** `/api/auth/verify/email/send`

Sends a verification link to the authenticated user's email address. If the email is already verified, returns success immediately without sending.

```bash
curl -X POST \
  -H "Authorization: Bearer <token>" \
  https://api.example.com/api/auth/verify/email/send
```

**Response:**

```json
{"status": true, "message": "Verification email sent"}
```

The email contains a link to `{BASE_URL}/api/auth/verify/email/confirm?token=ev:<token>` — this deployment's own confirmation page.

If the email provider did not accept the message (no mailbox configured, provider refusal or outage) the endpoint returns **HTTP 503** with a fixed, safe retry message instead of a false success — offer a Retry button:

```json
{"status": false, "code": 503, "error": "Unable to send the email right now. Please try again in a few minutes."}
```

An account with no email address on file gets **HTTP 400** (`No email address on account`) — retrying will not help. See [email_verification.md](email_verification.md) for the full contract.

---

### Confirm Email

**GET** `/api/auth/verify/email/confirm?token=<token>`

Public endpoint — designed to be clicked directly from an email client. No `Authorization` header required.

```
GET /api/auth/verify/email/confirm?token=ev:3a9f...
```

Returns an **HTML confirmation page**, not JSON. Since #3257 it verifies
nothing: it does not validate or consume the token, does not touch the account,
and names no account. A mail scanner, link preview or browser prefetch opening
the URL changes nothing.

**POST** `/api/auth/email/verify/confirm`

What the page's button calls, and what an SPA handling the token itself should
call. Public — the token is the credential.

```json
{ "token": "ev:3a9f..." }
```

**Response:**

```json
{"status": true, "message": "Email verified", "data": {"email": "alice@example.com"}}
```

- Tokens are single-use and expire after 24 hours (configurable via `EMAIL_VERIFY_TOKEN_TTL`).
- On success, `is_email_verified` is set to `true` on the user's account.
- **No JWT is issued** and `last_login` is untouched — verifying an address is
  not signing in. Use `POST /api/auth/email/verify` when you do want the token
  exchanged for a session.

---

### Auto-Verify via Invite Link

When a user accepts an **invite link** for the first time (`POST /api/auth/password/reset/token` with no prior login), `is_email_verified` is set automatically — receiving and clicking the invite proves inbox ownership. No separate verification step is needed.

**Magic login** (`POST /api/auth/magic/login`) also auto-verifies email on use for the same reason.

---

## Phone Verification

### Send SMS Code

**POST** `/api/auth/verify/phone/send`

Sends a 6-digit verification code to the phone number on the user's account. The number must be set on the account before calling this endpoint.

```bash
curl -X POST \
  -H "Authorization: Bearer <token>" \
  https://api.example.com/api/auth/verify/phone/send
```

**Response:**

```json
{"status": true, "message": "Verification code sent"}
```

**Errors:**

| Condition | Response |
|---|---|
| No phone number on account | `400 No phone number on account` |
| Phone number invalid/un-normalizable | `400 Phone number is invalid` |
| SMS transport did not accept the message (misconfiguration, provider refusal or outage) | `503` `{"status": false, "code": 503, "error": "Unable to send the text message right now. Please try again in a few minutes."}` — offer a Retry button |
| Provider rejected the recipient number (invalid, blocked, or not SMS-capable) | `400` `{"status": false, "code": 400, "error": "This phone number cannot receive text messages."}` — retrying with the same number will not help |
| Already verified | `200` success, no SMS sent |

The 503 mirrors the email send contract above: the body is fixed, no provider error text is ever exposed, and the code is still generated — a retry simply rotates it. Retries share this endpoint's rate limit of **5 requests per 300 seconds per IP**, so keep the retry manual. Deployments running the legacy `MOJO_APP_STATUS_200_ON_ERROR` shim receive both failure bodies over HTTP 200, with the real `code` inside — read `status`/`code` from the body.

---

### Confirm Phone Code

**POST** `/api/auth/verify/phone/confirm`

```json
{"code": "482917"}
```

**Response:**

```json
{"status": true, "message": "Phone verified"}
```

- Codes are 6 digits, single-use, and expire after 10 minutes (configurable via `PHONE_VERIFY_CODE_TTL`).
- On success, `is_phone_verified` is set to `true` on the user's account.

---

## Admin Password Reset (for another user)

Admins with `manage_users` can reset any user's password without knowing the current one:

**POST** `/api/user/<target_id>`

```json
{
  "new_password": "NewPass##123"
}
```

No `current_password` field needed. Password strength validation still applies.

For self-service password change, the user must include `current_password`:

```json
{
  "new_password": "NewPass##123",
  "current_password": "OldPass##456"
}
```

---

## Filtering

```
GET /api/user?is_active=true
GET /api/user?search=alice
GET /api/user?org=5
```

---

## Disable Lifecycle

Admins manage user `is_active` state through two named POST_SAVE_ACTIONS. Writing the bare `is_active` field directly still works (`{"is_active": false}`), but does not populate the disable namespace or emit audit events — the actions below are the recommended path for any new code.

### Disable a user

**POST** `/api/user/<id>`  · requires `manage_users`

```json
{"disable": {"reason": "admin", "note": "TOS violation §4"}}
```

`reason` must be one of: `admin`, `abuse`. Server-set reasons (`inactive`, `anonymized`) are rejected from REST.

Effect: `is_active=False`, populates `metadata.protected.disable` with `{reason, at, by_user_id, by_username, note}`. Also clears any `disable.warning` block.

**Disable is instant (kill switch).** The user's `auth_key` is rotated in the
same update as the flip, and their live websocket connections are dropped —
every outstanding token (including API-key-derived tokens) is rejected on
its very next request with a generic error, whether that request lands in
under a second or the JWT wouldn't otherwise have expired for hours. There
is no propagation delay to plan around.

### Reactivate a user

**POST** `/api/user/<id>`  · requires `manage_users`

```json
{"reactivate": {"note": "Appeal granted"}}
```

Effect: `is_active=True`. Appends a history entry to `disable.history` (FIFO cap 20) with the prior disable context plus `reactivated_at`, `reactivated_by_user_id`, `reactivated_by_username`, `reactivated_note`.

**Reactivation does not restore old sessions.** Because disable rotated the
user's `auth_key`, tokens issued before the disable remain invalid after
reactivation — the user must log in again to get a working token.

### Read disable state

`metadata` is included in the default and list graphs, so `data.metadata.protected.disable` is on every user response. Distinguish admin-disabled vs auto-disabled vs anonymized via `disable.reason`.

| `disable.reason` | Meaning |
|---|---|
| `admin` | Admin-disabled |
| `abuse` | Disabled for TOS / abuse |
| `inactive` | Auto-disabled by inactivity sweep |
| `anonymized` | Permanently erased via self-deactivate (irreversible) |
| `null` and `is_active=true` | Active |

### Read login throttle status

**GET** `/api/auth/manage/throttle?user_id=42`  (or `?username=alice`) · requires `manage_users` as a **global** grant (`@md.requires_global_perms` — no group/member fallback)

```json
{
  "status": true,
  "data": {
    "count": 7,
    "limit": 10,
    "window": 900,
    "retry_after_seconds": 0
  }
}
```

`count` is the number of failed-login attempts in the current sliding window. `retry_after_seconds > 0` means the user is currently locked out. Pure read — does not modify the counter. Pass `key=login` (default; only `login` is supported in v1).

### Clear login throttle

**POST** `/api/auth/manage/clear_rate_limit` · requires `manage_users` as a **global** grant (`@md.requires_global_perms` — no group/member fallback)

```json
{"key": "login", "user_id": 42}
```

Use this to manually unlock a user after a failed-login lockout.
