# Request: Session Revoke / Log Out Everywhere

## Status
Ready to build

## Priority
High

## Summary
Add a self-service endpoint that lets a logged-in user invalidate all their active
sessions across every device simultaneously. Rotating `auth_key` on the User model
is the correct mechanism — all outstanding JWTs signed with the old key become
invalid immediately.

---

## Decisions & Approach

- Requires `current_password` — prevents an attacker with a stolen JWT from
  locking the real user out of all their sessions
- Rotates `auth_key` immediately — every other session is killed
- Returns a **fresh JWT** for the calling session so the user stays logged in
- "Log out everywhere **except this session**" — document this clearly
- Do not claim per-device revocation is supported (architecture does not allow it)
- Log incident `sessions:revoked` on success, `sessions:revoke_failed` on wrong password

---

## Background

`GET /api/user/device` already lists devices. `UserDevice` records have `DELETE`
wired via the standard CRUD handler, but deleting a device record does not
invalidate the JWT — the token remains valid until it expires naturally.

True per-device JWT revocation is not possible with the current single-`auth_key`-per-user
signing architecture without a significant redesign. The honest, safe, and immediately
useful implementation is "log out everywhere": one endpoint that rotates `auth_key`,
invalidating every active session. This is already what email-change confirm does as a
side effect and is well understood by users.

---

## Endpoint

```
POST /api/auth/sessions/revoke
Authorization: Bearer <access_token>

{ "current_password": "mysecretpassword" }
```

Response on success — fresh JWT for the current session:

```json
{
  "status": true,
  "data": {
    "access_token": "eyJhbGci...",
    "refresh_token": "eyJhbGci...",
    "expires_in": 21600
  }
}
```

---

## Implementation Steps

1. `@md.requires_auth()` + `@md.requires_params("current_password")`
2. Apply strict rate limit (e.g. `ip_limit=5, ip_window=300`)
3. Verify `current_password` against `request.user.check_password(...)` — return 401
   on failure, log incident `sessions:revoke_failed`, do NOT modify any state
4. Rotate: `user.auth_key = uuid.uuid4().hex; user.save(update_fields=["auth_key", "modified"])`
5. Log incident `sessions:revoked`
6. Issue fresh JWT via `jwt_login(request, user)` — must happen AFTER rotation so the
   new token is signed with the new key
7. Return the JWT response directly

---

## Key Files

| File | Change |
|---|---|
| `mojo/apps/account/rest/user.py` | Add `on_sessions_revoke` endpoint |
| `docs/web_developer/account/user_self_management.md` | Section 8 — Sessions & Devices |
| `docs/web_developer/account/authentication.md` | Security Notes section |
| `tests/test_accounts/session_revoke.py` | New test file |
| `CHANGELOG.md` | Entry under next version |

---

## Tests Required

- Happy path: correct password → fresh JWT returned, old JWT no longer valid
- Wrong password: 401, `auth_key` unchanged, old JWT still valid
- Missing `current_password` param: 400
- Unauthenticated request: 403
- Fresh JWT from response is valid (can call `/api/user/me` with it)
- Old access token is rejected after revocation
- Incident logged on success and on failed attempt

---

## Documentation Notes

- Make explicit in the doc: "this logs out all other sessions; the current session
  receives a new token and stays active"
- Note that email change confirm also rotates `auth_key` as a side effect
- Do NOT say per-device revocation is supported
- Cross-reference from the `user_self_management.md` Sessions section

---

## Out of Scope

- Per-device JWT revocation (requires per-device signing keys — architecture change;
  see `planning/rejected/trusted_devices.md` for context)
- Deleting `UserDevice` tracking records as part of this flow (orthogonal)
- Admin-initiated revocation of another user's sessions (separate `manage_users` endpoint)

---

## Constraints

- `request.DATA` for all inputs
- No migrations
- No Python type hints
- Fail-closed: wrong password must log incident and return 401 before any state changes
- `auth_key` must never appear in any response field