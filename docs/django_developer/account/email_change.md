# Email Change — Django Developer Reference

## Overview

Self-service email change lets an authenticated user replace their email address without admin involvement. Two confirmation methods are supported:

- **Link flow** (default) — a confirmation link containing an `ec:` token is sent to the new address. The user clicks it to commit the change.
- **Code flow** — a 6-digit OTP is sent to the new address. The user submits it while still authenticated in the portal.

Both paths go through the same request endpoint. The confirm endpoint accepts either a token or a code and routes accordingly.

The feature is controlled by the `ALLOW_EMAIL_CHANGE` setting (default `True`). Set it to `False` to disable self-service changes entirely — the request endpoint will return 403.

Code lives in:
- `mojo/apps/account/rest/user.py` — REST endpoints
- `mojo/apps/account/utils/tokens.py` — token and OTP generation/verification

---

## Token Infrastructure

### Link flow

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
- **Pending email storage:** the new address is stored in `mojo_secrets` and cleared when the token is consumed or cancelled
- **Mutual exclusivity:** `generate_email_change_token` clears any outstanding code-flow OTP before issuing the new token, so link and code paths can never be active simultaneously

### Code flow

```python
from mojo.apps.account.utils.tokens import (
    generate_email_change_otp,
    verify_email_change_otp,
)

# Request step — store pending email and issue 6-digit OTP
otp = generate_email_change_otp(user, "newemail@example.com")
# otp is the 6-digit string to include in the email

# Confirm step — verify OTP, retrieve and clear pending email
new_email = verify_email_change_otp(user, submitted_code)
```

OTP details:

- **TTL:** controlled by `EMAIL_CHANGE_CODE_TTL` (default `600` — 10 minutes)
- **Single-use:** all OTP secrets are cleared on success
- **Mutual exclusivity:** `generate_email_change_otp` clears the outstanding `ec:` JTI before storing the new OTP, so link and code paths can never be active simultaneously

Both `verify_email_change_token` and `verify_email_change_otp` raise `merrors.ValueException` on any failure (expired, invalid, no pending state).

---

## REST Endpoints

| Endpoint | Auth | What it does |
|---|---|---|
| `POST /api/auth/email/change/request` | Bearer token required | Validates password + new email; sends link or OTP depending on `method`; always notifies old address |
| `POST /api/auth/email/change/confirm` | None (token path) / Bearer token required (code path) | Commits the new email, rotates `auth_key`, returns fresh JWT |
| `GET /api/auth/email/change/confirm` | None | Browser link-click handler; renders `account/email_change_confirm.html` |
| `POST /api/auth/email/change/cancel` | Bearer token required | Invalidates any outstanding token **and** OTP immediately |

### Request — `method` parameter

The `method` field in the request body controls which confirmation path is used:

| `method` value | Behaviour |
|---|---|
| `"link"` (default, or omitted) | Generates an `ec:` token, sends `email_change_confirm` template to new address |
| `"code"` | Generates a 6-digit OTP, sends `email_change_code` template to new address |

In both cases the `email_change_notify` template is sent to the old address.

### Confirm — routing logic

The confirm endpoint inspects the request body and routes accordingly:

```python
token = request.DATA.get("token")
code  = request.DATA.get("code")

if code:
    # Code path — requires active authenticated session
    user     = request.user          # identity from Bearer token
    new_email = verify_email_change_otp(user, code)
else:
    # Link/token path — token is the credential; no session required
    user, new_email = verify_email_change_token(token)
```

---

## Email Templates Required

Three email templates must be defined in the project's email template system.

### `email_change_confirm` (link flow)

Sent to the **new** address when `method: "link"`. Context variables:

| Variable | Description |
|---|---|
| `token` | The `ec:` token string to embed in the confirmation link |
| `new_email` | The new email address being confirmed |
| `user` | Basic user dict |

The template should contain a link such as:

```
https://yourapp.com/email-change?token={{ token }}
```

The frontend extracts the token from the URL and calls `POST /api/auth/email/change/confirm` with `{ "token": "ec:..." }`. Alternatively, point the link directly at `GET /api/auth/email/change/confirm?token={{ token }}&redirect=https://yourapp.com/login` to have the API render the result page.

### `email_change_code` (code flow)

Sent to the **new** address when `method: "code"`. Context variables:

| Variable | Description |
|---|---|
| `code` | The 6-digit OTP string to display |
| `new_email` | The new email address being confirmed |
| `user` | Basic user dict |

The template should display the code prominently with a note about expiry:

```
Your email change code is: {{ code }}
This code expires in 10 minutes and can only be used once.
```

