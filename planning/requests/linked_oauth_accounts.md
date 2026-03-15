# Request: Linked OAuth Accounts (Social Connections)

## Status
Ready to build

## Summary
Expose `OAuthConnection` to authenticated users so they can see which OAuth
providers are linked to their account and unlink them. The model and REST
endpoint likely already work — this task is to verify, add a safety guard,
and document.

## Background
`OAuthConnection` already has:
- `CAN_DELETE = True`
- `VIEW_PERMS = ["owner", "manage_users"]`
- `SAVE_PERMS = ["manage_users"]`
- `OWNER_FIELD = "user"`

`GET /api/account/oauth_connection` and `DELETE /api/account/oauth_connection/<id>`
may already function correctly for the account owner. This needs to be confirmed
and one critical safety guard added.

## Scope

### In scope
- Verify `GET /api/account/oauth_connection` lists only the requesting user's
  connections (owner filter working correctly)
- Verify `DELETE /api/account/oauth_connection/<id>` works for the owner
- Add a pre-delete safety guard: **if the user has no usable password AND this
  is their last OAuth connection, block the delete** — otherwise the user
  is permanently locked out
- Document both endpoints in `docs/web_developer/account/oauth.md`
- Add the two endpoints to `docs/web_developer/account/user_self_management.md`
  section 6 and the quick reference table

### Out of scope
- Adding new OAuth providers (separate concern)
- Linking a new provider from within the settings page (that is the existing
  OAuth begin/complete flow)
- Any UI for the connect flow

## Files in scope
- `mojo/apps/account/models/oauth.py` — add pre-delete guard (hook or custom
  endpoint)
- `mojo/apps/account/rest/oauth.py` — add unlink endpoint if standard CRUD
  is insufficient for the guard
- `docs/web_developer/account/oauth.md` — add Managing Connections section
- `docs/web_developer/account/user_self_management.md` — add connections to
  section and quick reference table
- `tests/test_accounts/oauth.py` — add tests for list, unlink, and lockout guard

## Safety guard detail
Check at delete time:
1. Does the user have a password set? (`user.has_usable_password()`)
2. How many active `OAuthConnection` records does the user have?

If password is not set AND connection count == 1 → reject with 400 and a
clear error message: `"Cannot unlink your only login method. Set a password first."`

## API shape

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
Success: `{ "status": true }`
Blocked: `400` with `"Cannot unlink your only login method. Set a password first."`

## Tests required
- Owner can list their own connections
- Owner cannot see another user's connections
- Owner can unlink a connection when they have a password set
- Owner can unlink one of two connections even without a password
- Unlink blocked when no password + last connection
- `manage_users` user can delete any connection (admin use case)

## Docs to update
- `docs/web_developer/account/oauth.md` — new "Managing Connections" section
- `docs/web_developer/account/user_self_management.md` — add to quick reference
- `CHANGELOG.md` — entry under current version

## Constraints
- No migrations (model already exists)
- No type hints
- Use `request.DATA` for inputs
- Fail-closed: if the guard check itself errors, deny the delete