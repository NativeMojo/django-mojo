# Request: TOTP Recovery Codes

## Status
Ready to build

## Priority
High — security gap. TOTP without recovery codes forces a support intervention
whenever a user loses their authenticator app.

## Decisions Made

| Question | Decision |
|---|---|
| Where are codes first shown? | In the `POST /api/account/totp/confirm` response body — simplest for UI, no extra GET needed |
| Hash algorithm | bcrypt — stronger security guarantee than sha256 |
| Code format | `xxxx-xxxx-xxxx` (12 hex chars, hyphen-grouped) |
| GET behaviour | Returns masked codes (`xxxx-xxxx-****`) after first display; full plaintext only in confirm/regenerate response |
| Regeneration gate | Requires a currently-valid live TOTP code |
| Login path | Requires `mfa_token` (user must have passed password step first) + `recovery_code` |

---

## Problem

Users who lose access to their authenticator app (lost phone, factory reset,
new device) have no self-service recovery path. The only current option is
admin intervention to disable TOTP on their account. This is a support burden
and a known gap in any MFA implementation.

---

## Solution

Generate 8 single-use recovery codes when TOTP is confirmed. Return them in
plaintext in the `POST /api/account/totp/confirm` response. Store them as
bcrypt hashes in `mojo_secrets` on the `UserTOTP` record. Provide endpoints to
view (masked), regenerate, and consume them.

---

## Endpoints

| Method | URL | Auth | Purpose |
|---|---|---|---|
| `GET` | `/api/account/totp/recovery-codes` | Required | View current codes (masked after generation) + remaining count |
| `POST` | `/api/account/totp/recovery-codes/regenerate` | Required | Invalidate old codes, generate fresh set — requires live TOTP code |
| `POST` | `/api/auth/totp/recover` | Public | Login using a recovery code instead of TOTP code |

---

## Recovery Code Spec

- **Count:** 8 codes per user
- **Format:** `xxxx-xxxx-xxxx` (12 lowercase hex chars, split by hyphens)
- **Generation:** `secrets.token_hex(6)` split into 3 groups of 4
- **Storage:** Each code stored as a bcrypt hash in a JSON list in `mojo_secrets`
  under key `"recovery_codes"` on the `UserTOTP` record. Plaintext is never
  persisted after generation.
- **Single-use:** When a code is consumed it is immediately removed from the
  stored list atomically.
- **Regeneration:** Requires a valid live TOTP code to prevent an attacker with
  a stolen session from quietly rotating all recovery codes.

---

## `POST /api/account/totp/confirm` — updated response

After this change, the confirm endpoint returns the recovery codes in plaintext
in its response body. The UI must display these to the user immediately and
prompt them to store them safely. They will not be shown in full again.

```json
{
  "status": true,
  "data": {
    "is_enabled": true,
    "recovery_codes": [
      "3a7f-b2c1-9e4d",
      "a1b2-c3d4-e5f6",
      "...6 more..."
    ]
  }
}
```

---

## `GET /api/account/totp/recovery-codes`

Returns masked codes and remaining count. Safe to call on a settings page
without leaking usable codes.

```json
{
  "status": true,
  "data": {
    "remaining": 7,
    "codes": [
      "3a7f-xxxx-xxxx",
      "a1b2-xxxx-xxxx",
      "...5 more masked..."
    ]
  }
}
```

Masking strategy: reveal the first 4 chars of each code, mask the rest with
`xxxx-xxxx`. This lets the user confirm which codes they have without exposing
them fully.

---

## `POST /api/account/totp/recovery-codes/regenerate`

Requires authentication + a valid live TOTP code. Atomically replaces all
existing codes with a fresh set of 8. Returns plaintext codes in response
(same shape as confirm).

```json
POST /api/account/totp/recovery-codes/regenerate
Authorization: Bearer <access_token>

{ "code": "482910" }
```

Response: same shape as `totp/confirm` — `{ is_enabled, recovery_codes: [...] }`.

If the TOTP code is **invalid**: return 403, do **not** clear existing codes.

---

## `POST /api/auth/totp/recover`

Public endpoint (no Bearer token). Requires the `mfa_token` issued after
password login — this proves the user passed the password check. Then accepts
a recovery code in place of a TOTP code.

```json
{
  "mfa_token": "a3f1c9d2...",
  "recovery_code": "3a7f-b2c1-9e4d"
}
```

On success:
- Consumes the recovery code (removes it from the stored list atomically)
- Issues a full JWT (same response as `auth/totp/verify`)
- Logs incident `totp:recovery_used`
- If `remaining == 0` after consumption, creates a `Notification` warning
  the user they have no recovery codes left

On failure:
- Invalid `mfa_token` → 401
- Invalid/already-used recovery code → 403
- Inactive user → 403

---

## Files in Scope

| File | Change |
|---|---|
| `mojo/apps/account/rest/totp.py` | Add 3 new endpoints; update `on_totp_confirm` to generate + return codes |
| `mojo/apps/account/models/totp.py` | Add `generate_recovery_codes()`, `get_masked_recovery_codes()`, `verify_and_consume_recovery_code()` helpers |
| `docs/web_developer/account/mfa_totp.md` | Document new endpoints under Group 1 (management) and Group 2 (auth) |
| `docs/web_developer/account/user_self_management.md` | Add recovery code rows to section 7 and quick reference table |
| `tests/test_accounts/totp_recovery.py` | New test file |
| `CHANGELOG.md` | Entry under next version |

---

## Implementation Notes

- Use `bcrypt.hashpw(code.encode(), bcrypt.gensalt())` for hashing. Verify
  with `bcrypt.checkpw()`. bcrypt is already available via Django's auth stack.
- Store the list as JSON: `[{"hash": "...", "hint": "3a7f"}]` — the hint
  (first 4 chars) enables the masked GET without re-hashing.
- Atomic consumption: use `select_for_update()` or a compare-and-swap pattern
  so two concurrent requests cannot both consume the same code.
- `UserTOTP` already uses `MojoSecrets` — store under key `"recovery_codes"`.

---

## Edge Cases

| Scenario | Expected |
|---|---|
| GET when TOTP not set up | 400 |
| GET when codes were never generated | `remaining: 0, codes: []` |
| Regenerate with invalid TOTP code | 403, existing codes unchanged |
| Recover with already-used code | 403 |
| Recover with valid code, inactive user | 403 |
| Recover with invalid `mfa_token` | 401 |
| Last code consumed | 200, warning notification sent |
| Regenerate when 0 codes remain | Allowed — generates fresh 8 |

---

## Tests Required

- Codes returned in plaintext in `totp/confirm` response
- Codes stored as bcrypt hashes (plaintext not in DB)
- `GET` returns masked codes, correct remaining count
- Each recovery code is single-use — second use of same code rejected
- Concurrent consumption of same code: only one succeeds
- `regenerate` happy path: 8 new codes returned, old codes invalidated
- `regenerate` with invalid TOTP code: 403, old codes still valid
- Recovery login happy path: `mfa_token` + recovery code → JWT
- Recovery login: invalid/used recovery code rejected
- Recovery login: invalid `mfa_token` rejected
- Inactive user blocked at recover endpoint
- Incident logged on recovery code use
- Warning notification created when last code consumed

---

## Out of Scope

- Email delivery of recovery codes (downstream project responsibility)
- Recovery codes for SMS MFA (separate feature)
- Admin-side recovery code reset (admin can already disable TOTP directly)