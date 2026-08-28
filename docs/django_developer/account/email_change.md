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
| `POST /api/auth/email/change/request` | Bearer token required | Validates password + new email; sends link or OTP depending on `method`; notifies the old address **after** the provider accepted the confirmation |
| `POST /api/auth/email/change/confirm` | None (token path) / Bearer token required (code path) | Commits the new email, rotates `auth_key`, **returns a fresh JWT**. The SPA's endpoint — both the `ec:` link and the 6-digit code are accepted here |
| `POST /api/auth/email/change/apply` | None — the `ec:` token is the credential | Commits exactly the same change and **issues no session**: no token pair, no `last_login`, no login event. Token-only. This is what the landing page's button calls |
| `GET /api/auth/email/change/confirm` | None | Confirmation **landing page** the emailed link opens; renders `account/email_change_landing.html` and changes **nothing** — see below |
| `POST /api/auth/email/change/cancel` | Bearer token required | Invalidates any outstanding token **and** OTP immediately |

### Request — `method` parameter

The `method` field in the request body controls which confirmation path is used:

| `method` value | Behaviour |
|---|---|
| `"link"` (default, or omitted) | Generates an `ec:` token, sends `email_change_confirm` template to new address |
| `"code"` | Generates a 6-digit OTP, sends `email_change_code` template to new address |

In both cases the `email_change_notify` template is sent to the old address —
but only after the confirmation message was accepted, and only when the account
has an old address to tell (see below).

---

## Send Truthfulness — What a 200 Means

`POST /api/auth/email/change/request` used to answer 200 unconditionally: both
send helpers discarded the transport result, so a message SES refused produced
the same cheerful "check your inbox" as one it accepted, plus an
`email_change:requested` line in the account's history that never happened.

Both helpers now **return** the transport result, and the endpoint judges it
with the shared acceptance rule:

```python
from mojo.apps.account.services import email_delivery

sent = _send_email_change_confirm(request, user, new_email, token, send=send)
if not email_delivery.was_accepted(sent):
    _record_email_change_send_failure(request, user, sent)
    return email_delivery.send_unavailable_response()
```

| Helper | Signature | Returns |
|---|---|---|
| `_send_email_change_confirm` | `(request, user, new_email, token, *, send=None)` | the `SentMessage`, or `None` |
| `_send_email_change_code` | `(user, new_email, otp, *, send=None)` | the `SentMessage`, or `None` |
| `_notify_old_email_change_address` | `(request, user, new_email, *, send=None)` | nothing — best effort |

`email_delivery.was_accepted(sent)` (owned by
`mojo/apps/account/services/email_delivery.py`) is True only for a non-`None`
result whose `status` is `sending`/`delivered` **and** which carries a nonempty
`ses_message_id`. A truthy `SentMessage` is not proof of a send: a refused
message is persisted with `status="failed"`, no id, and the provider's error
text in `status_reason`.

`email_delivery.send_unavailable_response()` is the one failure answer — HTTP
503 with the fixed body `{"status": false, "code": 503, "error": ...}`. It is
**returned, not raised**: raising would make `dispatch_error_handler` file a
second, raw `mojo_rest_error` event carrying the request body, a stack trace
and an unverified `request.group` stamp — exactly the duplicate raw/safe pair
this flow exists to remove. `MOJO_APP_STATUS_200_ON_ERROR` is honored inside
that helper.

**No provider text ever reaches the client or the activity row.** The refusal
reason survives on the `SentMessage` row (admin-gated) and in `email.log`. The
raw `email:send_failed` incident the two send helpers used to file is gone;
they log through `logit.error("email_change", ...)` and return `None` instead.
`email:no_mailbox` is **kept** — it is an operator signal about deployment
configuration, carries no provider text, and never surfaces in the
self-service feed.

**Nothing is cleaned up on failure.** `pending_email` and the `ec:` JTI / OTP
stay exactly as generated, so a retry resumes the same change instead of
racing a confirmation the provider may still be processing.

**The 5/hour IP budget still counts failures.**
`@md.strict_rate_limit("email_change_request", ip_limit=5, ip_window=3600)`
runs before the view, so an honest 503 invites retries that can exhaust it.
That is accepted: the failure copy asks for "a few minutes", and exempting
failures from the counter would open a free probe channel on an endpoint that
takes a password.

