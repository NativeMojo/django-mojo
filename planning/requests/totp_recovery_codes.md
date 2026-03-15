# Request: TOTP Recovery Codes

## Priority
High — security gap. TOTP without recovery codes forces a support intervention
whenever a user loses their authenticator app.

## Problem
Users who lose access to their authenticator app (lost phone, factory reset,
new device) have no self-service recovery path. The only current option is
admin intervention to disable TOTP on their account. This is a support burden
and a known gap in any MFA implementation.

## Proposed Solution

Generate 8 single-use recovery codes when TOTP is confirmed. Store them hashed
in `mojo_secrets`. Provide endpoints to view, regenerate, and consume them.

### New Endpoints

| Method | URL | Auth | Purpose |
|---|---|---|---|
| `GET` | `/api/account/totp/recovery-codes` | Required | View current recovery codes (shown once after generation, then masked) |
| `POST` | `/api/account/totp/recovery-codes/regenerate` | Required | Invalidate old codes, generate fresh set — requires live TOTP code |
| `POST` | `/api/auth/totp/recover` | Public | Login using a recovery code instead of TOTP |

### Recovery Code Spec
- 8 codes per user
- Format: `xxxx-xxxx-xxxx` (12 hex chars, hyphen-grouped for readability)
- Stored as bcrypt/sha256 hashes in `mojo_secrets` — plaintext never persisted
- Single-use: consumed code is removed from the stored set immediately
- Generated at `POST /api/account/totp/confirm` time (same response, or
  retrievable immediately after via GET)
- Regeneration requires a valid live TOTP code (proves they still have the app)

### `GET /api/account/totp/recovery-codes` behaviour
- Returns codes **masked** after first display (`xxxx-xxxx-****`) so the
  endpoint is safe to call on a settings page without leaking full codes
- Returns count of remaining codes regardless
- Full codes only visible immediately after generation or regeneration

### `POST /api/auth/totp/recover`
```json
{
  "mfa_token": "a3f1c9d2...",
  "recovery_code": "3a7f-b2c1-9e4d"
}
```
- Requires a valid `mfa_token` (same as `auth/totp/verify`) — user must have
  passed the password step first
- Consumes the code on success, issues JWT
- Logs incident: `totp:recovery_used`
- If 0 codes remain after use, emit a warning notification to the user

## Files in Scope

| File | Change |
|---|---|
| `mojo/apps/account/rest/totp.py` | Add 3 new endpoints |
| `mojo/apps/account/models/totp.py` | Add recovery code helpers (generate, hash, verify, consume) |
| `docs/web_developer/account/mfa_totp.md` | Document new endpoints under Group 1 (management) and Group 2 (auth) |
| `docs/web_developer/account/user_self_management.md` | Add recovery code rows to section 7 and quick reference table |
| `tests/test_accounts/totp.py` | New test file or extend existing |
| `CHANGELOG.md` | Entry under new version |

## Out of Scope
- Email delivery of recovery codes (downstream project responsibility)
- Recovery codes for SMS MFA (separate feature)
- Admin-side recovery code reset (admin can already disable TOTP directly)

## Edge Cases to Handle
- `GET` recovery codes when TOTP is not set up → 400
- `POST regenerate` with invalid TOTP code → 403, do not clear existing codes
- `POST recover` with already-used code → 403
- `POST recover` with valid code but inactive user → 403
- `POST recover` with invalid `mfa_token` → 401
- Regeneration produces exactly 8 new codes and atomically replaces old set

## Tests Required
- Generate codes at TOTP confirm time
- GET returns masked codes after initial view
- Regenerate requires valid TOTP code; invalid code does not clear existing
- Each recovery code is single-use
- Recovery code login happy path (mfa_token + recovery_code → JWT)
- Invalid / already-used recovery code rejected
- User with no codes remaining gets 0 in count
- Inactive user blocked at recover endpoint
- Incident logged on use

## Security Notes
- Codes are stored hashed — a DB compromise does not leak usable codes
- Consumption is atomic — no TOCTOU race on concurrent requests
- Regeneration is gated behind live TOTP to prevent an attacker with a stolen
  session from quietly rotating codes