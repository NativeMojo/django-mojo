# Email Change — REST API Reference

## Overview

Self-service email change is a two-step flow: **request → confirm.**

1. The authenticated user submits their desired new address.
2. Either a **confirmation link** or a **6-digit OTP code** is sent to the **new** address (your choice). The current email is not changed yet.
3. The user confirms — by clicking the link or submitting the code — and the new address is committed.

`current_password` is **optional**. If provided and correct it is validated; if omitted the request proceeds without a password check — this supports OAuth-only and passkey-only users who have no usable password. When `FRESH_AUTH_WINDOW` is enabled server-side, a recent login is required instead (see [Step-Up Auth](step_up_auth.md)).

The feature is controlled by the `ALLOW_EMAIL_CHANGE` setting (default `True`). When set to `False`, all requests to `POST /api/auth/email/change/request` return 403.

---

## Step 1 — Request the Change

**POST** `/api/auth/email/change/request`

Requires authentication (Bearer token). Rate limited.

**Request (link flow — default):**

```json
{
  "email": "newemail@example.com"
}
```

**Request (code flow — portal use):**

```json
{
  "email": "newemail@example.com",
  "method": "code"
}
```

`current_password` is accepted in the body but is optional — omit it for OAuth/passkey-only users. The `method` field is optional and defaults to `"link"`. Pass `"code"` to receive a 6-digit OTP at the new address instead of a confirmation link — this is the recommended approach when the user is already in an authenticated portal context and should not have to leave to click a link.

**Error cases (both methods):**

| Condition | Status | Response |
|---|---|---|
| Step-up auth required (stale session, `FRESH_AUTH_WINDOW` enabled) | 440 | `"error": "reauth_required"` |
| `current_password` provided but incorrect | 401 | `"error": "Incorrect password"` |
| `email` has an invalid format | 400 | `"error": "Invalid email address"` |
| `email` is the same as the current address | 400 | `"error": "New email must be different from current email"` |
| `email` is already in use by another account | 400 | `"error": "Email already in use"` |
| Feature disabled via `ALLOW_EMAIL_CHANGE` | 403 | `"error": "Email change is not allowed"` |
| The email provider did not accept the confirmation message | 503 | `"error": "Unable to send the email right now. Please try again in a few minutes."` |

A **notification** is sent to the **old** address informing the real owner so they can react if the request was not made by them — but only after the confirmation message was accepted, and only when the account has an old address (a first-email account has none). Nothing is committed yet.

### When the email could not be sent

```json
{
  "status": false,
  "code": 503,
  "error": "Unable to send the email right now. Please try again in a few minutes."
}
```

- **It is retryable, and the pending change survives it.** The stored pending address and the link token / OTP are untouched, so calling `/request` again resumes the same change. Show the message and offer a retry.
- **The error text is fixed.** It never carries provider detail, so do not parse it for a cause — there is nothing behind it to read.
- **Mind the budget.** The request endpoint allows 5 attempts per hour per IP and failures count, so back off rather than retrying in a tight loop.
- **Deployments running the legacy `MOJO_APP_STATUS_200_ON_ERROR` shim** receive the same body over HTTP 200 with `"code": 503` inside it — read `status`/`code` from the body, exactly as for any other error on those deployments.

> **A 200 means the provider accepted the message, not that it arrived.** The address can still bounce afterwards. "We sent it" is the strongest honest claim the endpoint can make at request time, so word your success copy that way.

**Response (link flow):**

```json
{
  "status": true,
  "message": "A confirmation link has been sent to your new email address."
}
```

**Response (code flow):**

```json
{
  "status": true,
  "message": "A verification code has been sent to your new email address."
}
```

> **Note — only one pending change at a time.** Calling `/request` again (regardless of method) automatically invalidates any previously issued link or code before issuing a new one.

---

## Step 2 — Confirm the Change

### Option A — Code confirm (portal / in-context, authenticated)

Use this when the request was made with `method: "code"`. The user stays in the portal and types the code they received.

**POST** `/api/auth/email/change/confirm`