### `email_change_notify`

Sent to the **old** address for both flows. Context variables:

| Variable | Description |
|---|---|
| `new_email` | The new address that was requested |

The template should tell the account owner what happened and direct them to `POST /api/auth/email/change/cancel` if they did not request this change, and to reset their password as a precaution.

---

## What Happens on Confirm

When either `verify_email_change_token` or `verify_email_change_otp` succeeds, the endpoint performs these steps:

```python
User.objects.filter(pk=user.pk).update(
    email=new_email,
    is_email_verified=True,
    auth_key=uuid.uuid4().hex,   # rotates all outstanding JWTs
)
# Only when username previously matched the old email:
if str(user.username).lower() == old_email.lower():
    User.objects.filter(pk=user.pk).update(username=new_email)

user.refresh_from_db()
user.log(kind="email:changed", log=f"{old_email} to {new_email}")
_send_account_realtime_event(user, "account:email:changed", {"email": new_email})
```

Key points:

- **`User.objects.filter(...).update(...)`** is used deliberately to bypass the REST field guard that normally makes `email` and `is_email_verified` read-only via the API.
- **`auth_key` rotation** immediately invalidates every JWT signed with the old key — including any attacker session that may have triggered the flow. This applies to both the link path and the code path.
- **`username` sync** only occurs when `user.username` was equal to the old `user.email` at the time of confirm. Accounts that use a separate username are not affected.
- **`user.log`** writes an audit record queryable via the standard incident/audit log.
- **Realtime event** `account:email:changed` is emitted to all of the user's active WebSocket connections after every successful confirm path.

After the update, the confirm endpoint issues a new JWT signed against the rotated `auth_key` and returns it as a standard login response. The client must replace its stored tokens with the new ones.

---

## Cancel — What Gets Cleared

`POST /api/auth/email/change/cancel` clears all pending state in one operation:

```python
user.set_secret("pending_email", None)
user.set_secret(_JTI_KEYS[KIND_EMAIL_CHANGE], None)   # kills any ec: token
user.set_secret("email_change_otp", None)             # kills any OTP code
user.set_secret("email_change_otp_ts", None)
user.save(update_fields=["mojo_secrets", "modified"])
```

This means a single cancel call covers both paths regardless of which method was used to initiate the change.

---

## `ALLOW_EMAIL_CHANGE` Setting

Add to `settings.py` to disable the feature:

```python
ALLOW_EMAIL_CHANGE = False
```

When `False`, `POST /api/auth/email/change/request` returns a 403 immediately. The confirm and cancel endpoints are unaffected — any token or code already in flight can still be confirmed or cancelled.

---

## Settings Reference

| Setting | Default | Description |
|---|---|---|
| `ALLOW_EMAIL_CHANGE` | `True` | Set to `False` to disable self-service email change entirely |
| `EMAIL_CHANGE_TOKEN_TTL` | `3600` (1 h) | Expiry time for link-flow `ec:` tokens, in seconds |
| `EMAIL_CHANGE_CODE_TTL` | `600` (10 min) | Expiry time for code-flow OTP codes, in seconds |

---

## Security Design Notes

**Why `current_password` is required**

A Bearer token alone does not prove the legitimate user is present — it could be a stolen or leaked JWT. Requiring the current password ensures only someone who knows the credential can initiate a change.

**Why confirm rotates `auth_key`**

If an attacker obtained a valid session and somehow bypassed the password check, rotating `auth_key` on confirm evicts every active session — including the attacker's — the moment the real owner confirms. The real owner gets a fresh JWT; everyone else is logged out. This applies equally to the link and code paths.

**Why the old address receives a notification**

Email change is a high-value account takeover vector. Sending a notification to the old address gives the real owner an immediate signal and a cancellation path even before the TTL expires.

**Why only one pending change can be active at a time**

`generate_email_change_token` clears any OTP, and `generate_email_change_otp` clears the `ec:` JTI, before generating new credentials. This prevents an attacker from initiating a second change while a first is in flight and avoids any ambiguity in the confirm step about which pending email is authoritative.

**Why the code path requires authentication**

The link path uses the `ec:` token itself as the credential — it contains a signed user reference and is sufficient on its own. The code path is designed for portal use where the user already has an active session; the Bearer token provides identity, and the OTP proves ownership of the new address. Requiring both ensures neither alone is sufficient.

**Why confirm re-checks email availability**

Between request and confirm there is a window (up to 1 hour for links, 10 minutes for codes) during which another account may have registered or been assigned the same address. The confirm step re-validates uniqueness on all paths and returns an error if the address is no longer available.