**The `send=` / `notify_send=` seams are test-only.** The dispatcher never
passes them; they exist because a test project with no mailbox can never reach
the accepted path over HTTP.

---

## Account Activity Rows

The email-change flow writes its own audit rows through one private helper in
`mojo/apps/account/rest/user.py`:

```python
_record_account_activity(category, uid, title, details, *, provenance,
                         group=None, source_ip=None, level=1, **metadata)
```

| Aspect | Behaviour |
|---|---|
| `provenance="account"` | genuinely account-wide. `group` is forced to `None`; `metadata.security_activity_scope = "account"` |
| `provenance="brand"` | originated inside one brand context. `metadata.security_activity_scope = "brand"`; `metadata.origin_group_id` carries the group id when attribution succeeded, and is absent when it did not |
| `Event.scope` | always `"account"` — `provenance` selects the metadata marker only, and never touches the indexed column the RuleSet engine looks up |
| Request | always `request=None`, so the caller's explicit `uid` / `source_ip` / `group` are never overwritten by an ambient request identity |
| Failure | swallowed and logged — an audit write must never fail an action that already committed |

It calls `incident.record_event`, not `report_event`: no `RuleSet` traversal on
the request path. Two consequences are deliberate and declared — these
categories no longer increment `record_event_metrics()` counters, and no
deployment rule keyed on the `"account"` scope (or a `"*"` catch-all) sees
them. No shipped ruleset matches `email_change:*`, so the loss is
deployment-specific.

`Event.group` uses `SET_NULL`, so a null FK alone is **never** proof of account
origin. The explicit marker is what a reader may trust.

**`_attributable_group(request)`** decides whether a row may be attributed to a
brand at all. The dispatcher sets `request.group` from a caller-supplied
`?group=` / `?group_uuid=` with no membership check, so attribution requires an
**active direct membership** (`get_member_for_user(user, check_parents=False,
is_active=True)`, which also refuses a group under a deactivated ancestor).
Anything unexpected returns `None` — the row loses its brand placement and
nothing else.

### Categories this flow writes

| Category | Provenance | When |
|---|---|---|
| `email_change:requested` | brand | link confirmation accepted by the provider |
| `email_change:requested_code` | brand | code confirmation accepted by the provider |
| `email_change:send_failed` | brand | confirmation was not accepted; `metadata.failure_class` is `not_sent` (nothing came back) or `not_accepted` (a result came back that was not accepted) |
| `email_change:notice_failed` | brand | the notice to the previous address was not accepted; never changes the request's own outcome |
| `email_change:confirmed` | **account** | the address actually moved (POST confirm) |

`email_change:confirmed` records the **token's verified subject** (`user.pk`),
not `request.user` — the link path may carry no session at all, or a different
signed-in user.

**Scope of the privacy promise.** This is a targeted projection over the new
rows, not framework-wide redaction. `email_change:bad_password`,
`email_change:cancelled` and the token/OTP diagnostics keep the context they
have always carried.

### `_notify_old_email_change_address`

- Returns immediately when the account has no old address — a first-email
  account used to hand `""` to the mailbox, which raised and filed a raw event.
- Calls `user.send_template_email(..., fail_silently=False)` deliberately: the
  forgiving default files its own raw `email:send_failed` with provider text.
  The failure is caught here and reported once, safely.
- Runs **after** acceptance and after the `requested`/`requested_code` row, and
  never re-raises.

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

## Shipped Email Templates

Django-MOJO ships all three email-change templates. Projects may override the
database rows for branding or product-specific routing.

### `email_change_confirm` (link flow)

Sent to the **new** address when `method: "link"`. Context variables:

| Variable | Description |
|---|---|
| `token_url` | Fully resolved URL of the confirmation landing page, carrying the `ec:` token |
| `new_email` | The new email address being confirmed |
| `user` | Basic user dict |

The shipped template renders:

```
{{ token_url }}
```

`_send_email_change_confirm` builds that URL through `build_token_url` using
the `email_change` flow. **Since #3257 that flow points at this deployment's own
landing page** — `BASE_URL` plus `GET /api/auth/email/change/confirm` (the `/api`
segment comes from `MOJO_PREFIX`) — not at `WEBAPP_BASE_URL` + `WEBAPP_AUTH_PATH`.
`WEBAPP_BASE_URL` is the *frontend* origin, and on any deployment whose frontend
is a separate SPA there is no page there that can handle an `ec:` token. It may
still be wrapped in a shortlink.