Requires authentication (Bearer token). Rate limited.

**Request:**

```json
{ "code": "847291" }
```

On success:

- The new email address is committed to the account.
- `is_email_verified` is set to `true`.
- **All other active sessions are invalidated** — the account's `auth_key` is rotated.
- A fresh JWT is issued for the current session.
- If the account uses email as its username, `username` is updated automatically.

**Response** (identical to a normal login):

```json
{
  "status": true,
  "data": {
    "access_token": "eyJhbGci...",
    "refresh_token": "eyJhbGci...",
    "user": {
      "id": 42,
      "username": "newemail@example.com",
      "display_name": "Alice"
    }
  }
}
```

Replace all stored tokens with the new JWT immediately. Previously stored tokens are no longer valid.

**Error responses:**

| Response | Meaning |
|---|---|
| `"error": "Invalid code"` | Code does not match |
| `"error": "Expired code"` | Code is older than 10 minutes |
| `"error": "No pending email change"` | No code-flow change was initiated |
| `"error": "Email address is no longer available"` | Another account claimed the address in the window |
| 401 | No valid Bearer token — authentication is required for the code path |

Codes expire after `EMAIL_CHANGE_CODE_TTL` seconds (default 10 minutes) and are **single-use**.

---

### Option B — Link confirm via API page (simple setups)

Use this when the request was made with the default `method: "link"` and your
**This is what the shipped email links to since #3257** — `token_url` resolves
to `{BASE_URL}/api/auth/email/change/confirm?token=ec:...`, not to a frontend
route. Option C below is still available for deployments that override the email
template and handle the token themselves.

**GET** `/api/auth/email/change/confirm?token=ec:...`

Public endpoint. No authentication header required. Rate limited.

