# Request: Account Deactivation / Self-Service Deletion

## Status
Ready to build

## Priority
High

---

## Summary

Expose `User.pii_anonymize()` — which already exists and is GDPR-correct — behind a
self-service REST endpoint secured with an **email confirmation link** (not a password).

This approach was chosen over password confirmation because:
- OAuth-only users have no password set and must not be excluded from self-service deactivation
- Email confirmation is equivalent proof of account ownership for this purpose
- It gives the user a moment to reconsider (they must check their email and click)

---

## Background

`User.pii_anonymize()` is already implemented in `mojo/apps/account/models/user.py` (~L738).
It:
- Anonymises all PII fields (username, email, phone, display_name, etc.)
- Rotates `auth_key` (immediately invalidates all active JWTs)
- Sets `is_active = False`
- Wipes `mojo_secrets`
- Deletes passkeys and push devices
- Preserves the row for FK integrity and audit trail

No model or migration work is needed. This request is a REST endpoint + token
infrastructure + docs task.

---

## Flow

```
Step 1: POST /api/account/deactivate
  → sends a confirmation email with a short-lived signed token
  → returns 200 immediately (no enumeration — always succeeds if authenticated)

Step 2: POST /api/account/deactivate/confirm   { "token": "dv:..." }
  → validates token
  → calls pii_anonymize()
  → returns 200
  → JWT is now invalid (auth_key was rotated) — client clears stored tokens
```

---

## Endpoints

### Step 1 — Request deactivation

```
POST /api/account/deactivate
Authorization: Bearer <access_token>
```

No request body required.

**Response:**
```json
{
  "status": true,
  "message": "A confirmation email has been sent. Follow the link to complete deactivation."
}
```

Behaviour:
- Requires authentication (`@md.requires_auth()`)
- Generates a single-use `dv:` token (new kind in token infrastructure, TTL 15 minutes)
- Sends `account_deactivate_confirm` email template with the token
- Logs incident: `account:deactivate_requested`
- Rate-limited: `ip_limit=5, ip_window=300`

---

### Step 2 — Confirm deactivation

```
POST /api/account/deactivate/confirm
```

```json
{
  "token": "dv:4e6f..."
}
```

Public endpoint (no Bearer token required — the token is the credential, same
pattern as password reset and email change confirm).

**Response on success:**
```json
{
  "status": true,
  "message": "Your account has been deactivated."
}
```

Behaviour:
1. Validate the `dv:` token — resolve the user, reject if expired or already used
2. Check `user.is_active` — if already `False`, return 200 (idempotent, no double-anonymise)
3. Log incident `account:deactivated` **before** calling `pii_anonymize()` so the
   entry is written while the username is still readable
4. Call `user.pii_anonymize()`
5. Return 200

---

## Token Infrastructure

Add `KIND_DEACTIVATE = "dv"` to `mojo/apps/account/utils/tokens.py`.

Follow the exact same pattern as `KIND_MAGIC_LOGIN`:
- `generate_deactivate_token(user)` → token string `dv:<signed>`
- `verify_deactivate_token(token)` → user (or raises on invalid/expired)
- TTL: `DEACTIVATE_TOKEN_TTL` setting, default `900` (15 minutes)
- Single-use: JTI rotation on consume

---

## Email Template

Template name: `account_deactivate_confirm`

Context variables:
- `token` — the raw `dv:` token string
- `user` — the user object

Template must be created by the downstream project. Framework does not ship
a default. Document the required variables.

---

## OAuth-Only Accounts (No Password Set)

OAuth-only users have no usable password. The email link flow handles them
naturally — they have a verified email address (set at OAuth login time) and
that is sufficient proof of ownership.

**Related fix (handled in `linked_oauth_accounts` request):** When a new user is
created via OAuth in `_find_or_create_user`, call `user.set_unusable_password()`
explicitly so Django's `has_usable_password()` returns `False` cleanly, rather
than relying on the empty-string default from `AbstractBaseUser`.

---

## Security Requirements

- Token is single-use and expires in 15 minutes
- Token validates against the user's current `auth_key` — so if the user changes
  their password or rotates sessions between request and confirm, the token is dead
- Successful deactivation incident must be written before `pii_anonymize()` is called
- Rate-limit the request endpoint (5 per IP per 5 minutes)
- The confirm endpoint is public but the token itself is the authentication —
  no additional auth header required (same pattern as `auth/email/change/confirm`)

---

## Settings

| Setting | Default | Purpose |
|---|---|---|
| `DEACTIVATE_TOKEN_TTL` | `900` | Seconds until the confirmation token expires (15 min) |
| `ALLOW_SELF_DEACTIVATION` | `True` | Feature flag — set `False` to disable entirely |

---

## Files in Scope

| File | Change |
|---|---|
| `mojo/apps/account/utils/tokens.py` | Add `KIND_DEACTIVATE`, `generate_deactivate_token()`, `verify_deactivate_token()` |
| `mojo/apps/account/rest/user.py` | Add `on_account_deactivate` and `on_account_deactivate_confirm` endpoints |
| `docs/web_developer/account/user_self_management.md` | Add section + quick reference rows |
| `docs/web_developer/account/user.md` | Note that self-service deactivation is available |
| `tests/test_accounts/deactivation.py` | New test file |
| `CHANGELOG.md` | Entry under next version |

---

## Tests Required

- Happy path: request sends email, confirm with valid token → `is_active=False`
- Already inactive: confirm returns 200, `pii_anonymize()` not called twice
- Token expired: confirm returns 400
- Token wrong kind (e.g. `ml:` or `pr:` token): confirm returns 400
- Token already used: confirm returns 400
- `ALLOW_SELF_DEACTIVATION = False`: request returns 403
- Unauthenticated request to `/deactivate`: returns 403
- JWT is invalid after deactivation (`validate_jwt` returns error)
- Incident `account:deactivated` written before anonymisation
- Rate limit on request endpoint

---

## Out of Scope

- Grace period / undo window (product-level concern)
- Hard delete
- Admin-initiated deactivation (already possible via `manage_users` +
  `POST /api/user/<id>` with `is_active=false`)
- Email notification to the user on deactivation beyond the confirmation email
  (downstream project concern)

---

## See Also

- `mojo/apps/account/models/user.py` — `pii_anonymize()` ~L738
- `mojo/apps/account/utils/tokens.py` — existing token kinds as pattern reference
- `planning/requests/linked_oauth_accounts.md` — `set_unusable_password()` fix