# Email & Phone Verification — REST API Reference

## Overview

By default, users can log in regardless of whether their email or phone number has been verified. Two server-side settings enable stricter behavior:

- `REQUIRE_VERIFIED_EMAIL` (default: `False`) — when `True`, logins where the identifier is an email address are blocked until the user's email is verified. Logins via plain username are not affected.
- `REQUIRE_VERIFIED_PHONE` (default: `False`) — when `True`, phone-based (SMS) logins are blocked until the user's phone number is verified.

When a login attempt is blocked by one of these gates, the API returns a structured error response rather than a generic 403, so your client can prompt the user to verify instead of showing an unexplained failure:

```json
{
  "status": false,
  "code": 403,
  "error": "email_not_verified",
  "message": "Please verify your email before logging in."
}
```

Detect `error: "email_not_verified"` on a 403 and show a **Resend verification email** prompt. The user does not need to re-enter their password — they just need to click the link in the email.

The equivalent response when phone verification is required:

```json
{
  "status": false,
  "code": 403,
  "error": "phone_not_verified",
  "message": "Please verify your phone number before logging in."
}
```

---

## Email Verification

Email verification supports two flows depending on your integration context:

- **Link flow** (default) — sends a verification link to the user's inbox. Suitable for post-registration flows or unauthenticated resend scenarios.
- **Code flow** — sends a 6-digit OTP to the user's inbox. Suitable for in-portal verification where you don't want the user to leave the page to click a link.

Both flows use the same send endpoint with an optional `method` parameter.

---

### Send a Verification Email

**POST** `/api/auth/verify/email/send`

Requires authentication (Bearer token). Sends a verification message to the logged-in user's email address.

**Request (link flow — default):**

```json
{ "method": "link" }
```

The `method` field is optional. Omitting it is equivalent to `"link"`.

**Request (code flow):**

```json
{ "method": "code" }
```

**Response (link flow):**

```json
{
  "status": true,
  "message": "Verification email sent"
}
```

**Response (code flow):**

```json
{
  "status": true,
  "message": "Verification code sent"
}
```

If the email address is already verified, no message is sent regardless of `method`:

```json
{
  "status": true,
  "message": "Email is already verified"
}
```

**A 200 means the email provider accepted the message — not that it landed in the inbox.** The message can still bounce or be rejected by the recipient's mail server afterwards; that is reported later, not on this call.

#### Failure response

If the provider did **not** accept the message — no sending mailbox is configured, the provider refused it, or the provider is unreachable — the endpoint says so instead of claiming success:

```
HTTP 503
```

```json
{
  "status": false,
  "code": 503,
  "error": "Unable to send the email right now. Please try again in a few minutes."
}
```

The message is fixed: no provider error text is ever exposed. Offer the user a **Retry** button rather than telling them to check their inbox. The verification token or code is still generated and stored, so a retry is safe — it simply rotates it.

Retries share this endpoint's rate limit of **5 requests per 300 seconds per IP**, which the public resend endpoint below also draws from, so keep the retry manual (a button) rather than automatic. The "few minutes" in the copy matches that window.

Two other error cases:

| Status | `error` | Meaning |
|---|---|---|
| 400 | `No email address on account` | The account has no email address (e.g. registered by phone only). Retrying will never help — collect an address first. |
| 401 / 403 | — | Not authenticated. |

> **Note:** There is also a public (unauthenticated) endpoint at `POST /api/auth/email/verify/send` that accepts a `username` or `email` field and always returns 200 regardless of account existence (prevents enumeration) — including when the send fails, since any other answer would be an enumeration oracle. That endpoint is intended for post-registration nudges and does not support the `method` parameter — it always sends a link.

> **Deployments running the legacy `MOJO_APP_STATUS_200_ON_ERROR` shim** receive the same body over HTTP 200, with `"code": 503` inside it — read `status`/`code` from the body, exactly as for any other error on those deployments.

---

### Confirm — Code Flow

**POST** `/api/auth/verify/email/confirm`

Requires authentication (Bearer token). Submits the 6-digit code received via email. On success, sets `is_email_verified = true` on the account. Does **not** issue a new JWT — the user's existing session remains active.

**Request:**

```json
{ "code": "123456" }
```

**Response:**

```json
{ "status": true, "message": "Email verified" }
```

**Error responses:**

| Status | `error` | Meaning |
|---|---|---|
| 400 | `Value Error` | Invalid or expired code |

Codes expire after `EMAIL_VERIFY_CODE_TTL` seconds (default 10 minutes) and are single-use. Codes and links are mutually exclusive — generating one via `/send` clears any outstanding token of the other type.