**The GET commits nothing.** It renders `account/email_change_landing.html` — a
minimal, self-contained confirmation page — and that is all it does: it does not
validate the token, does not consume it, and never displays the account's
address. A mail scanner, link preview or browser prefetch that opens the URL
leaves the account untouched. Downstream projects can override the template (see
[Template Customisation](#template-customisation)).

Pressing the page's button sends `{"token": "ec:…"}` to
`POST /api/auth/email/change/confirm` (Option C's endpoint) with
`credentials: "omit"` and no `Authorization` header. That POST is what commits
the change, rotates `auth_key` and returns a fresh JWT — which this page
**discards**, telling the person to sign in again. Confirming requires
JavaScript; a `<noscript>` block says so, and the token survives for a later
attempt in a JavaScript-capable browser.

**Optional redirect parameter:**

Append `&redirect=https://yourapp.com/login` to the link in the confirmation email to add a **Go back** link to the page. Nothing auto-navigates — no `<meta http-equiv="refresh">` is emitted in any state.

```
GET /api/auth/email/change/confirm?token=ec:4e6f...&redirect=https://app.example.com/login
```

**The destination must be `http`, `https`, or scheme-less.** Anything else — `javascript:`, `data:`, or a custom app scheme such as `myapp://home` — is dropped: **no link is rendered at all**, and no automatic redirect is emitted. Status code and page copy are unchanged. Point deep links at an https universal/app link instead. The **host is deliberately not restricted** — a cross-origin `https://` destination works — and scheme-relative or path-relative values pass through unchanged and may still resolve off-origin. Passing the parameter more than once (`?redirect=a&redirect=b`) is refused rather than taking the last value.

---

### Option C — Link confirm via frontend (SPA / mobile)

Use this when the request was made with the default `method: "link"` and your frontend handles the token directly.

**POST** `/api/auth/email/change/confirm`

Public endpoint. No authentication header required. Rate limited.

**Request:**

```json
{ "token": "ec:4e6f74546f6b656e..." }
```

On success: same behavior as the code path — new email committed, `auth_key` rotated, fresh JWT returned.

---

### Token / code behaviour summary

| | Code (method: code) | Link (method: link) |
|---|---|---|
| Confirm endpoint | `POST /confirm` with `{ "code": "..." }` | `GET /confirm?token=ec:...` or `POST /confirm` with `{ "token": "ec:..." }` |
| Auth required on confirm | Yes — Bearer token | No — token/link is the credential |
| TTL | 10 minutes (configurable) | 1 hour (configurable) |
| Single-use | Yes | Yes |

If another account claimed the new email address in the window between the request and the confirm step, the confirmation is rejected on all paths.

**Error responses for invalid or expired tokens (link paths):**

```json
{
  "status": false,
  "code": 400,
  "error": "Invalid or expired token"
}
```

---

## Cancelling a Pending Change

**POST** `/api/auth/email/change/cancel`

Requires authentication (Bearer token).

Immediately invalidates any outstanding confirmation link **or** OTP code — even before their TTL expires. The account is unchanged.

**Response:**

```json
{
  "status": true,
  "message": "Pending email change has been cancelled."
}
```

This endpoint is **idempotent**: if there is no pending change, it still returns 200 with the same response body.

---

## Realtime Events

After a successful email change (on any confirm path), the server emits a WebSocket event to all of the user's active connections:

```json
{
  "event": "account:email:changed",
  "data": { "email": "newemail@example.com" }
}
```

Because `auth_key` is rotated on confirm, any open sessions will find their JWTs immediately invalid. This event gives them a clean signal to re-prompt login rather than silently failing on the next API call. Listen for it and redirect to your login screen with a message like *"Your email address was changed. Please sign in again."*

---

## Recommended UI Flow

### Portal / in-context (code flow)

1. Show the user a form with a field for `email` (new address).
2. Call `POST /api/auth/email/change/request` with `method: "code"`. On success, display an OTP entry prompt: *"A 6-digit code has been sent to newemail@example.com. Enter it below to confirm."*
3. Optionally show a **Cancel** button that calls `POST /api/auth/email/change/cancel`.
4. When the user submits the code, call `POST /api/auth/email/change/confirm` with `{ "code": "..." }` and a valid Bearer token.
5. Replace all stored tokens with the new JWT and continue the session.

### Simple setup (link → API page)

1. Show the user a form with a field for `email`.
2. Call `POST /api/auth/email/change/request` (no `method` param). On success, display: *"A confirmation link has been sent to newemail@example.com. Check your inbox and click the link to confirm."*
3. Optionally show a **Cancel pending change** button that calls `POST /api/auth/email/change/cancel`.
4. The link in the email points directly to `GET /api/auth/email/change/confirm?token=ec:...&redirect=https://yourapp.com/login`. The server renders the confirmation page and the user presses its button; no frontend route needed.

### SPA / mobile setup (frontend handles link)

1–3. Same as the simple setup.
4. The link in the email points to a frontend route like `/email-change?token=ec:...`. Your frontend extracts the token and calls `POST /api/auth/email/change/confirm` with `{ "token": "ec:..." }`.
5. Replace all stored tokens with the new JWT and continue the session.

---

## Security Notes

- **`current_password` is optional.** If provided and non-empty it is validated; if omitted the request proceeds. The primary ownership proof is the authenticated session; when `FRESH_AUTH_WINDOW` is enabled, a recent login is required as the step-up gate instead. See [Step-Up Auth](step_up_auth.md).
- **The old address always receives a notification.** If the real owner did not request the change, they should call `POST /api/auth/email/change/cancel` immediately and change their password.
- **All existing sessions are invalidated on confirm.** `auth_key` is rotated regardless of whether the link or code path was used.
- **Cancellation covers both paths.** `POST /api/auth/email/change/cancel` clears the `pending_email`, the outstanding `ec:` JTI, and any OTP code simultaneously — no matter which method was used to initiate the change.
- **Only one pending change at a time.** Issuing a new request (link or code) immediately invalidates the previous one before generating the new credentials.
- **The code path requires authentication.** The Bearer token is the session guard; the OTP proves ownership of the new address. Both must be correct.
- **Email availability is re-checked at confirm time.** Another account may have registered the target address in the window between request and confirm. All confirm paths reject the request if this has occurred.
- **Username is kept in sync.** If the account uses email as its username, the `username` field is updated automatically on confirm so login with the new address works immediately.

---

## Template Customisation

> ### ⚠️ Upgrade note — the old template name is gone
>
> #3257 replaced `account/email_change_confirm.html` with
> `account/email_change_landing.html` (extending the shared
> `account/token_landing_base.html`). **A branded override of the old name stops
> rendering silently** — Django resolves the new name and falls back to the
> framework's default page with no error. Rename yours and re-check it against
> the blocks and context below. The sibling renames are
> `account/email_verify_confirm.html` → `account/email_verify_landing.html`, and
> the new `account/account_deactivate_landing.html`.

The `GET /api/auth/email/change/confirm` endpoint renders `account/email_change_landing.html`, which extends `account/token_landing_base.html`. Both are minimal and fully self-contained — no external stylesheet, script, image or font. To customise, create your own version at a path that takes priority in Django's `TEMPLATES` settings:

```
yourproject/templates/account/email_change_landing.html
yourproject/templates/account/token_landing_base.html   # to restyle all three landings at once
```

Blocks the flow template overrides: `page_title`, `headline`, `consequence`, `button_class`, `button_label`, `success_headline`, `success_copy`, `success_footer`.

Template context variables:

| Variable | Type | Description |
|---|---|---|
| `has_token` | bool | `True` when the request carried a usable `?token=`. `False` renders the invalid-link state and **no submit control** |
| `landing_data` | dict | `{"token": …, "confirm_url": …}`, emitted through `json_script` as an inert `application/json` data block. The token appears nowhere else on the page and is never written to `localStorage`/`sessionStorage` |
| `redirect_url` | string | Vetted value of the `?redirect=` param — always either `""` or an `http`/`https`/relative URL. May be empty |

There is no `success` / `new_email` / `error_title` / `error_message` / `redirect_delay` any more: the GET knows none of that, because it never validates the token. Success and error copy are page states the button's response switches on, client-side.

`redirect_url` is scheme-guarded **before** it reaches the context, so an overridden template inherits the guard for free: it is never a `javascript:`/`data:`/custom-app value, and a refused destination arrives as `""`. Keep the link inside a `{% if redirect_url %}` wrapper so a refused destination omits the link rather than rendering a dead one.

If you override the base, keep three properties: the token stays inside the `json_script` block (never interpolated into `<script>` text or an `href`), the page loads nothing from another origin, and nothing navigates on its own.

Two email templates must also be defined in your project's email template system:

### `email_change_confirm` (link flow)

Sent to the **new** address when `method: "link"`. Django-MOJO ships a default
template containing the resolved URL:

```
{{ token_url }}
```

Since #3257 the URL is built server-side as
`{BASE_URL}/api/auth/email/change/confirm?token=...` — this deployment's own
confirmation page — then may be shortened. `WEBAPP_BASE_URL` and
`WEBAPP_AUTH_PATH` no longer steer this flow: they name the *frontend* origin,
where on a separate-SPA deployment no page exists that can handle an `ec:`
token. To send people to your own route instead, override this email template.
Context: `token_url`, `new_email`, `user`.

### `email_change_code` (code flow)

Sent to the **new** address when `method: "code"`. Django-MOJO ships a default
template that displays the 6-digit code prominently:

```
Your email change code is: {{ code }}
This code expires in 10 minutes.
```

Context: `code`, `new_email`, `user`.

### `email_change_notify`

Sent to the **old** address for both flows. Context: `new_email`. Should tell the owner what happened and direct them to cancel via `POST /api/auth/email/change/cancel` if they did not request this change.

---

## Settings Reference

| Setting | Default | Description |
|---|---|---|
| `ALLOW_EMAIL_CHANGE` | `True` | Set to `False` to disable self-service email change entirely. The request endpoint returns 403 when disabled. |
| `EMAIL_CHANGE_TOKEN_TTL` | `3600` (1 h) | Expiry time for link-flow email change tokens, in seconds |
| `EMAIL_CHANGE_CODE_TTL` | `600` (10 min) | Expiry time for code-flow OTP codes, in seconds |
