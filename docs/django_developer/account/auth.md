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

## `jwt_login` Helper

All login endpoints (password, OAuth, magic link, MFA complete, invite accept) issue tokens via the shared `jwt_login` helper:

```python
from mojo.apps.account.rest.user import jwt_login

return jwt_login(request, user)
```

### Extra response data

Pass an `extra` dict to merge additional fields into the response `data` without polluting the JWT payload:

```python
return jwt_login(request, user, extra={"is_new_user": True})
```

The JWT payload stays clean — `extra` fields are only in the HTTP response body. This is how the OAuth flow signals a newly-created account to the frontend.

### Webapp URL tracking

On every `jwt_login` call the framework captures the frontend origin from `request.DATA["webapp_base_url"]` or `HTTP_ORIGIN` and stores it on the user:

- `user.metadata["protected"]["orig_webapp_url"]` — set once at first login, never overwritten
- `user.metadata["protected"]["last_webapp_url"]` — updated on every subsequent login

These are used as a fallback in the `build_token_url` lookup chain (see [Token URLs](#token-urls)).

---

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

## Magic Login

Passwordless login via a signed single-use `ml:` token, delivered by email or SMS.

```python
from mojo.apps.account.utils.tokens import generate_magic_login_token

# Email (default)
token = generate_magic_login_token(user)
user.send_template_email("magic_login_link", {"token": token})

# SMS
token = generate_magic_login_token(user, channel="sms")
phonehub.send_sms(user.phone_number, f"Your login token: {token}")
```

`verify_magic_login_token(token)` returns `(user, channel)` — the channel is whichever was passed to `generate_magic_login_token`. On success the framework automatically marks `is_email_verified` or `is_phone_verified` depending on the channel.

Tokens are single-use and expire after `MAGIC_LOGIN_TOKEN_TTL` seconds (default 3600). The channel is stored encrypted in `mojo_secrets` and cleared on consume.

See the [Magic Login REST API](../../../web_developer/account/magic_login.md) for the full client-facing flow.

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

## Registration / Onboarding Patterns

Self-service registration is intentionally **not** a framework endpoint — what "registration" means varies too widely across projects (open signup, invite-only, domain restriction, approval queue, CAPTCHA, etc.). The framework provides every building block; each project composes them.

### Pattern A — Invite-only

The project creates accounts server-side and sends invite links. The user sets their password on first visit.

```python
# In your project's admin, management command, or REST endpoint:
from mojo.apps.account.models import User

user = User(email="alice@example.com")
user.username = user.generate_username_from_email()
user.set_unusable_password()
user.save()
user.send_invite()  # builds token URL, sends invite email
```

`send_invite()` accepts an optional `request` kwarg for multi-tenant URL resolution (see [Token URLs](#token-urls) below).

User clicks the link → `POST /api/auth/invite/accept` with the token → JWT issued, email verified.

### Pattern B — Open self-registration

The project adds its own registration endpoint:

```python
# In your project's REST layer:
@md.POST("auth/register")
@md.public_endpoint()
@md.strict_rate_limit("register", ip_limit=5, ip_window=300)
@md.requires_params("email", "password")
def on_register(request):
    from mojo.apps.account.models import User
    from mojo import errors as merrors

    email = request.DATA.email.lower().strip()
    if User.objects.filter(email=email).exists():
        raise merrors.ValueException("Email already registered")
    user = User(email=email)
    user.username = user.generate_username_from_email()
    user.set_new_password(request.DATA.password)
    user.save()
    # Trigger the framework's verify-send flow via internal call or redirect
    # user.send_template_email("email_verify_link", ...) or POST to /api/auth/email/verify/send
    return JsonResponse({"status": True, "message": "Check your email to verify your account."})
```

With `REQUIRE_VERIFIED_EMAIL = True`, the user cannot log in until they click the verification link — no additional gate logic needed.

### Framework primitives available

| Need | How |
|---|---|
| Create a user | `User(...).save()` + `user.save_password()` |
| Send invite link | `user.send_invite(request=request)` |
| Accept invite + set password | `POST /api/auth/invite/accept` |
| Send email verify link | `POST /api/auth/email/verify/send` |
| Confirm email verify | `POST /api/auth/email/verify` |
| Require verified email before login | `REQUIRE_VERIFIED_EMAIL = True` |
| OAuth auto-registration | Built into `auth/oauth/<provider>/complete` — gate with `OAUTH_ALLOW_REGISTRATION` |
| Block OAuth new-user creation | `OAUTH_ALLOW_REGISTRATION = False` in settings |

---

## Token URLs

Transactional token links (invite, magic login, password reset, email verify) are built as:

```
{base_url}{auth_path}?flow={flow}&token={token}
```

The frontend dispatches on `flow=` so only one auth path needs to be configured per tenant.

**Resolution order** (first non-empty wins):

1. `request.DATA["webapp_base_url"]` — per-request override (useful for multi-tenant admin portals)
2. `group.metadata["webapp_base_url"]` — tenant config, traverses parent chain
3. `user.org.metadata["webapp_base_url"]` — user's primary org
4. `WEBAPP_BASE_URL` setting
5. `user.metadata["protected"]["orig_webapp_url"]` — URL recorded at the user's first login
6. `HTTP_ORIGIN` header
7. `BASE_URL` setting (legacy fallback)

Auth path follows the same precedence with `group.metadata["webapp_auth_path"]` and `WEBAPP_AUTH_PATH` (default `"/auth"`).

Configure per tenant without a deploy:

```python
group.metadata["webapp_base_url"] = "https://app.acme.com"
group.metadata["webapp_auth_path"] = "/login"  # optional, default /auth
group.save()
```

## Incident Reporting

Failed login attempts, unknown usernames, and invalid password resets are automatically reported to the incident system with appropriate severity levels.

