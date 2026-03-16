# POST /api/auth/password/reset/token rejects invite tokens (iv: prefix)

**Type**: bug
**Status**: Resolved — 2026-03-16
**Date**: 2026-03-16

## Description

`POST /api/auth/password/reset/token` returns `{"error": "Invalid token kind"}` when called with an invite token (`iv:` prefix). Frontends that send users through a single "set password" flow using the token from the invite email are broken.

The endpoint calls `verify_password_reset_token(token)` which calls `_verify(token, expected_kind="pr")`. If the token is `iv:`, the kind check at `tokens.py:95` raises `ValueException("Invalid token kind")`.

## Observed

```
POST /api/auth/password/reset/token
{ "token": "iv:...", "new_password": "..." }

→ 400 {"error": "Invalid token kind"}
```

## Root Cause

`on_user_password_reset_token` always calls `verify_password_reset_token` regardless of token prefix. No kind detection before verification.

## Acceptance Criteria

- `POST /api/auth/password/reset/token` accepts both `pr:` and `iv:` tokens.
- When an `iv:` token is submitted: verify via `verify_invite_token`, mark `is_email_verified = True`, set password, issue JWT.
- When a `pr:` token is submitted: existing behaviour unchanged.
- No other token kinds accepted.

## Resolution

- `on_user_password_reset_token` (`rest/user.py`) now detects the token prefix before verifying:
  - `iv:` → `verify_invite_token`, sets `is_email_verified = True`
  - `pr:` → `verify_password_reset_token`, existing behaviour unchanged
  - anything else → raises `ValueException("Invalid token kind")`
- Extracted `User.check_password_strength(password)` from `set_new_password` so strength validation is reusable without the CRUD current-password requirement. Both reset paths (token and code) now call it.
- `set_new_password` delegates to `check_password_strength` — no behaviour change for CRUD flows.

## Files Changed

- `mojo/apps/account/rest/user.py` — prefix detection + `check_password_strength` call
- `mojo/apps/account/models/user.py` — extracted `check_password_strength`, simplified `set_new_password`
- `tests/test_accounts/invite_flow.py` — regression tests (all passing)