**The GET no longer commits anything.** It renders a page that describes what
will happen, carries the token as an inert `json_script` data block, names no
account, and acts only when the person presses the button — which sends
`POST /api/auth/email/change/apply` with `{ "token": "ec:..." }`,
`credentials: "omit"` and no `Authorization` header. Opening, prefetching or
scanning the URL changes nothing. Confirming needs JavaScript; a `<noscript>`
block says so, and the token is still unspent afterwards.

**The button posts to `apply`, not to `confirm`, on purpose.** The `confirm`
POST ends in `jwt_login`, so a landing pointed at it would hand a full
access+refresh pair — plus `last_login`, the `USER_LOGIN_HANDLER` and a
`UserLoginEvent` — to whatever browser opened the emailed link, which may be a
shared machine or a mail client's embedded webview. The page then throws the
pair away and tells the person they were not signed in. `apply` runs the
identical commit path (`_commit_email_change`: availability re-check, the
direct email/username writes, `auth_key` rotation, the `email:changed` log
line, the one account-global `email_change:confirmed` row, the realtime event)
and returns `{"status": true, "message": "Email address changed",
"data": {"email": "<new address>"}}`. It has its own strict rate bucket
(`email_change_apply`, 10/hour/IP), sized like the `confirm` POST's, so the
landing's confirmations and the SPA's cannot eat each other's budget. It is
token-only: the 6-digit code belongs to an authenticated SPA and stays on
`confirm`.

**If your SPA wants to own this confirmation**, `WEBAPP_BASE_URL` /
`WEBAPP_AUTH_PATH` no longer reach it — override the `email_change_confirm`
email template so the link points at your own page, and have that page call
either endpoint itself: `POST /api/auth/email/change/confirm` when it wants the
JWT back, `POST /api/auth/email/change/apply` when it only wants the address
committed. If you only want the framework's page to look like yours, override
`account/email_change_landing.html` (or the shared
`account/token_landing_base.html`) via `TEMPLATES.DIRS` instead.

**`/auth?flow=email_change&token=ec:…` still works.** Links already in inboxes
hit `on_login_page`, which redirects them to the landing server-side before any
bouncer work, keyed on the token prefix.

The `&redirect=` destination must be `http`, `https`, or scheme-less — anything else (a custom app scheme such as `myapp://home`, `javascript:`, `data:`) is dropped by `mojo.helpers.urls.safe_nav_url` before it reaches the template, and the page renders with **no "Go back" link at all**. The host is deliberately not restricted; scheme-relative and path-relative values pass through unchanged and may still resolve off-origin. The same rule applies to `&redirect=` on `GET /api/auth/verify/email/confirm` and `GET /api/account/deactivate/confirm`. `&redirect=` is only ever an optional navigation link on these pages — nothing auto-navigates, and no `<meta http-equiv="refresh">` is ever emitted.

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
_record_account_activity(
    "email_change:confirmed", user.pk,
    "Email address changed",
    "The account email address was changed and verified.",
    provenance="account", source_ip=getattr(request, "ip", None))
_send_account_realtime_event(user, "account:email:changed", {"email": new_email})
```

Key points:

- **`User.objects.filter(...).update(...)`** is used deliberately to bypass the REST field guard that normally makes `email` and `is_email_verified` read-only via the API.
- **`auth_key` rotation** immediately invalidates every JWT signed with the old key — including any attacker session that may have triggered the flow. This applies to both the link path and the code path.
- **`username` sync** only occurs when `user.username` was equal to the old `user.email` at the time of confirm. Accounts that use a separate username are not affected.
- **`user.log`** writes an audit record queryable via the standard incident/audit log.
- **`email_change:confirmed`** is the one account-activity row this flow writes with `provenance="account"` — the address moved for the whole account, so it stays visible under every brand in the self-service feed. Only the POST handler writes it; the GET landing page is documented separately.
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

**Why `current_password` is optional (not required)**

`current_password` is accepted for users who have one but is no longer required. Passwordless accounts (passkey / SMS-OTP) have no usable password and would otherwise be locked out. The authenticated session is the primary ownership proof; for deployments that need a stronger freshness gate, set `FRESH_AUTH_WINDOW` (the `@md.requires_fresh_auth()` decorator enforces it). See [step_up_auth.md](step_up_auth.md).

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
