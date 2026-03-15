# Passkeys (WebAuthn) — REST API Reference

Passkeys are FIDO2/WebAuthn credentials — fingerprint, Face ID, hardware key, etc. — that replace passwords entirely. The server uses FIDO2 challenge/response so the private key never leaves the user's device.

## How It Works

Each passkey is scoped to a **Relying Party ID (RP ID)**, which is the hostname of your app derived automatically from the `Origin` header. This means a passkey registered on `app.example.com` only works on `app.example.com`.

**Challenges are single-use** and expire after 5 minutes (stored in Redis).

**All requests must include an `Origin` header** matching your app's origin. Browsers send this automatically. In native apps or during development you must set it explicitly.

---

## Registration Flow

The user must already be logged in (JWT required) to register a passkey.

### Step 1 — Begin Registration

**POST** `/api/account/passkeys/register/begin`

Headers:
```
Authorization: Bearer <access_token>
Origin: https://your-app.example.com
```

Body: _(empty or `{}`)_

**Response:**

```json
{
  "status": true,
  "data": {
    "challenge_id": "a3f1c9...",
    "publicKey": {
      "rp": {
        "id": "your-app.example.com",
        "name": "MOJO"
      },
      "user": {
        "id": "<base64url-user-uuid>",
        "name": "alice",
        "displayName": "Alice"
      },
      "challenge": "<base64url-challenge>",
      "pubKeyCredParams": [
        {"type": "public-key", "alg": -7}
      ],
      "timeout": 60000,
      "excludeCredentials": [],
      "authenticatorSelection": {},
      "attestation": "none"
    },
    "expiresAt": "2024-01-01T12:05:00Z"
  }
}
```

Save `challenge_id` — you'll need it in step 2.

### Step 2 — Call the Browser API

Pass `publicKey` directly to `navigator.credentials.create()`:

```javascript
const beginResp = await fetch('/api/account/passkeys/register/begin', {
  method: 'POST',
  headers: {
    'Authorization': `Bearer ${accessToken}`,
    'Content-Type': 'application/json',
  },
  body: JSON.stringify({}),
});
const { data } = await beginResp.json();
const { challenge_id, publicKey } = data;

// Decode base64url fields the browser expects as ArrayBuffers
publicKey.challenge = base64urlToBuffer(publicKey.challenge);
publicKey.user.id = base64urlToBuffer(publicKey.user.id);
if (publicKey.excludeCredentials) {
  publicKey.excludeCredentials = publicKey.excludeCredentials.map(c => ({
    ...c,
    id: base64urlToBuffer(c.id),
  }));
}

const credential = await navigator.credentials.create({ publicKey });
```

### Step 3 — Complete Registration

**POST** `/api/account/passkeys/register/complete`

Headers:
```
Authorization: Bearer <access_token>
Origin: https://your-app.example.com
```

```json
{
  "challenge_id": "a3f1c9...",
  "friendly_name": "Touch ID on MacBook",
  "credential": {
    "id": "<base64url-credential-id>",
    "rawId": "<base64url-credential-id>",
    "type": "public-key",
    "response": {
      "clientDataJSON": "<base64url>",
      "attestationObject": "<base64url>"
    },
    "transports": ["internal"]
  }
}
```

`friendly_name` is optional — a human-readable label shown in the passkey list.

**Complete registration in JavaScript:**

```javascript
function bufferToBase64url(buffer) {
  return btoa(String.fromCharCode(...new Uint8Array(buffer)))
    .replace(/\+/g, '-').replace(/\//g, '_').replace(/=/g, '');
}

const completeResp = await fetch('/api/account/passkeys/register/complete', {
  method: 'POST',
  headers: {
    'Authorization': `Bearer ${accessToken}`,
    'Content-Type': 'application/json',
  },
  body: JSON.stringify({
    challenge_id,
    friendly_name: 'Touch ID on MacBook',
    credential: {
      id: credential.id,
      rawId: bufferToBase64url(credential.rawId),
      type: credential.type,
      response: {
        clientDataJSON: bufferToBase64url(credential.response.clientDataJSON),
        attestationObject: bufferToBase64url(credential.response.attestationObject),
      },
      transports: credential.response.getTransports?.() ?? [],
    },
  }),
});
const result = await completeResp.json();
// result.data is the saved Passkey record
```

**Response:** The saved passkey object (default graph).

---

