# Request: Linked OAuth Accounts (Social Connections)

## Status
Ready to build

## Priority
Medium

## Summary
Expose `OAuthConnection` to authenticated users so they can see which OAuth
providers are linked to their account and unlink them. Also fix a security gap
in OAuth user creation where new passwordless users are not explicitly marked
as having no usable password.

---

## Background

`OAuthConnection` already has:
- `CAN_DELETE = True`
- `VIEW_PERMS = ["owner", "manage_users"]`
- `SAVE_PERMS = ["manage_users"]`
- `OWNER_FIELD = "user"`

`GET /api/account/oauth_connection` and `DELETE /api/account/oauth_connection/<id>`
may already function correctly for the account owner. This needs to be confirmed
and one critical safety guard added.

Additionally: when a new user is created via OAuth (`_find_or_create_user` in
`mojo/apps/account/rest/oauth.py`), `set_unusable_password()` is never called.
Django's `AbstractBaseUser` starts with `password = ""` (empty string). This means
a brute-force attempt against an empty string would technically pass
`check_password("")` in some edge cases. The fix is to explicitly call
`user.set_unusable_password()` before saving any new OAuth-created user so the
Django sentinel value is correctly stored and no password-based login is possible.

---

## Decisions Made

- Verify existing `GET /api/account/oauth_connection` and
  `DELETE /api/account/oauth_connection/<id>` work correctly for the `owner` role
  before adding any new endpoint logic.
- Add a pre-delete safety guard: if the user has no usable password AND this is
  their last `OAuthConnection`, block the unlink with a clear 400 error.
- Fix `_find_or_create_user` in `oauth.py`: call `user.set_unusable_password()`
  on all new OAuth-created users before `user.save()`.
- The guard uses Django's `user.has_usable_password()` — the standard check.
- The guard check failure (e.g., DB error mid-check) must deny the delete
  (fail-closed).

---

## Scope

### In scope
1. Verify `GET /api/account/oauth_connection` lists only the requesting user's
   connections (owner filter working correctly via `RestMeta.OWNER_FIELD`)
2. Verify `DELETE /api/account/oauth_connection/<id>` works for the owner
3. Add a pre-delete safety guard (see detail below)
4. Fix OAuth user creation: call `set_unusable_password()` for new OAuth users
5. Document both endpoints in `docs/web_developer/account/oauth.md` under a new
   "Managing Connections" section
6. Add the two endpoints and connections section to
   `docs/web_developer/account/user_self_management.md` (section and quick
   reference table)
7. Tests covering all cases including the lockout guard

### Out of scope
- Adding new OAuth providers (separate concern)
- Linking a new provider from within a settings page (that is the existing
  OAuth begin/complete flow — no change needed there)
- Any UI for the connect flow

---

## Safety Guard Detail

Implement in a custom `DELETE` endpoint in `mojo/apps/account/rest/oauth.py`
rather than relying on the standard CRUD path, so the guard logic can be applied
cleanly:

```
DELETE /api/account/oauth_connection/<id>
Authorization: Bearer <access_token>
```

Guard logic before deletion:
1. Fetch the `OAuthConnection` — if not found or not owned by `request.user`,
   return 404 (do not leak existence to other users).
2. Check `request.user.has_usable_password()`.
3. Count `OAuthConnection.objects.filter(user=request.user, is_active=True)`.
4. If password is not usable AND active connection count == 1 → return 400:
   `"Cannot unlink your only login method. Set a password first."`
5. If guard passes → delete the connection, return `{ "status": true }`.
6. If the guard check itself raises an exception → deny the delete and log an
   incident (fail-closed).

Note: `manage_users` admins bypass the guard — they can delete any connection
regardless. The guard only applies to the account owner.

---

## OAuth User Creation Fix

In `mojo/apps/account/rest/oauth.py`, `_find_or_create_user`, path 3 (new user):

```python
# Current (missing set_unusable_password):
user = User(email=email)
user.username = user.generate_username_from_email()
if display_name:
    user.display_name = display_name
user.is_email_verified = True
user.save()

# Fixed:
user = User(email=email)
user.username = user.generate_username_from_email()
if display_name:
    user.display_name = display_name
user.is_email_verified = True
user.set_unusable_password()   # <-- add this
user.save()
```

This ensures `user.has_usable_password()` returns `False` correctly for all
OAuth-only accounts, which is what the lockout guard (and `username_change`
endpoint) rely on.

---

## API Shape

### List linked providers

```
GET /api/account/oauth_connection
Authorization: Bearer <access_token>
```

Response (default graph):
```json
{
  "status": true,
  "count": 1,
  "results": [
    {
      "id": 7,
      "provider": "google",
      "email": "alice@gmail.com",
      "is_active": true,
      "created": "2026-01-10T09:00:00Z"
    }
  ]
}
```

### Unlink a provider

```
DELETE /api/account/oauth_connection/<id>
Authorization: Bearer <access_token>
```

Success:
```json
{ "status": true }
```

Blocked (last connection, no password):
```json
{
  "status": false,
  "code": 400,
  "error": "Cannot unlink your only login method. Set a password first."
}
```

Not found or not owned:
```json
{ "status": false, "code": 404 }
```

---

## Files in Scope

| File | Change |
|---|---|
| `mojo/apps/account/rest/oauth.py` | Add custom `DELETE` endpoint with lockout guard |
| `mojo/apps/account/rest/oauth.py` | Fix `_find_or_create_user` — add `set_unusable_password()` |
| `docs/web_developer/account/oauth.md` | Add "Managing Connections" section |
| `docs/web_developer/account/user_self_management.md` | Add connections to section 6 and quick reference table |
| `tests/test_accounts/oauth.py` | Extend with list, unlink, and lockout guard tests |
| `CHANGELOG.md` | Entry under current version |

---

## Tests Required

- Owner can list their own connections; does not see another user's connections
- Owner can unlink a connection when they have a usable password set
- Owner can unlink one of two connections even without a usable password
  (guard only fires when it is the last connection)
- Unlink blocked when `has_usable_password() == False` and connection count == 1
- `manage_users` admin can delete any connection regardless of guard
- 404 returned when attempting to delete another user's connection
- New OAuth user created with `has_usable_password() == False` (usable=False)
- Existing OAuth user linked by email match: `has_usable_password()` reflects
  whatever was already set (not changed by this flow)

---

## Constraints

- No migrations (model already exists)
- No Python type hints
- Use `request.DATA` for inputs
- Fail-closed: guard check error → deny delete
- No `mojo_secrets` in any REST response (already enforced by `NO_SHOW_FIELDS`)