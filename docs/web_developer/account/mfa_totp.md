# TOTP (Authenticator App) — REST API Reference

TOTP (Time-based One-Time Password) lets users authenticate with a 6-digit code
from an authenticator app such as Google Authenticator, Authy, or 1Password.

There are **two completely separate groups of endpoints**. Use the right group
for the right page — they are not interchangeable.

| Group | URL prefix | Who calls it | When |
|---|---|---|---|
| **Account management** | `/api/account/totp/…` | Logged-in user, settings page | Enable / disable TOTP on the account |
| **Authentication** | `/api/auth/totp/…` | Anyone, login page | Use TOTP to prove identity and get a JWT |

---

## Group 1 — Account Management (Settings Page)

These endpoints **require a valid `Authorization: Bearer` token**. They live on
a settings or security page where the user manages their own account. They do
not issue JWTs.

### Check whether TOTP is already enabled

There is no dedicated status endpoint. Read it from the user profile:

```
GET /api/user/me
Authorization: Bearer <access_token>
```

The response includes:

```json
{
  "data": {
    "requires_mfa": true
  }
}
```

`requires_mfa: true` means TOTP (or another second factor) is active on this account.

---

### Enable TOTP — Step 1: Generate a secret

**POST** `/api/account/totp/setup`

```
Authorization: Bearer <access_token>
```

No request body required.

**Response:**

```json
{
  "status": true,
  "data": {
    "secret": "JBSWY3DPEHPK3PXP",
    "uri": "otpauth://totp/MOJO:alice?secret=JBSWY3DPEHPK3PXP&issuer=MOJO",
    "qr_code": "data:image/png;base64,iVBORw0KGgo..."
  }
}
```

Display the `qr_code` image so the user can scan it with their authenticator app.
Show `secret` as a fallback for manual entry. The `uri` value can be used to
deep-link directly into some authenticator apps.

The secret is saved server-side but **TOTP is not active yet** — the user must
confirm a valid code before it is enabled.

---

### Enable TOTP — Step 2: Confirm with first code

**POST** `/api/account/totp/confirm`

```
Authorization: Bearer <access_token>
```

```json
{
  "code": "482910"
}
```

The user opens their authenticator app and types the 6-digit code. Submitting a
valid code proves the app is correctly linked and activates TOTP on the account.

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

After a successful confirm:

- TOTP is active for this account
- `requires_mfa` is set to `true` on the user record
- **8 single-use recovery codes** are generated and returned
- All future **password logins** will be interrupted with an MFA challenge
  (see Group 2 below)

> **Important:** Display the recovery codes immediately and instruct the user to
> store them in a safe place. The plaintext codes are shown **only once** — they
> are stored as bcrypt hashes on the server and cannot be retrieved again.

---

### Disable TOTP

**DELETE** `/api/account/totp`

```
Authorization: Bearer <access_token>
```

No request body required.

**Response:**

```json
{ "status": true }
```

TOTP is immediately deactivated. The user's next password login will return a
JWT directly with no MFA challenge.

> **Note:** The current session remains valid. Only future logins are affected.

---

### View masked recovery codes

**GET** `/api/account/totp/recovery-codes`

```
Authorization: Bearer <access_token>
```

No request body required.

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

`remaining` is the number of unused recovery codes. Each masked code shows only
the first 4 hex characters (the hint) — enough for the user to identify which
codes they have already used.

Returns `400` if TOTP is not enabled on the account.

---

### Regenerate recovery codes

**POST** `/api/account/totp/recovery-codes/regenerate`

```
Authorization: Bearer <access_token>
```

```json
{
  "code": "482910"
}
```

A valid TOTP code from the authenticator app is **required** to authorize
regeneration. This **invalidates all previous recovery codes** and returns 8
new ones.

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
codes. The old codes are permanently invalidated.

---

## Group 2 — Authentication (Login Page)

