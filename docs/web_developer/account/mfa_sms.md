# SMS OTP — REST API Reference

SMS OTP sends a 6-digit code to the user's verified phone number via SMS.

Supports two modes:
- **2FA** — required second step after password login
- **Standalone** — passwordless login with username + SMS code only

A phone number must be on the user's account (`phone_number` field) and marked verified (`is_phone_verified: true`) for SMS MFA to be active.

---

## Login with SMS OTP (2FA)

When SMS MFA is active, password login returns an `mfa_token` instead of a JWT.

### Step 1 — Password Login

**POST** `/api/login`

```json
{
  "username": "alice",
  "password": "mysecretpassword"
}
```

**Response when SMS MFA is active:**

```json
{
  "status": true,
  "data": {
    "mfa_required": true,
    "mfa_token": "a3f1c9d2...",
    "mfa_methods": ["sms"],
    "expires_in": 300
  }
}
```

### Step 2 — Send SMS Code

**POST** `/api/auth/sms/send`

```json
{
  "mfa_token": "a3f1c9d2..."
}
```

Sends a 6-digit code to the user's phone number. Returns a fresh `mfa_token` (the original is consumed).

**Response:**

```json
{
  "status": true,
  "data": {
    "mfa_token": "b8e2f1a3...",
    "expires_in": 300
  }
}
```

### Step 3 — Submit SMS Code

**POST** `/api/auth/sms/verify`

```json
{
  "mfa_token": "b8e2f1a3...",
  "code": "839201"
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

The SMS code expires after 10 minutes. The `mfa_token` expires after 5 minutes.

---

## Standalone SMS Login (No Password)

### Step 1 — Request SMS Code

**POST** `/api/auth/sms/login`

```json
{
  "username": "alice"
}
```

Always returns success to prevent account enumeration:

```json
{
  "status": true,
  "message": "If the account exists, a code was sent."
}
```

### Step 2 — Submit SMS Code

**POST** `/api/auth/sms/verify`

```json
{
  "username": "alice",
  "code": "839201"
}
```

Returns the same JWT response as the 2FA flow on success.

---

## Multiple MFA Methods

If the user has both TOTP and SMS enabled, the login response includes both methods:

```json
{
  "mfa_required": true,
  "mfa_token": "a3f1c9d2...",
  "mfa_methods": ["totp", "sms"],
  "expires_in": 300
}
```

The user can complete the second factor using either method. Use the same `mfa_token` for whichever method they choose.

---

## Error Responses

| Status | Cause |
|--------|-------|
| `400` | Missing required params, no phone number on account |
| `401` | Invalid or expired `mfa_token` |
| `403` | Invalid or expired SMS code |
