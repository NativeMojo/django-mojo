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
  "username": "alice" or "phone_number": "555 123-4321"
}
```

Always returns success to prevent account enumeration:

```json
{
  "status": true,
  "message": "If the account exists, a code was sent."
}
```

The hosted sign-in page (`/auth`) mirrors this honestly in its UX: it tells the user up front that a code arrives only if the number is already linked to an account, and offers a "Create an account" link in the SMS view for users who do not yet have one.

**This endpoint deliberately never reports the send outcome.** An unknown identifier, a real account with no phone number on file, and an account whose SMS send failed all return the byte-identical body above. Any difference would be an account-existence oracle, so there is nothing here for a client to branch on — the operator gets the signal through incident events instead.

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

## Pre-Registration Phone Verification

`POST /api/auth/phone/register/start` (see [Authentication](authentication.md#phone-based-registration-verify-then-register)) reports its send truthfully — unlike `/auth/sms/login` it has no account-existence check, so it has no enumeration surface and the bodies below are identical for registered and unregistered numbers:

| Condition | Status | Response |
|---|---|---|
| SMS transport did not accept the message (misconfiguration, provider refusal or outage) | 503 | `{"status": false, "code": 503, "error": "Unable to send the text message right now. Please try again in a few minutes."}` — retryable |
| Provider rejected the number itself (invalid, blocked, or not SMS-capable) | 400 | `{"status": false, "code": 400, "error": "This phone number cannot receive text messages."}` — retrying the same number will not help |

Neither failure returns a `session_token`: restart at step 1. Provider error text and codes never reach the client.

---

## Rate Limits

Each endpoint has its own per-IP bucket. They used to share a single 60/minute bucket by accident, which let code-request spam lock a legitimate user out of submitting their code.

| Endpoint | Limit |
|---|---|
| `POST /api/auth/sms/send` | 10 requests / 60s per IP |
| `POST /api/auth/sms/verify` | 10 requests / 60s per IP |
| `POST /api/auth/sms/login` | 10 requests / 60s per IP |
| `POST /api/auth/phone/register/start` | 5 requests / 300s per IP |
| `POST /api/auth/phone/register/verify` | 10 requests / 60s per IP |

Exceeding a bucket returns `429` with a `Retry-After` header.

---

## Error Responses

| Status | Cause |
|--------|-------|
| `400` | Missing required params; no phone number on account (`/api/auth/sms/send` only — `/api/auth/sms/login` never reveals this) |
| `401` | Invalid or expired `mfa_token` |
| `403` | Invalid or expired SMS code |
| `429` | Rate limit exceeded — see the table above |

`/api/auth/sms/login` is the exception: it returns `200` for every input, including a real account with no phone number on file. See the note under Step 1 above.