## Login Flow

No JWT required — this is how users authenticate without a password.

### Step 1 — Begin Login

**POST** `/api/auth/passkeys/login/begin`

Headers:
```
Origin: https://your-app.example.com
```

```json
{
  "username": "alice"
}
```

`username` can be a username or email address.

**Response:**

```json
{
  "status": true,
  "data": {
    "challenge_id": "b7e2d1...",
    "publicKey": {
      "rpId": "your-app.example.com",
      "challenge": "<base64url-challenge>",
      "allowCredentials": [
        {
          "id": "<base64url-credential-id>",
          "type": "public-key",
          "transports": ["internal"]
        }
      ],
      "timeout": 60000,
      "userVerification": "preferred"
    },
    "expiresAt": "2024-01-01T12:05:00Z"
  }
}
```

### Step 2 — Call the Browser API

```javascript
const beginResp = await fetch('/api/auth/passkeys/login/begin', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ username }),
});
const { data } = await beginResp.json();
const { challenge_id, publicKey } = data;

// Decode base64url fields
publicKey.challenge = base64urlToBuffer(publicKey.challenge);
if (publicKey.allowCredentials) {
  publicKey.allowCredentials = publicKey.allowCredentials.map(c => ({
    ...c,
    id: base64urlToBuffer(c.id),
  }));
}

const credential = await navigator.credentials.get({ publicKey });
```

### Step 3 — Complete Login

**POST** `/api/auth/passkeys/login/complete`

Headers:
```
Origin: https://your-app.example.com
```

```json
{
  "challenge_id": "b7e2d1...",
  "credential": {
    "id": "<base64url-credential-id>",
    "rawId": "<base64url-credential-id>",
    "type": "public-key",
    "response": {
      "clientDataJSON": "<base64url>",
      "authenticatorData": "<base64url>",
      "signature": "<base64url>",
      "userHandle": "<base64url-or-null>"
    }
  }
}
```

**Complete login in JavaScript:**

```javascript
const completeResp = await fetch('/api/auth/passkeys/login/complete', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    challenge_id,
    credential: {
      id: credential.id,
      rawId: bufferToBase64url(credential.rawId),
      type: credential.type,
      response: {
        clientDataJSON: bufferToBase64url(credential.response.clientDataJSON),
        authenticatorData: bufferToBase64url(credential.response.authenticatorData),
        signature: bufferToBase64url(credential.response.signature),
        userHandle: credential.response.userHandle
          ? bufferToBase64url(credential.response.userHandle)
          : null,
      },
    },
  }),
});
const { data } = await completeResp.json();
// data.access_token, data.refresh_token, data.user — same shape as password login
```

**Response:** Same JWT token package as password login.

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

## Managing Passkeys

All management endpoints require authentication.

### List Passkeys

**GET** `/api/account/passkeys`

Returns all **enabled** passkeys for the authenticated user across all portals.
Disabled passkeys are excluded from the list by default — use
`GET /api/account/passkeys?is_enabled=false` to include them.

```json
{
  "status": true,
  "count": 2,
  "results": [
    {
      "id": 1,
      "friendly_name": "Touch ID on MacBook",
      "credential_id": "abc123...",
      "rp_id": "app.example.com",
      "is_enabled": true,
      "last_used": "2024-01-01T10:00:00Z",
      "created": "2024-01-01T08:00:00Z"
    }
  ]
}
```

### Rename a Passkey

**POST** `/api/account/passkeys/<id>`

```json
{
  "friendly_name": "YubiKey 5"
}
```

### Disable a Passkey

**POST** `/api/account/passkeys/<id>`

```json
{
  "is_enabled": false
}
```

Disabled passkeys are excluded from login challenges but not deleted.

### Delete a Passkey

**DELETE** `/api/account/passkeys/<id>`

```json
{
  "status": true
}
```

---

## Configuration

| Setting | Default | Purpose |
|---|---|---|
| `PASSKEYS_RP_NAME` | `"MOJO"` | Relying Party name shown in the browser's passkey prompt (e.g. `"Acme Corp"`) |

The RP ID is derived automatically from the `Origin` header — no setting required. Set `PASSKEYS_RP_NAME` in your Django settings to show your app's name in the authenticator dialog instead of the default.

```python
# settings.py
PASSKEYS_RP_NAME = "Acme Corp"
```

---

## Complete JavaScript Helper

