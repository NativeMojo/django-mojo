# API Keys (JWT “API Tokens”)

This document describes how to generate and use **API Keys** for server-to-server or automation access.

In MOJO, an “API Key” is a **JWT access token** generated for the currently authenticated user, with:
- an **expiration**
- an **allowlist of source IPs** (`allowed_ips`)

The API key is validated like a normal JWT, plus an additional IP restriction check.

---

## Generate an API Key

### Endpoint

- **Method:** `POST`
- **Path:** `auth/generate_api_key`
- **Auth:** Required (must already be logged in / have a valid auth session or token)

This endpoint generates a new API key **for the authenticated user** (`request.user`).

### Required parameters

- `allowed_ips` (list of strings)  
  A list of IP addresses allowed to use the token. **Must not be empty.**

### Optional parameters

- `expire_days` (int, default: `360`)  
  Intended expiration in days.

### Request example (curl)

```bash
curl -X POST "https://YOUR_HOST/api/auth/generate_api_key" \
  -H "Authorization: Bearer <YOUR_LOGIN_JWT>" \
  -H "Content-Type: application/json" \
  -d '{
    "allowed_ips": ["203.0.113.10", "203.0.113.11"],
    "expire_days": 360
  }'
```

### Successful response

The response is a standard response wrapper:

- `status`: boolean
- `data`: token payload

`data` contains:

- `jti` (string): token id (short hex)
- `expires` (datetime): token expiration time (UTC)
- `token` (string): the API key itself (JWT)

```json
{
  "status": true,
  "data": {
    "jti": "a1b2c3d4",
    "expires": "2026-01-21T12:34:56Z",
    "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."
  }
}
```

### Error cases

- Missing `allowed_ips` or `allowed_ips` is empty:
  - Error message: `Requires allowed_ips`

See `errors.md` for the standard error envelope and status codes used by your API.

---

## Using an API Key

Send the `token` you received as a Bearer token in the `Authorization` header, like any other JWT:

```bash
curl "https://YOUR_HOST/api/account/some/endpoint" \
  -H "Authorization: Bearer <API_KEY_TOKEN>"
```

---

## What’s inside the token?

When generated, the JWT payload includes (at minimum):

- `uid`: the user id
- `allowed_ips`: list of allowed source IPs
- `token_type`: `"access"`
- `iat`: issued-at (unix timestamp)
- `jti`: token id
- `exp`: expiration datetime

The token is signed using:
- algorithm: `JWT_ALGORITHM`
- key: the user’s `auth_key`

---

## Validation rules (server-side)

When an API key is presented, validation follows this flow:

1. Decode JWT to read `uid` (without signature verification) to locate the user.
2. Re-validate the JWT signature using the user’s `auth_key`.
3. If `allowed_ips` is a list, the request source IP must be in `allowed_ips`.
4. If valid, the user is considered authenticated for that request.

Failure modes you may see:
- Token expired
- Token has invalid signature
- Not allowed from location (source IP not in `allowed_ips`)
- Invalid token user / Invalid token data

---

## Operational notes / best practices

- **Store API keys like passwords.** Anyone who has the token can use it (subject to IP allowlist + expiry).
- **Prefer narrow IP allowlists.** Use specific egress IPs for servers/CI rather than `0.0.0.0/0`.
- **Rotate keys periodically.** Generate a new key and replace it in your automation.
