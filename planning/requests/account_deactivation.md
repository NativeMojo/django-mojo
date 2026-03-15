# Request: Account Deactivation / Self-Service Deletion

## Status
pending

## Priority
high

## Summary

Expose `User.pii_anonymize()` — which already exists and is GDPR-correct — behind a
self-service REST endpoint secured with password confirmation.

---

## Background

`User.pii_anonymize()` is already implemented in `mojo/apps/account/models/user.py`.
It:
- Anonymises all PII fields (username, email, phone, display_name, etc.)
- Rotates `auth_key` (immediately invalidates all active JWTs)
- Sets `is_active = False`
- Wipes `mojo_secrets`
- Deletes passkeys and push devices
- Preserves the row for FK integrity and audit trail

No model or migration work is needed. This request is purely a REST endpoint + docs task.

---

## Endpoint

```
POST /api/account/deactivate
Authorization: Bearer <access_token>
```

Request body:
```json
{
  "current_password": "mysecretpassword"
}
```

Response on success:
```json
{
  "status": true,
  "message": "Your account has been deactivated."
}
```

---

## Behaviour

1. Require authentication (`@md.requires_auth()`).
2. Require `current_password` param (`@md.requires_params("current_password")`).
3. Verify `current_password` against `request.user` — reject 401 + log incident on wrong password.
4. Call `request.user.pii_anonymize()`.
5. Return 200. The JWT is now invalid (auth_key was rotated) — the client should
   treat the session as ended and clear stored tokens.

**Hard-delete is out of scope.** The framework preserves the row for FK integrity.
Downstream projects that need a true hard-delete or a grace period before anonymisation
should implement that at the project level (e.g. a scheduled job that calls
`pii_anonymize()` after N days, triggered by a soft-delete flag in `user.metadata`).

---

## Security requirements

- Password confirmation is mandatory — no deactivation without it.
- Wrong password: return 401, report incident (`account:deactivate_failed`), do NOT deactivate.
- Successful deactivation: report incident (`account:deactivated`) before anonymising
  (so the log entry is written while the username is still readable).
- Rate-limit the endpoint (e.g. `ip_limit=5, ip_window=300`).
- No MFA bypass consideration needed — this is intentionally a high-friction action.

---

## Files in scope

| File | Change |
|---|---|
| `mojo/apps/account/rest/user.py` | Add `on_account_deactivate` endpoint |
| `docs/web_developer/account/user_self_management.md` | Add section + quick ref row |
| `docs/web_developer/account/user.md` | Note that deactivation is self-service |
| `tests/test_accounts/deactivation.py` | New test file |
| `CHANGELOG.md` | Entry under next version |

---

## Tests required

- Happy path: valid password → 200, `is_active=False`, login no longer works
- Wrong password: 401, account NOT deactivated
- Missing password param: 400
- Already inactive user: should still return 200 (idempotent) or 400 — decide at implementation
- JWT is invalid after deactivation (validate_jwt returns error)
- Rate limit: 6th attempt from same IP within window is rejected

---

## Out of scope

- Grace period / undo window (product-level concern)
- Hard delete
- Admin-initiated deactivation (already possible via `manage_users` + `POST /api/user/<id>` with `is_active=false`)
- Email notification to the user on deactivation (downstream project concern)

---

## See also

- `mojo/apps/account/models/user.py` — `pii_anonymize()` (~L738)
- Existing pattern: `on_phone_change_confirm` in `user.py` for current_password verification pattern