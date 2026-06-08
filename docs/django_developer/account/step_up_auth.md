# Step-Up Authentication (Recent-Auth Gate)

Optional, **off-by-default** "recent authentication" requirement for sensitive
operations. A valid JWT proves *who* you are; step-up additionally requires that
the token was minted from a genuine login **recently**, so a leaked/long-lived
token cannot perform account-takeover-grade actions (change username/email/phone,
revoke sessions, enable/disable MFA, add passkeys, deactivate account, or an admin
acting on another user) without a fresh re-authentication.

It works for **passwordless** accounts (passkey / SMS-OTP) — re-auth is whatever
factor the user has, never a password prompt — and it applies to **admins** too
(the actor's own token must be fresh).

## How it works

- **`auth_time` claim.** `jwt_login` stamps `auth_time` (epoch seconds) into every
  token at a genuine authentication event. This is **unconditional** — always
  stamped, regardless of whether enforcement is on.
- **Survives refresh.** The token refresh endpoint carries the original `auth_time`
  forward unchanged (it is *not* reset on refresh — a refresh is not a fresh login,
  and resetting it would defeat the gate). `iat` is **not** usable for freshness
  because it is regenerated on every refresh.
- **Enforcement is gated.** Sensitive handlers call the freshness check; if the
  token's `auth_time` is older than the window they get HTTP **440**
  `reauth_required`.

## Configuration

```python
FRESH_AUTH_WINDOW = 0      # seconds; 0 (default) = disabled (no enforcement)
# e.g. 300 to require a login within the last 5 minutes for sensitive ops
```

Default `0` means upgrades are inert: nothing changes until an operator opts in.
Stamping happens regardless, so when you later raise the window, existing tokens
already carry `auth_time` — no flag-day mass re-auth. Legacy tokens minted before
this shipped have no `auth_time` and are treated as stale (fail-closed) once a
window is enabled, forcing one re-auth.

## Applying the gate

### Endpoints — `@md.requires_fresh_auth()`

```python
@md.POST("auth/sessions/revoke")
@md.requires_auth()
@md.requires_fresh_auth()          # inner to requires_auth
def on_sessions_revoke(request):
    ...
```

Place it **inner to** `@md.requires_auth()`. Pass `seconds=` to override the global
window for one endpoint: `@md.requires_fresh_auth(seconds=120)`. Inert when
`FRESH_AUTH_WINDOW <= 0`, so it is safe to apply broadly.

### Model actions / service code

```python
from mojo.apps.account.services import fresh_auth

fresh_auth.require_fresh(request)            # raises ReauthRequiredException if stale
# or, non-raising:
if fresh_auth.is_fresh(request, seconds=300):
    ...
```

The `User` model's sensitive `on_action_*` handlers call `self._require_fresh_auth()`
(which delegates to `fresh_auth.require_fresh(self.active_request)`).

## Bypass rules

`fresh_auth.is_fresh(request, seconds=None)` returns **True** (allow) when:

- the window is `<= 0` (disabled), or
- `request` is `None` (non-REST/internal call), or
- the request is **not** JWT-authenticated — i.e. `request.bearer != "bearer"`
  (API-key callers bypass; machine credentials have no interactive login to be
  "recent").

It returns **False** (stale) when the JWT has no `auth_time` claim (legacy) or
`now - auth_time > window`.

## The response — `ReauthRequiredException` / HTTP 440

`mojo.errors.ReauthRequiredException(reason="reauth_required", code=440, status=440)`
→ body `{"status": false, "error": "reauth_required", "code": 440}` at HTTP 440.
This is a deliberate **third** state, distinct from `403` (authorized: permission
denied) and `401` (token invalid/expired). 440 avoids the common client
`401 → token refresh` path, which cannot help here (a refresh preserves the stale
`auth_time`). Clients branch on `code == 440` / `error == "reauth_required"` and
drive a step-up re-auth.

## Step-up re-auth

There is **no dedicated step-up endpoint**. Every existing verify/login flow
already calls `jwt_login`, which stamps a fresh `auth_time`. So a client that hits
a 440 re-runs the appropriate flow for the signed-in user (passkey assertion,
`POST /api/auth/sms/verify`, `POST /api/account/totp/verify`, or password login),
swaps in the returned tokens, and retries the operation.

## Testing

Set the window per-request with the `X-Mojo-Test-Fresh-Auth-Window` header
(test-mode only — gated by `mojo.helpers.test_mode.is_test_request`), so tests run
in parallel without server reloads:

```python
resp = opts.client.post("/api/auth/sessions/revoke", {},
                        headers={"X-Mojo-Test-Fresh-Auth-Window": "300"})
```

See `tests/test_auth/fresh_auth.py`.