---

### Confirm — Link Flow

The verification email contains a link with a `token` query parameter. There are two ways to handle link clicks depending on your setup.

**Option A — Link opens the framework's confirmation page (the shipped default)**

Since #3257 the email link points at `GET /api/auth/verify/email/confirm?token=ev:...` on the **API origin** (built from `BASE_URL`, not `WEBAPP_BASE_URL`), and that page is a **confirmation landing**:

* Opening it verifies nothing. It does not validate or consume the token, does not touch the account, and never displays the account's email address or username. A mail scanner, link preview or browser prefetch leaves everything as it found it — that is the whole point of the change.
* The page describes what will happen and offers one button. Pressing it POSTs `{"token": "ev:…"}` to `POST /api/auth/email/verify/confirm` with `credentials: "omit"` and no `Authorization` header.
* That confirm is **verify-only**: it marks the address verified and returns `{"status": true, "message": "Email verified", "data": {"email": "…"}}`. It issues **no JWT**, does not touch `last_login`, and records no login. Clicking a verification link is not signing in — the page tells the person to return to the app.
* **Confirming requires JavaScript.** (This retires the old "No frontend JavaScript is required" guarantee.) A `<noscript>` block explains it, and because the GET consumes nothing, opening the same link later in a JavaScript-capable browser still works.
* A network failure or unreadable response shows an *"we could not confirm whether this went through"* state — it never claims the account was unchanged.

Append `&redirect=https://yourapp.com/dashboard` to the link in the email to add a **Go back** link to the page. Nothing auto-navigates: no `<meta http-equiv="refresh">` is ever emitted, on any state.

**The destination must be `http`, `https`, or scheme-less.** Anything else — `javascript:`, `data:`, or a custom app scheme such as `myapp://home` — is dropped and **no link is rendered at all**. Status code and page copy are unchanged. Point deep links at an https universal/app link instead. The **host is deliberately not restricted** — a cross-origin `https://` destination works — and scheme-relative or path-relative values pass through unchanged and may still resolve off-origin. Passing the parameter more than once (`?redirect=a&redirect=b`) is refused rather than taking the last value. The same applies to `?token=`: a repeated or structured value is treated as absent, and the page renders its invalid-link state with no button.

**Option B — Frontend handles the token (SPA / mobile apps)**

The email link points to a frontend route (e.g. `/verify-email?token=ev:...`). The frontend extracts the token and submits it via API.

**POST** `/api/auth/email/verify`

```json
{ "token": "ev:4e6f74546f6b656e..." }
```

On success, the server marks `is_email_verified = true` and **logs the user in**, returning a full JWT — no separate login step is needed. Unchanged, and still supported.

> Want verification *without* a session? Use **`POST /api/auth/email/verify/confirm`** instead — same body, same token, but it only sets `is_email_verified` and returns `{"status": true, "message": "Email verified", "data": {"email": "…"}}`. No JWT, no `last_login` update, no login event. This is what the framework's own confirmation page calls, and it is the right endpoint whenever the person is not actually signing in.

**Response:**

```json
{
  "status": true,
  "data": {
    "access_token": "eyJhbGci...",
    "refresh_token": "eyJhbGci...",
    "user": {
      "id": 42,
      "username": "alice@example.com",
      "display_name": "Alice"
    }
  }
}
```

Tokens are **single-use** and expire after 24 hours by default (configurable via `EMAIL_VERIFY_TOKEN_TTL`). An invalid or expired token returns:

```json
{
  "status": false,
  "code": 400,
  "error": "Invalid token"
}
```

---

### Recommended UI Flow — Email Verification

**Portal / in-context (code flow):**

1. Call `POST /api/auth/verify/email/send` with `{ "method": "code" }`.
2. Show an inline OTP input: *"We sent a 6-digit code to alice@example.com. Enter it below."*
3. Submit the code to `POST /api/auth/verify/email/confirm`.
4. On success, dismiss the prompt. The `account:email:verified` realtime event will also fire — use it to update any other open views.

**Standard / link flow:**

1. Call `POST /api/auth/verify/email/send` (no body, or `{ "method": "link" }`).
2. Display: *"A verification link has been sent to alice@example.com. Click it to verify."*
3. When the user clicks the link, handle it via Option A (API renders the result page) or Option B (frontend extracts the token and calls `POST /api/auth/email/verify`).

---

## Invite Links

When a user is invited to the system or to a group, they receive an invite email containing a token. This token serves two purposes at once:

