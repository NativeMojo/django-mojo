# POST /api/auth/password/reset/token rejects invite tokens (iv: prefix)

**Type**: bug
**Status**: Resolved — 2026-03-17 (reopened and fixed)
**Date**: 2026-03-16

## Description

`POST /api/auth/password/reset/token` returns `400` when called with an invite token (`iv:` prefix). Confirmed still failing on production (latest release) on 2026-03-17.

The fix was merged locally and passes tests. The error message on production is `"Invalid token"` — not `"Invalid token kind"` (which the old buggy code would produce). This means either:
1. The fix is deployed but the specific token being tested is expired/consumed/invalid, OR
2. The fix was not included in the deployed release

## Observed (production, 2026-03-17)

```
POST /api/auth/password/reset/token
{
    "token": "iv:65794a31615751694f6941...9aa9b6",
    "new_password": "FakePassword123"
}

→ 400 {"error": "Invalid token", "code": 400, "status": false, "server": "s3"}
```

Note: error is `"Invalid token"`, NOT `"Invalid token kind"`. This distinction matters for diagnosis.

## Root Cause (original)

`on_user_password_reset_token` always called `verify_password_reset_token` regardless of token prefix. No kind detection before verification.

## Fix (merged locally, tests passing)

- `on_user_password_reset_token` (`rest/user.py`) detects the token prefix before verifying:
  - `iv:` → `verify_invite_token`, sets `is_email_verified = True`
  - `pr:` → `verify_password_reset_token`, existing behaviour unchanged
  - anything else → raises `ValueException("Invalid token kind")`

## Files Changed

- `mojo/apps/account/rest/user.py` — prefix detection + `check_password_strength` call
- `mojo/apps/account/models/user.py` — extracted `check_password_strength`, simplified `set_new_password`
- `tests/test_accounts/invite_flow.py` — regression tests (all passing)

## Production Diagnosis Steps

Run these in `python manage.py shell` on the production server:

### Step 1 — Confirm which code is deployed

```python
import inspect
from mojo.apps.account.rest.user import on_user_password_reset_token
print(inspect.getsource(on_user_password_reset_token))
```

If the fix is deployed you'll see `token.startswith("iv:")`.
If not, you'll see `verify_password_reset_token(token)` called unconditionally.

### Step 2 — Decode the token to inspect its contents

```python
token = "iv:65794a31615751694f6941794f446b7349434a3063794936494445334e7a4d334e5451304f54597349434a7164476b694f6941695a6e644e615768535a4564745a54685849697767496d7470626d51694f6941695a5764705a4445694f3d3d9aa9b6"
kind, hex_token = token.split(":", 1)
hex_payload = hex_token[:-6]  # strip 6-char signature
raw = bytes.fromhex(hex_payload).decode("utf-8")
from mojo.helpers import crypto
obj = crypto.b64_decode(raw)
print("Token payload:", obj)
```

### Step 3 — Check expiry

```python
import time
now = int(time.time())
issued = obj["ts"]
ttl = 604800  # 7 days (INVITE_TOKEN_TTL default)
print(f"Token age: {now - issued}s")
print(f"TTL: {ttl}s")
print(f"Expired: {now - issued > ttl}")
```

### Step 4 — Check the user's stored JTI

```python
from mojo.apps.account.models import User
user = User.objects.get(pk=obj["uid"])
stored_jti = user.get_secret("invite_jti")
print(f"Token JTI:  {obj['jti']}")
print(f"Stored JTI: {stored_jti}")
print(f"Match: {stored_jti == obj['jti']}")
print(f"User: {user.username} (last_login={user.last_login})")
```

### Step 5 — Re-send a fresh invite token and test

```python
from mojo.apps.account.utils import tokens
fresh_token = tokens.generate_invite_token(user)
print("Fresh token:", fresh_token)
# Then POST this token to /api/auth/password/reset/token
```

## Acceptance Criteria

- `POST /api/auth/password/reset/token` accepts both `pr:` and `iv:` tokens on production.
- When an `iv:` token is submitted: sets password, marks email verified, returns JWT.
- `"Invalid token kind"` is never returned for a valid `iv:` token.