A reusable utility covering the full flows:

```javascript
// utils/passkeys.js

function base64urlToBuffer(base64url) {
  const base64 = base64url.replace(/-/g, '+').replace(/_/g, '/');
  const binary = atob(base64);
  return Uint8Array.from(binary, c => c.charCodeAt(0)).buffer;
}

function bufferToBase64url(buffer) {
  return btoa(String.fromCharCode(...new Uint8Array(buffer)))
    .replace(/\+/g, '-').replace(/\//g, '_').replace(/=/g, '');
}

export async function registerPasskey(accessToken, friendlyName) {
  // Step 1: Begin
  const beginResp = await fetch('/api/account/passkeys/register/begin', {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${accessToken}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({}),
  });
  if (!beginResp.ok) throw new Error('Registration begin failed');
  const { data } = await beginResp.json();
  const { challenge_id, publicKey } = data;

  // Step 2: Browser prompt
  publicKey.challenge = base64urlToBuffer(publicKey.challenge);
  publicKey.user.id = base64urlToBuffer(publicKey.user.id);
  if (publicKey.excludeCredentials) {
    publicKey.excludeCredentials = publicKey.excludeCredentials.map(c => ({
      ...c, id: base64urlToBuffer(c.id),
    }));
  }
  const credential = await navigator.credentials.create({ publicKey });

  // Step 3: Complete
  const completeResp = await fetch('/api/account/passkeys/register/complete', {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${accessToken}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      challenge_id,
      friendly_name: friendlyName,
      credential: {
        id: credential.id,
        rawId: bufferToBase64url(credential.rawId),
        type: credential.type,
        response: {
          clientDataJSON: bufferToBase64url(credential.response.clientDataJSON),
          attestationObject: bufferToBase64url(credential.response.attestationObject),
        },
        transports: credential.response.getTransports?.() ?? [],
      },
    }),
  });
  if (!completeResp.ok) throw new Error('Registration complete failed');
  return completeResp.json();
}

export async function loginWithPasskey(username) {
  // Step 1: Begin
  const beginResp = await fetch('/api/auth/passkeys/login/begin', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username }),
  });
  if (!beginResp.ok) throw new Error('Login begin failed');
  const { data } = await beginResp.json();
  const { challenge_id, publicKey } = data;

  // Step 2: Browser prompt
  publicKey.challenge = base64urlToBuffer(publicKey.challenge);
  if (publicKey.allowCredentials) {
    publicKey.allowCredentials = publicKey.allowCredentials.map(c => ({
      ...c, id: base64urlToBuffer(c.id),
    }));
  }
  const credential = await navigator.credentials.get({ publicKey });

  // Step 3: Complete
  const completeResp = await fetch('/api/auth/passkeys/login/complete', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      challenge_id,
      credential: {
        id: credential.id,
        rawId: bufferToBase64url(credential.rawId),
        type: credential.type,
        response: {
          clientDataJSON: bufferToBase64url(credential.response.clientDataJSON),
          authenticatorData: bufferToBase64url(credential.response.authenticatorData),
          signature: bufferToBase64url(credential.response.signature),
          userHandle: credential.response.userHandle
            ? bufferToBase64url(credential.response.userHandle)
            : null,
        },
      },
    }),
  });
  if (!completeResp.ok) throw new Error('Login complete failed');
  return completeResp.json(); // { data: { access_token, refresh_token, user } }
}
```

---

## Error Responses

| Status | Cause |
|--------|-------|
| `400` | Missing or invalid `Origin` header |
| `401` | Registration endpoint called without a valid JWT |
| `403` | Unknown username, no passkeys registered for this portal, invalid/expired challenge, or cloned credential detected |

```json
{
  "status": false,
  "code": 403,
  "error": "No passkeys registered for this portal"
}
```

---

## Notes

- **Multi-portal:** A user can have passkeys on multiple portals. Each portal's passkeys are isolated by hostname (RP ID).
- **Challenges expire** after 5 minutes. If the user takes too long, restart from step 1.
- **`Origin` header:** Browsers set this automatically. In React Native or non-browser clients, set it manually to match the hostname you registered with.
- **Conditional UI (autofill):** To support passkey autofill on username inputs, pass `mediation: "conditional"` and omit `allowCredentials`. This is an advanced browser feature — see the [WebAuthn spec](https://www.w3.org/TR/webauthn-3/).