1. It verifies their email address.
2. It logs them in immediately — no password is required to complete verification.

**POST** `/api/auth/invite/accept`

```json
{ "token": "iv:4e6f74546f6b656e..." }
```

The response is identical to the email verify response — a full JWT on success:

```json
{
  "status": true,
  "data": {
    "access_token": "eyJhbGci...",
    "refresh_token": "eyJhbGci...",
    "user": {
      "id": 17,
      "username": "bob@example.com",
      "display_name": "Bob"
    }
  }
}
```

Invite tokens expire after 7 days by default (configurable via `INVITE_TOKEN_TTL`).

If the invited user has not yet set a password, they will be logged in but passwordless. After accepting the invite, prompt them to set a password using the standard password reset flow — they are already authenticated, so the reset can be performed directly from the authenticated session.

An invalid or expired invite token returns:

```json
{
  "status": false,
  "code": 400,
  "error": "Invalid token"
}
```

---

## Phone Verification

Phone verification uses a 6-digit SMS code rather than a link.

### Send verification code

**POST** `/api/auth/verify/phone/send`

Requires authentication. Sends a 6-digit OTP to the user's `phone_number` on file.

Returns 200 immediately if the phone is already verified. Returns 400 if no phone number is on the account.

**Request:** No body required.

**Response:**
```json
{ "status": true, "message": "Verification code sent" }
```

**Error responses:**

| Status | `error` | Meaning |
|---|---|---|
| 400 | `Value Error` | No phone number on account, or number is invalid |

---

### Confirm verification code

**POST** `/api/auth/verify/phone/confirm`

Requires authentication. Submits the 6-digit code received via SMS. On success, sets `is_phone_verified = true` on the account. Does **not** issue a new JWT — the user's existing session remains active.

**Request:**

```json
{ "code": "123456" }
```

**Response:**
```json
{ "status": true, "message": "Phone verified" }
```

**Error responses:**

| Status | `error` | Meaning |
|---|---|---|
| 400 | `Value Error` | Invalid or expired code |

Codes expire after `PHONE_VERIFY_CODE_TTL` seconds (default 10 minutes) and are single-use.

---

### Automatic phone verification via SMS login

When a user completes a standalone SMS login — `POST /api/auth/sms/login` followed by `POST /api/auth/sms/verify` — successfully entering the OTP code is also treated as proof of phone ownership. The server automatically sets `is_phone_verified = true` on the first successful standalone verify. This path is equivalent to the dedicated verify flow above, but combines login and verification into one step.

See [SMS OTP](mfa_sms.md) for the full SMS login flow.

---

## Verification State in the User Profile

**GET** `/api/user/me`

The authenticated user's profile includes verification flags:

```json
{
  "status": true,
  "data": {
    "id": 42,
    "username": "alice@example.com",
    "display_name": "Alice",
    "is_email_verified": true,
    "is_phone_verified": false
  }
}
```

Use these fields to decide whether to surface a verification prompt in your UI — for example, a banner encouraging the user to verify their email even when `REQUIRE_VERIFIED_EMAIL` is not enabled.

---

## Realtime Events

After successful verification, the server emits a WebSocket event to all of the user's active connections. Listen for these events to update the UI in real-time without polling or page reloads.

### Email verified

Emitted after any confirm path succeeds — `POST /api/auth/verify/email/confirm` (code), `POST /api/auth/email/verify/confirm` (verify-only token) or `POST /api/auth/email/verify` (verify-and-login). The **GET** landing emits nothing: it changes nothing.

```json
{
  "event": "account:email:verified",
  "data": { "email": "alice@example.com" }
}
```

Use this to dismiss a "please verify your email" banner, update the profile icon, or unlock features gated on `is_email_verified` without requiring a page reload.

### Phone verified

Emitted after `POST /api/auth/verify/phone/confirm` succeeds:

```json
{
  "event": "account:phone:verified",
  "data": { "phone_number": "+14155550123" }
}
```

Use this to dismiss a phone verification prompt or enable SMS-dependent features without requiring a page reload.

---

## Template Customisation

> ### ⚠️ Upgrade note — the old template names are gone
>
> #3257 replaced the two server-rendered confirm pages with three confirmation
> landings, under **new file names**:
>
> | Old (deleted) | New |
> |---|---|
> | `account/email_verify_confirm.html` | `account/email_verify_landing.html` |
> | `account/email_change_confirm.html` | `account/email_change_landing.html` |
> | — (this page did not exist) | `account/account_deactivate_landing.html` |
> | — | `account/token_landing_base.html` (shared base for all three) |
>
> **This breaks branded overrides silently.** Django resolves the *new* name, so
> a deployment's own `templates/account/email_verify_confirm.html` simply stops
> being rendered — no error, no warning, just the framework's default page.
> Rename your override and re-check it against the new blocks below.

