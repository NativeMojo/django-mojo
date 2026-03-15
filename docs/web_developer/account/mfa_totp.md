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
  "data": { "is_enabled": true }
}
```

After a successful confirm:

- TOTP is active for this account
- `requires_mfa` is set to `true` on the user record
- All future **password logins** will be interrupted with an MFA challenge
  (see Group 2 below)

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
```

---

## Error Responses

| Status | Endpoint group | Cause |
|--------|---------------|-------|
| `400` | Management | Setup not started — call `/api/account/totp/setup` first |
| `400` | Management | Invalid or already-used code during confirm |
| `401` | Auth | `mfa_token` is invalid or expired |
| `403` | Auth | TOTP code is wrong |
| `403` | Auth | TOTP not enabled on this account |