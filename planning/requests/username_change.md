# Request: Username Change

## Status
Pending

## Priority
High

## Summary

Add a self-service username change endpoint. Currently `username` is a
superuser-only write — the existing `_handle_existing_user_pre_save` guard
blocks non-superusers from changing it via the standard REST path. A dedicated
endpoint adds `current_password` confirmation as the proof-of-ownership step,
following the same pattern as the email and phone change flows already in the
framework.

---

## Endpoints

```
POST /api/auth/username/change
```

**Request (authenticated):**
```json
{
  "username": "new_username",
  "current_password": "currentpassword"
}
```

**Response:**
```json
{
  "status": true,
  "data": {
    "username": "new_username"
  }
}
```

---

## Behaviour

- Requires a valid Bearer token (`@md.requires_auth()`).
- `current_password` is required and must match the user's stored password.
  An incorrect password returns 401 and logs a security incident.
- `username` is required. Lowercased and trimmed before use.
- Validates uniqueness against existing usernames — returns 400 if taken.
- Calls `user.validate_username()` (existing method) to enforce format rules.
- On success: saves the new username and logs `username:changed` to the
  incident/audit table (existing pattern in `_handle_existing_user_pre_save`).
- Does **not** rotate `auth_key` — username is not used in JWT signing.
  The existing session continues uninterrupted.
- Does **not** require a confirmation step — password is sufficient proof.
- Rate-limited to prevent enumeration of taken usernames.

### What does NOT change
- Email address — use `POST /api/auth/email/change/request`
- Password — use `POST /api/user/me` with `old_password` + `password`
- JWT tokens remain valid after the change

---

## Settings

| Setting | Default | Purpose |
|---|---|---|
| `ALLOW_USERNAME_CHANGE` | `True` | Feature flag — set to `False` to disable the endpoint entirely |

---

## Files in Scope

| File | Change |
|---|---|
| `mojo/apps/account/rest/user.py` | Add `on_username_change` endpoint |
| `docs/web_developer/account/user_self_management.md` | Add section + quick reference row |
| `docs/web_developer/account/authentication.md` | Note that username is changeable via this flow |
| `docs/django_developer/account/user.md` | Note `ALLOW_USERNAME_CHANGE` setting |
| `CHANGELOG.md` | Entry under next version |

---

## Tests

Add to `tests/test_accounts/` (new file `username_change.py` or extend `test_accounts.py`):

- Happy path — username changes, old username is freed
- `current_password` wrong — returns 401, username not changed
- `current_password` missing — returns 400
- `username` taken — returns 400
- `username` same as current — returns 400 (must be different)
- `username` invalid format — returns 400
- `ALLOW_USERNAME_CHANGE = False` — returns 403
- Unauthenticated request — returns 403
- Username is lowercased on save
- Audit log entry written (`username:changed`)

---

## Edge Cases

- User has no password set (OAuth-only account) — return 400 with a clear
  message: `"No password set on this account. Use password reset to set one first."`
- Username containing only whitespace — reject (validate_username handles this)
- Username normalisation must happen before uniqueness check

---

## Out of Scope

- Email change — already implemented
- Forcing re-verification after username change (username is not a login
  verification target in this framework)
- Admin bulk-rename — superusers can still write `username` directly via
  standard REST