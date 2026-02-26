# TOTP (Authenticator App) — REST API Reference

TOTP (Time-based One-Time Password) lets users authenticate with a 6-digit code from an authenticator app like Google Authenticator, Authy, or 1Password.

Supports two modes:
- **2FA** — required second step after password login
- **Standalone** — passwordless login with username + code only

---

## Setup (Authenticated)

The user must be logged in to register an authenticator app.

### Step 1 — Get QR Code

**POST** `/api/account/totp/setup`

```
Authorization: Bearer <access_token>
```

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

Display the `qr_code` image for the user to scan, and optionally show `secret` for manual entry.

### Step 2 — Confirm First Code

**POST** `/api/account/totp/confirm`

```json
{
  "code": "482910"
}
```

The user enters the first code from their app to confirm setup.

**Response:**

```json
{
  "status": true,
  "data": { "is_enabled": true }
}
```

TOTP is now active. Subsequent password logins will require a second factor.

### Disable TOTP

**DELETE** `/api/account/totp`

```
Authorization: Bearer <access_token>
```

```json
{ "status": true }
```

---

## Login with TOTP (2FA)

When TOTP is enabled, password login returns an `mfa_token` instead of a JWT.

### Step 1 — Password Login

**POST** `/api/login`

```json
{
  "username": "alice",
  "password": "mysecretpassword"
}
```

**Response when TOTP is enabled:**

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

The `mfa_token` expires in 5 minutes. If both TOTP and SMS are enabled, `mfa_methods` will contain both — the user can choose either method.

### Step 2 — Submit TOTP Code

**POST** `/api/auth/totp/verify`

```json
{
  "mfa_token": "a3f1c9d2...",
  "code": "482910"
}
```

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

The `mfa_token` is single-use — a new one is required if verification fails.

---

## Standalone TOTP Login (No Password)

**POST** `/api/auth/totp/login`

```json
{
  "username": "alice",
  "code": "482910"
}
```

Returns the same JWT response as above on success.

---

## Error Responses

| Status | Cause |
|--------|-------|
| `400` | Missing required params or TOTP not set up |
| `401` | Invalid or expired `mfa_token` |
| `403` | Invalid TOTP code |
