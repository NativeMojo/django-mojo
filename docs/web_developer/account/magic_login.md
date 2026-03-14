# Magic Login Links — REST API Reference

A magic login link lets a user log in without a password — delivered via **email** (default) or **SMS**. The link/token contains a signed `ml:` token that is single-use and expires after 1 hour.

This is distinct from password reset links (`pr:` tokens), which require a new password to be submitted. A magic login link issues a JWT directly.

---

## Flow

### Step 1 — Request Magic Link

**POST** `/api/auth/magic/send`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `email` or `username` or `phone_number` | Yes | Used to look up the account |
| `method` | No | `"email"` (default) or `"sms"` |

**Email (default):**

```json
{
  "email": "alice@example.com"
}
```

**SMS:**

```json
{
  "phone_number": "+15550001234",
  "method": "sms"
}
```

Always returns success to prevent account enumeration:

```json
{
  "status": true,
  "message": "If account is in our system a login link was sent."
}
```

For `method=email`, an email is sent using the `magic_login_link` template with a `{{ token }}` variable.

For `method=sms`, the token is sent as a text message to the user's verified phone number. If the user has no phone number on file the request is silently ignored.

### Step 2 — Complete Login

**POST** `/api/auth/magic/login`

```json
{
  "token": "ml:616c69636..."
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
    "user": {
      "id": 42,
      "username": "alice",
      "display_name": "Alice"
    }
  }
}
```

---

## Token Format

Tokens are prefixed so your frontend can identify them before decoding:

| Prefix | Kind | Endpoint |
|--------|------|----------|
| `ml:` | Magic login | `POST /api/auth/magic/login` |
| `pr:` | Password reset | `POST /api/auth/password/reset/token` |

Submitting the wrong token kind to an endpoint is rejected — a `pr:` token cannot be used to log in, and an `ml:` token cannot be used to reset a password.

Tokens are **single-use**. Once consumed (successfully or not after signature validation), they cannot be reused.

---

## Verification side-effect

On successful login, the channel used to deliver the token is recorded and the matching verified flag is set automatically:

| Channel | Flag set |
|---------|----------|
| `email` | `is_email_verified = true` |
| `sms` | `is_phone_verified = true` |

## Email Template

Create a `magic_login_link` email template in the database. The template receives:

| Variable | Value |
|----------|-------|
| `{{ token }}` | The full `ml:` prefixed token string |
| `{{ user.display_name }}` | User's display name |
| `{{ user.username }}` | User's username |

Example link in your template:

```
https://your-app.example.com/auth/magic?token={{ token }}
```

Your frontend page at that URL extracts the token and posts it to `/api/auth/magic/login`.

## SMS delivery

When `method=sms` the raw `ml:` token is sent as a text message. Your frontend should provide a text input where the user can paste it, then submit it to `/api/auth/magic/login` the same way as an email link.

---

## JavaScript Example

```javascript
// On the magic link landing page
async function completeMagicLogin() {
  const params = new URLSearchParams(window.location.search);
  const token = params.get('token');

  const resp = await fetch('/api/auth/magic/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token }),
  });

  const { data } = await resp.json();
  // data.access_token, data.refresh_token, data.user
}
```

---

## Error Responses

| Status | Cause |
|--------|-------|
| `400` | Invalid token, wrong token kind (`pr:` submitted), or expired/already used |
