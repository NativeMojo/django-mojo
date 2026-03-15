# Request: Username Change

## Status
Ready to build

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

## Decisions Made

- **Password required** — `current_password` is the proof of ownership. Wrong
  password → 401, incident logged, no change.
- **OAuth-only accounts** — if the user has no usable password
  (`not user.has_usable_password()`), return 400 with:
  `"No password set on this account. Use password reset to set one first."`
  Never attempt `check_password()` on a passwordless account.
- **Validation** — call `user.validate_username()` (existing method in
  `mojo/apps/account/models/user.py` ~L512). That method already runs
  `content_guard.check_username()` for non-email usernames and raises
  `ValueException` on blocked content. No duplicate guard needed.
- **No confirmation step** — password proof is sufficient. No email link, no
  code, no token. Single endpoint.
- **No `auth_key` rotation** — username is not part of JWT signing. Existing
  session continues uninterrupted.
- **Feature flag** — `ALLOW_USERNAME_CHANGE` (default `True`). Read at
  call time via `settings.get()`, not at import time.
- **Normalisation** — lowercase and strip before any check. Normalisation
  must happen before the uniqueness check.

---

## Endpoint

```
POST /api/auth/username/change
Authorization: Bearer <access_token>
```

**Request:**
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

## Behaviour (ordered)

1. Check `ALLOW_USERNAME_CHANGE` setting — return 403 if disabled.
2. Require authentication (`@md.requires_auth()`).
3. Require `username` and `current_password` params.
4. If `not user.has_usable_password()` → return 400:
   `"No password set on this account. Use password reset to set one first."`
5. Verify `current_password` — if wrong, 401 + log incident `username:change_failed`.
6. Lowercase and strip `new_username`.
7. Check `new_username != user.username` — return 400 if same.
8. Set `user.username = new_username` and call `user.validate_username()` —
   this runs the content_guard check and raises `ValueException` on failure.
9. Check uniqueness: if another user has this username → return 400 `"Username already taken"`.
10. Save. Log `username:changed` via `user.log(kind="username:changed", ...)`.
11. Return `{ "status": true, "data": { "username": new_username } }`.

---

## Settings

| Setting | Default | Purpose |
|---|---|---|
| `ALLOW_USERNAME_CHANGE` | `True` | Feature flag — set `False` to disable the endpoint entirely |

---

## Files in Scope

| File | Change |
|---|---|
| `mojo/apps/account/rest/user.py` | Add `on_username_change` endpoint |
| `docs/web_developer/account/user_self_management.md` | Add section under Profile + quick reference row |
| `docs/django_developer/account/user.md` | Note `ALLOW_USERNAME_CHANGE` setting |
| `CHANGELOG.md` | Entry under next version |

---

## Tests

New file: `tests/test_accounts/username_change.py`

- Happy path — username changes, response contains new username
- `current_password` wrong — 401, username unchanged
- `current_password` missing — 400
- `username` missing — 400
- `username` taken by another user — 400
- `username` same as current — 400
- `username` invalid content (content_guard blocked) — 400
- `username` all whitespace — 400 (validate_username catches)
- `username` is lowercased on save
- `ALLOW_USERNAME_CHANGE = False` — 403 (skip via `TestitSkip` when setting not active on server)
- Unauthenticated request — 403
- OAuth-only user (no usable password) — 400 with correct message
- Audit log entry written (`username:changed`)

> **Note:** Never use `override_settings` in testit tests. Tests that require
> `ALLOW_USERNAME_CHANGE=False` must read the live setting and raise
> `TestitSkip` when the required condition is not present on the server.

---

## Edge Cases

- Normalisation (lowercase + strip) must happen before uniqueness check and
  before `validate_username()` so the stored value and the checked value are
  the same.
- `validate_username()` reads `self.username` — set `user.username = new_username`
  before calling it.
- Uniqueness check must exclude the current user
  (`User.objects.filter(username=new_username).exclude(pk=user.pk).exists()`).

---

## Out of Scope

- Email change — already implemented (`POST /api/auth/email/change/request`)
- Password change — already implemented (`POST /api/user/me` with `old_password`)
- Forcing re-verification after username change (username is not a verified
  identity channel in this framework)
- Admin bulk-rename — superusers can still write `username` directly via
  standard REST (`POST /api/user/<id>`)

---

## Key References

- `mojo/apps/account/models/user.py` — `validate_username()` ~L512,
  `has_usable_password()` (inherited from `AbstractBaseUser`),
  `_handle_existing_user_pre_save()` for existing audit log pattern
- `mojo/apps/account/rest/user.py` — `on_phone_change_confirm` for
  `current_password` verification pattern
- `mojo/helpers/content_guard.py` — `check_username()` called inside
  `validate_username()`
