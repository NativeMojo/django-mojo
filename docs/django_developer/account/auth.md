# Authentication Flow — Django Developer Reference

## Overview

Authentication is JWT-based. `AuthMiddleware` validates Bearer tokens on every request and populates `request.user`. All REST endpoints in `mojo/apps/account/rest/user.py` handle the full auth lifecycle.

## Login

**Endpoint:** `POST /api/login` (also: `/api/auth/login`)

Required params: `username`, `password`

```python
# Pseudocode of what the framework does
user = User.objects.filter(Q(username=username) | Q(email=username)).last()
if not user.check_password(password):
    raise PermissionDeniedException()
token_package = JWToken(user.get_auth_key()).create(uid=user.id, ip=request.ip)
```

Returns `access_token`, `refresh_token`, `expires_in`, and `user` dict.

## MFA Challenge (Login with MFA enabled)

MFA is opt-in per user via the `requires_mfa` boolean field (default `False`). Your app sets this when creating or updating users — the framework never forces it automatically. When `requires_mfa=True`, the login endpoint does **not** return a JWT. Instead it returns an MFA challenge:

```json
{
  "status": true,
  "data": {
    "mfa_required": true,
    "mfa_token": "<short-lived token>",
    "mfa_methods": ["sms"],
    "expires_in": 300
  }
}
```

The client must detect `mfa_required: true` and route the user to the appropriate second factor.

**MFA methods:**
- `"sms"` — user has a verified `phone_number`; use the SMS OTP flow
- `"totp"` — user has an active TOTP device; use the TOTP flow (enrolling TOTP auto-sets `requires_mfa=True`)
- `"passkey"` — user has a registered passkey; can be used as second factor

**Completing MFA:**
- SMS: `POST /api/auth/sms/verify` with `mfa_token` + `code`
- TOTP: `POST /api/auth/totp/verify` with `mfa_token` + `code`

Both return the standard JWT response (`access_token`, `refresh_token`, `expires_in`, `user`) on success.

The `mfa_token` is single-use and expires in `expires_in` seconds (default 300).

## Token Refresh

**Endpoint:** `POST /api/refresh_token` (also: `/api/token/refresh`)

Required param: `refresh_token`

Validates the refresh token and issues a new token pair.

## Password Reset

Two flows supported:

### Code-based (OTP)
1. `POST /api/auth/forgot` with `email` + `method=code`
2. 6-digit code emailed, stored encrypted in user secrets
3. `POST /api/auth/password/reset/code` with `email`, `code`, `new_password`
4. Returns new JWT on success

### Link-based
1. `POST /api/auth/forgot` with `email` + `method=link`
2. Signed token emailed
3. `POST /api/auth/password/reset/token` with `token`, `new_password`
4. Returns new JWT on success

## API Keys

Long-lived JWTs restricted by IP allowlist.

**Generate own key:** `POST /api/auth/generate_api_key`
- Required: `allowed_ips` (list), `expire_days` (max 360)

**Admin generate for another user:** `POST /api/auth/manage/generate_api_key`
- Required: `allowed_ips`, `expire_days`, `uid`
- Requires: `manage_users` permission

## Current User

**Endpoint:** `GET /api/user/me`

Returns the authenticated user's own record using their `pk`.

## Middleware Auth Flow

1. `Authorization: Bearer <token>` header parsed
2. Bearer type looked up in handler registry
3. Default handler: `User.validate_jwt(token)` → returns `(user, error)`
4. `request.user` set to the resolved user (or anonymous)
5. `request.group` set if `group` param present and user is a member

## Custom Bearer Handlers

Register additional token types via settings:

```python
# settings.py
MOJO_BEARER_HANDLERS = {
    "ApiKey": "myapp.auth.validate_api_key",
}
```

The handler receives the raw token string and must return `(user_or_None, error_or_None)`.

## User CRUD Endpoint

```python
@md.URL('user')
@md.URL('user/<int:pk>')
def on_user(request, pk=None):
    return User.on_rest_request(request, pk)
```

- **List:** requires `view_users` or `manage_users`
- **Create:** handled via invite flow or direct admin creation
- **Get/Update own record:** allowed via `owner` permission (`OWNER_FIELD = "self"`)
- **Update others:** requires `manage_users`

## Incident Reporting

Failed login attempts, unknown usernames, and invalid password resets are automatically reported to the incident system with appropriate severity levels.