These endpoints are **public** (no `Authorization` header). They live on login
or authentication flows. They always result in either a JWT or an error — they
never manage account state.

### 2FA login — Step 1: Password login (triggers MFA challenge)

This is the normal login endpoint. When the user has TOTP enabled, it returns
an `mfa_token` instead of a JWT.

**POST** `/api/login`

```json
{
  "username": "alice",
  "password": "mysecretpassword"
}
```

**Response when TOTP (or any MFA) is enabled:**

```json
{
  "status": true,
  "data": {
    "mfa_required": true,
    "mfa_token": "a3f1c9d2...",
    "mfa_methods": ["totp"],
    "expires_in": 300
  }
}
```

`mfa_methods` lists every available second factor for this account. If both
TOTP and SMS are enabled the list contains both — the user picks one.

The `mfa_token` expires in **5 minutes** and is single-use.

---

### 2FA login — Step 2: Submit TOTP code

**POST** `/api/auth/totp/verify`

```json
{
  "mfa_token": "a3f1c9d2...",
  "code": "482910"
}
```

Both fields are required. The `mfa_token` from Step 1 proves the user passed
the password check; the `code` proves they have the authenticator app.

**Response:**

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

The `mfa_token` is consumed on use. If the code is wrong, the token is **not**
consumed — the user may try again with the same `mfa_token` until it expires.

---

### Recovery login (lost authenticator device)

Use this when the user has an `mfa_token` from a password login but **cannot
provide a TOTP code** because they lost their authenticator device.

**POST** `/api/auth/totp/recover`

```json
{
  "mfa_token": "a3f1c9d2...",
  "recovery_code": "a1b2-c3d4-e5f6"
}
```

Both fields are required. The `mfa_token` proves the user passed the password
check; the `recovery_code` is one of the 8 codes issued at TOTP setup or
regeneration.

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

**Behavior:**

- The recovery code is **consumed on use** — it cannot be used again.
- An incident is logged on the user's account (`totp:recovery_used`).
- If the last remaining code is consumed, the user receives a security
  notification prompting them to generate new codes.
- The `mfa_token` is consumed on use (same as the TOTP verify flow).

**Typical UI flow:**

1. User enters username + password → receives `mfa_token` + `mfa_required: true`
2. User clicks "Lost your authenticator? Use a recovery code"
3. User enters one of their saved recovery codes
4. Frontend calls `POST /api/auth/totp/recover` with `mfa_token` + `recovery_code`
5. On success, store the JWT and redirect to the app

---

### Standalone login (no password, TOTP code only)

For flows where TOTP is the sole credential — no password required.

**POST** `/api/auth/totp/login`

```json
{
  "username": "alice",
  "code": "482910"
}
```

Returns the same JWT response as 2FA Step 2 above on success.

This endpoint is useful for internal tools, kiosk apps, or any context where
issuing a password is impractical and TOTP alone is the accepted trust level.

---

## Quick Decision Guide

```
Building a settings / security page?
  -> Use /api/account/totp/*   (requires auth, manages the account)

Building a login page or handling a post-login MFA challenge?
  -> Use /api/auth/totp/*      (public, issues a JWT)

User lost their authenticator app?
  -> Use /api/auth/totp/recover with mfa_token + recovery_code

User wants to view or refresh their recovery codes?
  -> Use /api/account/totp/recovery-codes (GET) or .../regenerate (POST)
```

---

## Error Responses

| Status | Endpoint group | Cause |
|--------|---------------|-------|
| `400` | Management | Setup not started — call `/api/account/totp/setup` first |
| `400` | Management | Invalid or already-used code during confirm |
| `400` | Management | TOTP not enabled (GET/regenerate recovery codes) |
| `401` | Auth | `mfa_token` is invalid or expired |
| `403` | Auth | TOTP code is wrong |
| `403` | Auth | Recovery code is invalid or already used |
| `403` | Auth | TOTP not enabled on this account |
| `403` | Management | Invalid TOTP code during recovery-code regeneration |