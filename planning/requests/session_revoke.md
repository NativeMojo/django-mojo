# Request: Session Revoke / Log Out Everywhere

## Status
pending

## Priority
High

## Summary
Add a self-service endpoint that lets a logged-in user invalidate all their active
sessions across every device simultaneously. Rotating `auth_key` on the User model
is the correct mechanism — all outstanding JWTs signed with the old key become
invalid immediately.

## Background
`GET /api/user/device` already lists devices. `UserDevice` records have `DELETE`
wired via the standard CRUD handler, but deleting a device record does not
invalidate the JWT — the token remains valid until it expires naturally. True
per-device JWT revocation is not possible with the current single-`auth_key`-per-user
signing architecture without a significant redesign.

The honest, safe, and immediately useful implementation is "log out everywhere":
one endpoint that rotates `auth_key`, invalidating every active session. This is
already what email-change confirm does as a side effect and is well-understood by
users ("sign out of all devices").

## Scope

### In scope
- `POST /api/auth/sessions/revoke` — requires auth + `current_password`; rotates
  `auth_key`; returns a fresh JWT for the current session so the user stays logged in
- Audit/incident log entry on revoke
- Tests covering: happy path (fresh JWT returned), wrong password (401), unauthenticated (403)
- Docs: web developer (new endpoint) + update `user_self_management.md` section 8

### Out of scope
- Per-device JWT revocation (requires per-device signing keys — architecture change)
- Deleting `UserDevice` tracking records as part of this flow (orthogonal concern)
- Admin-initiated revocation of another user's sessions (separate manage_users endpoint)

## Key files
- `mojo/apps/account/rest/user.py` — add endpoint here
- `mojo/apps/account/models/user.py` — `get_auth_key()` / `auth_key` field, see L370-377
- `docs/web_developer/account/user_self_management.md` — section 8, L605
- `docs/web_developer/account/authentication.md` — mention revoke under Security Notes
- `tests/test_accounts/` — add `test_session_revoke.py` or extend existing auth tests

## Endpoint design

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

### Implementation steps
1. Verify `current_password` against `request.user` — return 401 on failure,
   log incident as `sessions:revoke_failed`
2. Rotate `auth_key`: `user.auth_key = uuid.uuid4().hex; user.save()`
3. Issue a fresh JWT via `jwt_login(request, user)` so the calling session
   survives the rotation
4. Log incident as `sessions:revoked`

### Security notes
- `current_password` is required to prevent an attacker with a stolen JWT from
  locking the real user out of all their sessions
- The fresh JWT must be issued **after** the rotation so it is signed with the new key
- Do not expose `auth_key` in any response field

## Documentation notes
- Make clear in the doc that this is "log out everywhere except this session"
- Note that email change confirm also rotates `auth_key` as a side effect
- Do not claim per-device revocation is supported

## Constraints
- Use `request.DATA` for all input
- No migrations
- No Python type hints
- Fail-closed: wrong password must return 401 and log an incident before any
  state is changed