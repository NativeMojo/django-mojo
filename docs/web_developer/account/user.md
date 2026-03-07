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
| POST | `/api/auth/verify/email/send` | required | Send email verification link |
| GET | `/api/auth/verify/email/confirm` | public | Confirm email via link |
| POST | `/api/auth/verify/phone/send` | required | Send SMS verification code |
| POST | `/api/auth/verify/phone/confirm` | required | Confirm phone via code |

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
3. A name derived from the username/email (e.g. `alice.smith` → `Alice Smith`)

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

### Protected Fields

| Field | Who can set it |
|---|---|
| `is_superuser` | Superusers only |
| `is_staff` | Superusers only |
| `permissions` | Users with `manage_users` (or matching `USER_PERMS_PROTECTION` rules) |

Attempts to set these fields without the required permission return `403`.

### Name Validation

`display_name`, `first_name`, and `last_name` are checked for inappropriate content on save. A blocked value returns a `400` error. Changed fields only are re-checked on updates.

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
| `basic` | id, display_name, username, last_activity, is_active, avatar |
| `default` | id, display_name, username, email, phone_number, permissions, metadata, is_active, avatar, org |
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

The email contains a link to `/api/auth/verify/email/confirm?token=ev:<token>`.

---

### Confirm Email

**GET** `/api/auth/verify/email/confirm?token=<token>`

Public endpoint — designed to be clicked directly from an email client. No `Authorization` header required.

```
GET /api/auth/verify/email/confirm?token=ev:3a9f...
```

**Response:**

```json
{"status": true, "message": "Email verified"}
```

- Tokens are single-use and expire after 24 hours (configurable via `EMAIL_VERIFY_TOKEN_TTL`).
- On success, `is_email_verified` is set to `true` on the user's account.

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
| SMS delivery failure | `400 Failed to send SMS — check your phone number` |
| Already verified | `200` success, no SMS sent |

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

## Filtering

```
GET /api/user?is_active=true
GET /api/user?search=alice
GET /api/user?org=5
```
