# Email Change — Django Developer Reference

## Overview

Self-service email change lets an authenticated user replace their email address without admin involvement. The flow has three steps:

1. **Request** — the user submits their new address and current password. A confirmation token is issued and emailed to the new address. A notification email is sent to the old address. Nothing changes yet.
2. **Confirm** — the user clicks the link in the new inbox. The token is validated, the new email is committed, and all existing sessions are invalidated.
3. **Done** — the confirm response returns a fresh JWT for the current session. All other sessions are logged out.

The feature is controlled by the `ALLOW_EMAIL_CHANGE` setting (default `True`). Set it to `False` to disable self-service changes entirely — the request endpoint will return 403.

Code lives in:
- `mojo/apps/account/rest/auth.py` — REST endpoints
- `mojo/apps/account/utils/tokens.py` — token generation and verification
- `mojo/apps/account/services/email_change.py` — commit logic

---

## Token Infrastructure

```python
from mojo.apps.account.utils.tokens import (
    generate_email_change_token,
    verify_email_change_token,
)

# Request step — store pending email and issue ec: token
token = generate_email_change_token(user, "newemail@example.com")

# Confirm step — verify token, retrieve pending email (clears it)
user, new_email = verify_email_change_token(token)
```

Token details:

- **Kind prefix:** `ec:`
- **TTL:** controlled by `EMAIL_CHANGE_TOKEN_TTL` (default `3600` — 1 hour)
- **Single-use:** the JTI is consumed on `verify_email_change_token`; replaying the token returns an error
- **Pending email storage:** the new address is stored encrypted in `mojo_secrets` and cleared when the token is consumed or cancelled

`verify_email_change_token` raises `InvalidTokenException` if the token is expired, already used, or structurally invalid. It raises `EmailUnavailableException` if another account claimed the address in the window between request and confirm.

---

## REST Endpoints

| Endpoint | Auth | What it does |
|---|---|---|
| `POST /api/auth/email/change/request` | Bearer token required | Validates password + new email, stores pending change, sends both emails |
| `POST /api/auth/email/change/confirm` | None — token is the credential | Commits the new email, rotates `auth_key`, returns fresh JWT |
| `POST /api/auth/email/change/cancel` | Bearer token required | Invalidates the outstanding `ec:` token immediately |

---

## Email Templates Required

Two email templates must be defined in the project's email template system.

### `email_change_confirm`

Sent to the **new** address. Context variables:

| Variable | Description |
|---|---|
| `token` | The `ec:` token string to embed in the confirmation link |
| `new_email` | The new email address being confirmed |

The template should contain a link such as:

```
https://yourapp.com/email-change?token={{ token }}
```

The frontend extracts the token from the URL and calls `POST /api/auth/email/change/confirm`.

### `email_change_notify`

Sent to the **old** address. Context variables:

| Variable | Description |
|---|---|
| `new_email` | The new address that was requested |

The template should tell the account owner what happened and direct them to cancel via `POST /api/auth/email/change/cancel` if they did not request this change, and to reset their password as a precaution.

---

## What Happens on Confirm

When `verify_email_change_token` succeeds, the service layer performs these steps atomically:

```python
User.objects.filter(pk=user.pk).update(
    email=new_email,
    is_email_verified=True,
    auth_key=generate_auth_key(),   # rotates all outstanding JWTs
    username=new_email,             # only if username previously matched old email
)
user.log(kind="email:changed", meta={"new_email": new_email})
```

Key points:

- **`User.objects.filter(...).update(...)`** is used deliberately to bypass the REST field guard that normally makes `email` and `is_email_verified` read-only via the API.
- **`auth_key` rotation** immediately invalidates every JWT that was signed with the old key — including any attacker session that may have triggered the flow.
- **`username` sync** only occurs when `user.username` was equal to the old `user.email` at the time of confirm. Accounts that use a separate username are not affected.
- **`user.log`** writes an audit record. The `email:changed` kind is queryable via the standard incident/audit log.

After the update, the confirm endpoint issues a new JWT signed against the rotated `auth_key` and returns it as a standard login response. The client must replace its stored tokens with the new ones.

---

## `ALLOW_EMAIL_CHANGE` Setting

Add to `settings.py` to disable the feature:

```python
ALLOW_EMAIL_CHANGE = False
```

When `False`, `POST /api/auth/email/change/request` returns a 403 immediately. The confirm and cancel endpoints are unaffected — any token already in flight can still be confirmed or cancelled.

---

## Security Design Notes

**Why `current_password` is required**

A Bearer token alone does not prove the legitimate user is present — it could be a stolen or leaked JWT. Requiring the current password ensures only someone who knows the credential can initiate a change.

**Why confirm rotates `auth_key`**

If an attacker obtained a valid session and somehow bypassed the password check, rotating `auth_key` on confirm evicts every active session — including the attacker's — the moment the real owner clicks the link. The real owner gets a fresh JWT; everyone else is logged out.

**Why the old address receives a notification**

Email change is a high-value account takeover vector. Sending a notification to the old address gives the real owner an immediate signal and a cancellation path even before the 1-hour token expires.

**Why confirm re-checks email availability**

Between request and confirm there is up to a 1-hour window. Another account may have registered or been assigned the same address in that time. The confirm step re-validates uniqueness and returns an error if the address is no longer available, preventing a silent collision.