The `GET /api/auth/verify/email/confirm` endpoint renders `account/email_verify_landing.html`, which extends `account/token_landing_base.html`. Both are minimal and fully self-contained — no external stylesheet, script, image or font. To customise, create your own version at a path that takes priority in Django's `TEMPLATES` settings:

```
yourproject/templates/account/email_verify_landing.html
yourproject/templates/account/token_landing_base.html   # to restyle all three at once
```

Blocks the flow templates override: `page_title`, `headline`, `consequence`, `button_class`, `button_label`, `success_headline`, `success_copy`, `success_footer`.

Template context variables:

| Variable | Type | Description |
|---|---|---|
| `has_token` | bool | `True` when the request carried a usable `?token=`. `False` renders the invalid-link state and **no submit control** |
| `landing_data` | dict | `{"token": …, "confirm_url": …}`, emitted through `json_script` as an inert `application/json` data block. The token appears nowhere else on the page and is never written to `localStorage`/`sessionStorage` |
| `redirect_url` | string | Vetted value of the `?redirect=` param — always either `""` or an `http`/`https`/relative URL. May be empty |

There is no `success` / `email` / `error_title` / `error_message` / `redirect_delay` any more: the GET knows none of that, because it never validates the token. Success and error copy are page states the button's response switches on, client-side.

`redirect_url` is scheme-guarded **before** it reaches the context, so an overridden template inherits the guard for free: it is never a `javascript:`/`data:`/custom-app value, and a refused destination arrives as `""`. Keep the link inside a `{% if redirect_url %}` wrapper so a refused destination omits the link rather than rendering a dead one.

If you override the base, keep three properties: the token stays inside the `json_script` block (never interpolated into `<script>` text or an `href`), the page loads nothing from another origin, and nothing navigates on its own.

---

## Settings Reference

| Setting | Default | Description |
|---|---|---|
| `REQUIRE_VERIFIED_EMAIL` | `False` | Block logins where the identifier is an email address until the user's email is verified. Username-based logins are not gated. |
| `REQUIRE_VERIFIED_PHONE` | `False` | Block phone (SMS) logins until the user's phone is verified |
| `EMAIL_VERIFY_TOKEN_TTL` | `86400` (24 h) | Expiry time for email verification link tokens, in seconds |
| `EMAIL_VERIFY_CODE_TTL` | `600` (10 min) | Expiry time for email verification OTP codes, in seconds |
| `INVITE_TOKEN_TTL` | `604800` (7 d) | Expiry time for invite tokens, in seconds |
| `PHONE_VERIFY_CODE_TTL` | `600` (10 min) | Expiry time for SMS phone verification codes, in seconds |

---

## Write Protection on Verification Fields

`is_email_verified` and `is_phone_verified` are **read-only** from the REST API for all non-superuser actors — including the account owner. Attempting to set them directly via `POST /api/user/<id>` will return a 403:

```json
{
  "status": false,
  "code": 403,
  "error": "Permission denied"
}
```

The only legitimate paths that set these fields are:

| Action | Sets |
|---|---|
| `POST /api/auth/email/verify` (link token redemption) | `is_email_verified = true` |
| `GET /api/auth/verify/email/confirm` (link click) | nothing — renders the confirmation page |
| `POST /api/auth/email/verify/confirm` (the page's button) | `is_email_verified = true`, no session |
| `POST /api/auth/verify/email/confirm` (OTP code) | `is_email_verified = true` |
| `POST /api/auth/invite/accept` (invite token redemption) | `is_email_verified = true` |
| `POST /api/auth/verify/phone/confirm` (OTP code) | `is_phone_verified = true` |
| `POST /api/auth/sms/verify` without `mfa_token` (standalone SMS login) | `is_phone_verified = true` |
| Superuser `POST /api/user/<id>` | either field, either value |

Superusers can also **revoke** verification (set back to `false`) — for example, after a suspected account takeover where the email address was changed.

This protection applies to both create and update requests. A non-superuser cannot create a new user record with `is_email_verified: true` pre-set in the payload.

> To allow users to change their email address, see [Email Change](email_change.md). The change uses a dedicated verify-then-commit flow that bypasses the write protection guard safely.
>
> To allow users to change their phone number, see [Phone Number Change](phone_change.md). Replacing an existing phone number requires OTP confirmation to the new number before the change is committed.