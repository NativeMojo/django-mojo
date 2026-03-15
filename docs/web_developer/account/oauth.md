# OAuth / Social Login — REST API Reference

OAuth allows users to log in with a third-party provider (Google, etc.) without a password. The server handles the token exchange — your frontend only needs to redirect the user and handle the callback.

**Supported providers:** `google`

> **Trusted second factor.** OAuth is treated as a strong, trusted authentication event. Completing an OAuth login automatically confirms the user's email address and bypasses any local MFA requirement — see [Security Behaviour](#security-behaviour) below.

---

## Flow Overview

```
1. GET  /api/auth/oauth/<provider>/begin   → get authorization URL
2. Redirect user to authorization URL
3. Provider redirects back to your app with ?code=...&state=...
4. POST /api/auth/oauth/<provider>/complete → exchange code, get JWT
```

---

## Step 1 — Get Authorization URL

**GET** `/api/auth/oauth/google/begin`

**Response:**

```json
{
  "status": true,
  "data": {
    "auth_url": "https://accounts.google.com/o/oauth2/v2/auth?client_id=...&state=abc123...",
    "state": "abc123..."
  }
}
```

Redirect the user to `auth_url`. Store `state` if you need it client-side (the server validates it automatically).

---

## Step 2 — Redirect User

```javascript
const { data } = await fetch('/api/auth/oauth/google/begin').then(r => r.json());
window.location.href = data.auth_url;
```

The user authenticates with Google and is redirected back to your `redirect_uri` with `?code=...&state=...`.

---

## Step 3 — Complete Login

**POST** `/api/auth/oauth/google/complete`

```json
{
  "code": "<code-from-callback>",
  "state": "<state-from-callback>"
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

Same JWT response as password login. Store and use tokens as normal.

---

## Auto-Link Behaviour

The server automatically resolves which account to log in to:

1. **Existing OAuth connection** — a previous login with this provider/account is found → log in that user directly
2. **Matching email** — no connection but an existing account has the same email → create a connection, mark email as verified, and log in
3. **New user** — no match → create a new account and connection, mark email as verified

No manual linking step is required.

### Email Verification on Auto-Link

Because the provider has confirmed ownership of the email address, the framework marks `is_email_verified = True` on the resolved account in all three cases above:

- **Existing connection** — user was already verified when they first connected; flag unchanged
- **Email match** — if the matched account was not yet verified, it is marked verified at link time
- **New user** — account is created with `is_email_verified = True`

This means a user who signed up via password but never clicked their verification email will be automatically verified the first time they log in with Google (or another provider) using the same address.

---

## Security Behaviour

### Email Verification

OAuth confirmation is treated as equivalent to clicking an email verification link. The provider vouches for ownership of the address, so no separate verification step is needed.

If your project has `REQUIRE_VERIFIED_EMAIL` enabled, users who log in via OAuth are unaffected by the gate — their email is marked verified by the OAuth flow itself.

### MFA Bypass

A user with MFA enabled (`requires_mfa = True`) is **not** challenged for TOTP or SMS after a successful OAuth login. The JWT is issued directly.

**Why:** OAuth is itself a trusted second factor:

- The user has authenticated to an external identity provider
- The provider may have enforced its own MFA (Google Workspace policies, Advanced Protection, etc.)
- The CSRF `state` token is single-use and Redis-backed — replay and CSRF attacks are prevented
- The authorization `code` is exchanged server-side only

Requiring an additional local second factor after a trusted provider assertion is redundant for most applications. If your project has a strict policy requiring local MFA regardless of OAuth, contact your backend developer to configure a project-level override.

---

## JavaScript Example

```javascript
// Begin — call on "Login with Google" button click
async function startGoogleLogin() {
  const resp = await fetch('/api/auth/oauth/google/begin');
  const { data } = await resp.json();
  window.location.href = data.auth_url;
}

// Complete — call in your OAuth callback page
async function completeGoogleLogin() {
  const params = new URLSearchParams(window.location.search);
  const resp = await fetch('/api/auth/oauth/google/complete', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      code: params.get('code'),
      state: params.get('state'),
    }),
  });
  const { data } = await resp.json();
  // data.access_token, data.refresh_token, data.user
}
```

---

## Configuration

Add to your Django settings:

```python
GOOGLE_CLIENT_ID = "your-client-id.apps.googleusercontent.com"
GOOGLE_CLIENT_SECRET = "your-client-secret"

# The URL Google redirects back to after login
# Must match one of the authorised redirect URIs in Google Console
OAUTH_REDIRECT_URI = "https://your-app.example.com/auth/oauth/google/complete"
```

If `OAUTH_REDIRECT_URI` is not set, the server builds it from the request `Origin` header as `<origin>/auth/oauth/<provider>/complete`.

### Optional Settings

| Setting | Default | Purpose |
|---|---|---|
| `GOOGLE_SCOPES` | `"openid email profile"` | OAuth scopes requested from Google |
| `OAUTH_STATE_TTL` | `600` | Seconds a CSRF state token is valid before it expires |

---

## Error Responses

| Status | Cause |
|--------|-------|
| `400` | Unknown provider or missing params |
| `401` | Invalid or expired OAuth state token |
| `403` | Account is disabled |
