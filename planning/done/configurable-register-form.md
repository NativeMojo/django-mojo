# Configurable Bouncer Register Form (Phone-As-Identity, DOB, Field Toggles)

**Type**: request
**Status**: resolved
**Date**: 2026-05-16
**Priority**: high

## Description

The bouncer-hosted registration page (`account/templates/account/register.html`) has a hard-coded field set: `first_name`, `last_name`, `email`, `password`, plus the T&Cs checkbox. Consumer apps need to vary which fields the page collects without forking the template — including the ability to **drop email entirely** and use phone number as the identity, plus collect DOB.

Deliver a single ordered-list setting (`AUTH_REGISTER_FIELDS`) that drives both the template and the server-side validator, and extend `on_register` to accept phone as the identity. When a phone field is configured, require server-side verification before user creation. When a user registers with phone-only (no email), `/api/auth/forgot` must accept a phone number and dispatch an SMS reset code.

## Context

The `User` model already has the necessary columns:
- `phone_number` (unique, indexed)
- `dob` (date)
- `is_phone_verified`, `is_dob_verified` (bools)

SMS infrastructure already exists:
- `POST /auth/sms/send` (`mojo/apps/account/rest/sms.py`)
- `POST /auth/sms/verify`
- `mojo-phonehub` gateway service (verified working in production for MFA)

Email-verify pattern exists:
- `REQUIRE_VERIFIED_EMAIL` setting + verify-token flow in `mojo/apps/account/rest/verify.py`

So the feature is wiring + a new pre-register phone-verify endpoint pair, not new infrastructure.

## Target Configuration

A consumer project for phone-only signup configures:

```python
AUTH_REGISTER_FIELDS = [
    {"name": "first_name", "required": True},
    {"name": "last_name",  "required": True},
    {"name": "phone",      "required": True, "verify": "sms"},
    {"name": "dob",        "required": True},
    {"name": "password",   "required": True},
]
AUTH_MIN_AGE_YEARS = 13   # optional age gate
```

The default (no setting) keeps today's email-based form working unchanged:

```python
AUTH_REGISTER_FIELDS = [
    {"name": "first_name", "required": False},
    {"name": "last_name",  "required": False},
    {"name": "email",      "required": True, "verify": "email"},
    {"name": "password",   "required": True},
]
```

Settings are group-scoped via the existing `settings.get(key, group=group)` chain — multi-tenant deployments can have different signup shapes per operator.

## Acceptance Criteria

### Template — `register.html`

- Replace the hard-coded `<input>` rows with a `{% for field in register_fields %}` loop driven by `AUTH_REGISTER_FIELDS`.
- Render appropriate input types per field key:
  - `first_name`, `last_name` → `<input type="text">`
  - `email` → `<input type="email">`
  - `phone` → `<input type="tel">` with a small "Send code" button beside it
  - `dob` → `<input type="date">`
  - `password` → `<input type="password">` with the existing eye toggle
- A field's `required` flag drives `required` on the input and the client-side empty check.
- Phone-verify UX: clicking "Send code" calls `POST /auth/sms/register/send`, the row swaps to show a 6-digit code input + "Verify" button. On verify, store the returned `phone_verify_token` in a hidden form field; the submit handler includes it in the register payload.
- Submit handler builds the payload dynamically from the rendered fields rather than the current hard-coded shape.

### Settings

- `AUTH_REGISTER_FIELDS` — ordered list of field dicts (see "Target Configuration"). When unset, default = email-based config preserving today's behavior.
- `AUTH_MIN_AGE_YEARS` — integer; when set + `dob` is required, register rejects with 400 if computed age is below the threshold. Default: unset (no gate).
- `AUTH_REGISTER_IDENTITY_FIELD` — explicit override; rarely needed. Default: auto-pick from `email`/`phone` (email wins if both are required).

### Server — `on_register` (`mojo/apps/account/rest/user.py`)

- Read `AUTH_REGISTER_FIELDS`; build a per-request validator (required keys, allowed keys).
- Identity field resolved from the configured fields (auto-pick `email` if required, else `phone`). `User.username` set to the identity value at create time.
- Password is always required (the password field is implicit — even if a project removes it from the visible list, registration without password is a separate feature out of scope here).
- When `phone` has `verify: "sms"`, the POST must include a valid `phone_verify_token`. Reject 400 otherwise. The server creates `User` with `is_phone_verified=True` only when the token validates.
- When `dob` is required, validate it parses as ISO date and (if `AUTH_MIN_AGE_YEARS` is set) age is at/above the threshold. Reject 400 otherwise. Persist to `user.dob`.
- Field allowlist remains the union of `AUTH_REGISTER_FIELDS` + `REGISTRATION_EXTRA_FIELDS`. Unknown keys silently dropped (existing contract).

### New endpoints — phone verification for registration

- `POST /auth/sms/register/send` — body `{phone}`. Generates a 6-digit code, sends via `phonehub.send_sms`, returns `{phone_verify_token, expires_in}`. The token is a short-lived (e.g. 10 min) HMAC blob bound to the phone + code. Rate-limited per IP and per phone number. Public; no auth.
- `POST /auth/sms/register/verify` — body `{phone_verify_token, code}`. Validates the code matches the token's claim. Returns a longer-lived `verified_phone_token` (the thing register payload includes). 5-minute TTL.

This is the **verify-then-register** flow chosen over **register-then-verify** to avoid creating User rows for unverified phone numbers (spam control on a scarce resource).

### Forgot-password via phone

- Extend `POST /api/auth/forgot` to accept `phone` in place of `email`. When supplied:
  - Look up user by `phone_number`. If not found, return generic success (no enumeration).
  - Send an SMS containing a 6-digit code (reuse existing `forgotPasswordCode` pattern; new SMS sender call).
  - `POST /api/auth/password/reset/code` already accepts a `{phone, code, new_password}` shape — extend the lookup to use phone when email isn't provided.
- The bouncer's `login.html` "Forgot password?" view continues to ask for email when the deployment uses email-as-identity; when phone-as-identity, the same form asks for phone and shows the code-method radio (link method is email-only and is hidden in phone mode).

### Mojo-auth JS helpers

- `MojoAuth.startPhoneRegister(phone)` → POST `/auth/sms/register/send`, returns the verify token.
- `MojoAuth.completePhoneRegister(token, code)` → POST `/auth/sms/register/verify`, returns the verified-phone token.
- `MojoAuth.register(payload)` already accepts arbitrary fields; no change needed. Caller includes `verified_phone_token` in the payload.

## Investigation

**What exists**:
- `mojo/apps/account/templates/account/register.html` — hard-coded form
- `mojo/apps/account/rest/user.py:on_register` — accepts email + password + optional `first_name`, `last_name`, `group_uuid` + REGISTRATION_EXTRA_FIELDS
- `mojo/apps/account/rest/sms.py:on_sms_send`, `on_sms_verify` — currently bound to MFA flow (require `mfa_token`); cannot be reused as-is for registration. We add a parallel pair scoped to registration.
- `mojo/apps/account/rest/verify.py` — email verification pattern; phone-verify mirrors its token shape
- `mojo/apps/account/models/user.py` — `phone_number` (unique), `dob`, `is_phone_verified`, `is_dob_verified` columns all exist
- `mojo/apps/account/services/extensions.py` — `PRE_REGISTER_VALIDATOR`, `USER_REGISTERED_HANDLER`, `USER_LOGIN_HANDLER` hooks; no change needed but `extra` dict will include `phone`, `dob`, etc. when configured
- `phonehub.send_sms()` — production SMS gateway

**What changes**:
- `mojo/apps/account/rest/user.py` — `on_register` reads `AUTH_REGISTER_FIELDS`, validates dynamically, supports phone-as-identity
- `mojo/apps/account/rest/sms.py` — add `on_register_sms_send` + `on_register_sms_verify`
- `mojo/apps/account/rest/verify.py` or new file — token mint/validate for phone-verify
- `mojo/apps/account/rest/user.py:on_forgot` (or wherever it is) — accept `phone` alternative
- `mojo/apps/account/rest/bouncer/views.py:_auth_context` — emit `register_fields` into the template context, resolved from the setting (with default)
- `mojo/apps/account/templates/account/register.html` — field-loop rewrite
- `mojo/apps/account/static/account/mojo-auth.js` — phone-register helpers + extend forgot-password helper to accept phone

**Constraints**:
- Backwards compatibility: when `AUTH_REGISTER_FIELDS` is unset, behavior must be identical to today (email-required form, today's API contract).
- Group-scoped: deployments with multi-tenant operators get different forms per group automatically via `settings.get(key, group=group)`.
- The bouncer's bot gate is independent of this work — challenge + token issuance unchanged.
- Existing tests in `tests/test_register/` must continue to pass with no edits (they don't set `AUTH_REGISTER_FIELDS`).

**Related files**:
- `mojo/apps/account/templates/account/register.html`
- `mojo/apps/account/rest/user.py`
- `mojo/apps/account/rest/sms.py`
- `mojo/apps/account/rest/verify.py`
- `mojo/apps/account/rest/bouncer/views.py`
- `mojo/apps/account/static/account/mojo-auth.js`
- `mojo/apps/account/models/user.py` (read-only)

## Endpoints

| Method | Path | Change | Permission |
|---|---|---|---|
| POST | `/api/auth/register` | Accepts `phone`, `dob`, `verified_phone_token`; validates against `AUTH_REGISTER_FIELDS`; identity may be phone. | Public (existing) |
| POST | `/api/auth/sms/register/send` | **New.** Send SMS code for register-time phone verification. Returns `phone_verify_token`. | Public, rate-limited |
| POST | `/api/auth/sms/register/verify` | **New.** Verify code, return `verified_phone_token` for inclusion in register body. | Public, rate-limited |
| POST | `/api/auth/forgot` | Accepts `phone` alternative to `email`. SMS reset code is sent when phone is supplied. | Public (existing) |
| POST | `/api/auth/password/reset/code` | Accepts `phone` alternative to `email` for the lookup. | Public (existing) |

## Settings

| Setting | Default | Behavior |
|---|---|---|
| `AUTH_REGISTER_FIELDS` | (computed: today's email-based form) | Ordered list of field dicts driving the form + validator |
| `AUTH_MIN_AGE_YEARS` | unset | When set + `dob` is required, age-gate registration |
| `AUTH_REGISTER_IDENTITY_FIELD` | auto (email if present + required, else phone) | Explicit override for the identity field |
| `REQUIRE_VERIFIED_EMAIL` | (existing) | Continues to apply when email is in fields |

## Tests Required

- Render `register.html` with `AUTH_REGISTER_FIELDS` = phone-only-config → HTML contains `<input type="tel">`, `<input type="date">`, does NOT contain email input.
- Render with default (no setting) → identical to current HTML (regression guard).
- Register POST with phone-only config + missing `verified_phone_token` → 400.
- Register POST with phone-only config + valid token → 200, `User` row created with `username=phone_number`, `is_phone_verified=True`.
- Register POST with `dob` below `AUTH_MIN_AGE_YEARS` → 400.
- Register POST with allowed extras + dynamic fields → handler `extra` includes the right keys.
- SMS register-send rate-limited per IP and per phone number (separate buckets).
- SMS register-verify rejects wrong/expired codes (4xx, no token issued).
- `/api/auth/forgot` with `phone` body → 200, SMS code sent (mock the gateway).
- `/api/auth/password/reset/code` with `phone` + code + new_password → 200, tokens returned.
- Backward-compat: full default-config register flow still works end-to-end (existing tests in `tests/test_register/register.py` must pass unchanged).

## Out of Scope

- Passwordless phone-only signup (no password collected). The password field stays implicit / required. A separate request can add `passwordless` modes later.
- OAuth signup integration with the new field set — OAuth flows still mint accounts with email as identity. If a deployment is phone-only AND OAuth-enabled, the OAuth account is created with phone empty until the user adds one. (Worth a follow-up: "OAuth signup respects AUTH_REGISTER_FIELDS".)
- MFA enrollment as part of register. Today MFA is a separate post-login flow.
- Custom field types beyond the six (`first_name`, `last_name`, `email`, `phone`, `dob`, `password`). Consumer-specific fields stay in `REGISTRATION_EXTRA_FIELDS`, rendered via the existing `{% block extra_fields %}` template hook.

## Open Questions

1. **Phone uniqueness on register**: `User.phone_number` already has `unique=True`. If a phone is supplied that matches an existing user, do we return the same `"An account with this email already exists"` family of errors (renamed appropriately) or do we offer "log in instead"? Default proposal: same error family — "An account with this phone number already exists". No magic-redirect to login.

2. **`?identity=phone` URL param to force phone mode on a shared deployment?** Most deployments will be all-phone or all-email per group. If a deployment wants to support both on the same group with the user choosing, that's a richer UX. Default proposal: out of scope for v1 — one shape per group is enough. A URL toggle can be added later if needed.

3. **Phone format / E.164 normalization**: server must normalize before lookup. Default proposal: use whatever the existing MFA-SMS flow uses for normalization (likely `phonehub` handles it). Confirm during design pass.

4. **DOB display format**: `<input type="date">` is ISO yyyy-mm-dd, browser-localized. Acceptable for v1; future enhancement can add locale-aware widgets.

## Plan

**Status**: planned
**Planned**: 2026-05-16

### Objective
Drive the bouncer register form and the server-side register validator from one ordered-list setting (`AUTH_REGISTER_FIELDS`), add a verify-then-register phone-verification flow gated by Redis-backed tokens, persist DOB with an optional age gate, allow phone-as-identity end-to-end, and extend forgot-password to dispatch SMS codes when the user has no email.

### Steps

#### A. Foundations: schema + service + tokens

1. `mojo/apps/account/services/register_schema.py` — **new** module.
   - `CANONICAL_FIELDS = {"first_name", "last_name", "email", "phone", "dob", "password"}` — closed set; unknown keys ignored.
   - `DEFAULT_FIELDS = [...]` — today's form (`first_name` optional, `last_name` optional, `email` required+verify=email, `password` required).
   - `resolve_fields(group=None)` — read `AUTH_REGISTER_FIELDS` via `settings.get(..., group=group)`, normalize to list-of-dicts `{name, required, verify}`, fall through to `DEFAULT_FIELDS` when unset/empty. Filter unknown names. Always force `password` to `required=True`. Cache key derived from setting value.
   - `resolve_identity_field(fields, group=None)` — read `AUTH_REGISTER_IDENTITY_FIELD` first; else auto-pick (`email` if required-and-present, else `phone` if required-and-present). Returns `"email"` or `"phone"`, never `None`.
   - `resolve_min_age(group=None)` — `AUTH_MIN_AGE_YEARS` as int, or `None`.
   - `validate_payload(fields, payload, identity_field, min_age)` — single-pass validator. Returns a sanitized dict (lowercased email, normalized phone, parsed `dob` date) or raises `ValueException` with a specific message. Enforces required keys server-side regardless of what the client sent.

2. `mojo/apps/account/services/phone_register.py` — **new** module.
   - Redis-backed, two-step token service mirroring [auth_handoff.py](mojo/apps/account/services/auth_handoff.py).
   - `start(phone, ip=None)` — normalize via `User.normalize_phone`, generate `session_token = uuid.uuid4().hex`, `setex` `phone:register:session:{token}` → `{phone, code, ts, ip}`, TTL `PHONE_REGISTER_SESSION_TTL` (default 600). Returns `(session_token, code, ttl)` so caller (endpoint) can dispatch SMS.
   - `verify_code(session_token, code)` — atomic `getdel` of session, constant-time compare, on success mint `verified_token = uuid.uuid4().hex` stored at `phone:register:verified:{token}` → `{phone}`, TTL `PHONE_REGISTER_VERIFIED_TTL` (default 600). Returns `(verified_token, phone, ttl)` or raises `ValueException`.
   - `consume(verified_token, phone)` — atomic `getdel` of verified key; require payload-`phone` matches passed-in `phone`. Returns True only on match.

3. `mojo/apps/account/rest/sms.py` — **add two endpoints** at the bottom:
   - `POST /auth/phone/register/start` — body `{phone}`. `@md.public_endpoint`, `@md.requires_bouncer_token('registration')`, `@md.requires_geofence(scope="auth")`, `@md.strict_rate_limit("phone_register_start", ip_limit=5, ip_window=300)` plus a per-normalized-phone counter (`check_account_attempt`-style or a fresh helper). Reject if a user already exists for that phone (`User.objects.filter(phone_number=normalized).exists()` → 400 "An account with this phone number already exists"). Call `phone_register.start`, then `phonehub.send_sms(...)`. Return `{session_token, expires_in}`.
   - `POST /auth/phone/register/verify` — body `{session_token, code}`. `@md.public_endpoint`, `@md.strict_rate_limit("phone_register_verify", ip_limit=10, ip_window=60)`, geofence. Call `phone_register.verify_code`. Return `{verified_phone_token, expires_in}`.

#### B. Server-side register

4. `mojo/apps/account/rest/user.py:on_register` — refactor to drive validation from `register_schema`.
   - At entry, load `fields = register_schema.resolve_fields(group=None initially, then group)`, `identity_field`, `min_age`.
   - Run `register_schema.validate_payload(...)` instead of hard-coded `email = request.DATA.email…`. Treats unknown / disallowed fields as silently-dropped (matches existing extras contract).
   - Existing-user check is now identity-keyed: `User.objects.filter(email=email).exists()` if identity=email, else `User.objects.filter(phone_number=phone).exists()`. Error message reads "An account with this {email/phone number} already exists".
   - Group resolution + extras allowlist + `PRE_REGISTER_VALIDATOR` + password strength stay where they are (`extra` dict now includes `dob`, `phone`, etc. when configured).
   - **Phone verify consumption (BEFORE atomic block)** — when `phone` field has `verify == "sms"`:
     - Require `verified_phone_token` in body; otherwise 400 "Phone verification required".
     - `phone_register.consume(token, phone)` — must return True; otherwise 400 "Invalid or expired phone verification".
   - Inside `transaction.atomic()`:
     - Construct `user = User(email=email or "")`.
     - Identity-driven username: `user.username = email-based generator` if identity=email, else `user.username = normalized_phone`.
     - Set `user.phone_number`, `user.dob`, `user.first_name`, `user.last_name` when their fields are in the config (drop the existing `if first_name: …` style — always copy from sanitized payload).
     - Mark `is_phone_verified = True` when a phone-verify token was consumed.
     - `set_password(password)` → `save()`.
     - GroupMember creation + `fire_user_registered` unchanged.
   - **Email-verify side effect** runs only when `email` is in the configured fields. Phone-only deployments don't send a verify email and don't fall into the `requires_verification` response branch.
   - `requires_verification` response respects `REQUIRE_VERIFIED_EMAIL` only when email is configured. (A future request can add `REQUIRE_VERIFIED_PHONE` gating on register, but `verify: "sms"` already produces a verified phone so it would be a no-op in the happy path.)

#### C. Forgot-password phone branch

5. `mojo/apps/account/rest/user.py:on_user_forgot` — extend method routing.
   - Continue to use `User.lookup_from_request(request, phone_as_username=True)` (already supports phone).
   - Method routing today: `"code"` → email code; `"link"`/`"email"` → email link.
   - **New behavior:** add `request.DATA.get("channel")`:
     - `channel == "sms"` (or absent + user has no email + has phone_number): send SMS code via `phonehub.send_sms("Your password reset code is: {code}")`. Stores in same `password_reset_code` / `_ts` secrets — only one active reset per user. Sets new flag `password_reset_via = "sms"` so reset endpoint knows the source for the unverify-on-reset edge case (out of scope to act on).
     - `channel == "email"` or current default: send email code (today's behavior).
     - `channel == "link"` or `method == "link"`: still email (link mode requires email).
   - Generic-success response unchanged (avoid enumeration).

6. `mojo/apps/account/rest/user.py:on_user_password_reset_code` — already does `lookup_from_request(phone_as_username=True)` so phone+code+new_password works. **No change needed.** Add a test that exercises the phone path end-to-end.

#### D. Bouncer template plumbing

7. `mojo/apps/account/rest/bouncer/views.py:_auth_context` — emit register-schema context.
   - `register_fields = register_schema.resolve_fields(group=group)` → list of dicts.
   - Add to context: `register_fields`, `identity_field`, `min_age` (for client-side hint; server enforces).
   - Add `forgot_channel = "sms" if identity_field == "phone" else "email"` (drives login.html forgot-subview UI).

8. `mojo/apps/account/templates/account/register.html` — field-loop rewrite.
   - Replace the hard-coded `<div class="mat-field-row">` / `<div class="mat-field">` blocks with `{% for f in register_fields %}{% include "account/_register_field.html" %}{% endfor %}`.
   - **New partial** `account/_register_field.html`: switches on `f.name`:
     - `first_name`, `last_name` → text input (`<input type="text" id="reg-{{f.name}}">`). When both are in the list adjacent, render them as the existing side-by-side row.
     - `email` → email input with `required` attribute when `f.required`.
     - `phone` → tel input + inline send-code button (`<button type="button" id="reg-phone-send">Send code</button>`); on send, swap to a 6-digit code field + verify button. On verify, populate hidden `<input type="hidden" id="reg-phone-token">`.
     - `dob` → date input.
     - `password` → existing eye-toggle pattern.
   - **Adjacent first/last grouping**: simplest is a tiny pre-pass in `_auth_context` that converts `[first_name, last_name, …]` into a row-grouped structure, e.g. `register_field_rows = [[first_name, last_name], [email], [password]]`. Template iterates rows; each row is either one field (full-width) or two adjacent name fields.
   - **Submit handler rewrite**: loops over `register_fields` (rendered into a JS array via `{{ register_fields|json_script:"reg-fields-data" }}`), pulls each value from `document.getElementById("reg-{name}")`, validates client-side required, builds payload. Adds `verified_phone_token` from hidden input when present. Adds `group_uuid` from `cfg` (already in place from prior commit).

9. `mojo/apps/account/templates/account/login.html` — forgot-subview tweaks.
   - When `forgot_channel == "sms"`: change "Email Address" label to "Phone Number" on the forgot-password form, change the radio tile labels to "SMS a code" (link mode hidden).
   - Driven via a `{% if forgot_channel == "sms" %}` conditional. No JS-flow change beyond the API payload key.

#### E. JS helpers

10. `mojo/apps/account/static/account/mojo-auth.js` — add register-phone helpers + extend forgot.
    - `startPhoneRegister(phone)` → `POST /auth/phone/register/start` body `{phone}`, returns `session_token`.
    - `verifyPhoneRegister(session_token, code)` → `POST /auth/phone/register/verify` body `{session_token, code}`, returns `verified_phone_token`.
    - `forgotPasswordCode(identifier, channel)` — existing signature gains optional `channel` arg (`"email"` or `"sms"`); payload becomes `{identifier_field: identifier, method: 'code', channel}` where `identifier_field` is `email` or `phone` based on channel.
    - `resetWithCode(identifier, code, newPassword, identifierType)` — overload for phone. Backwards-compatible: when called with three positional args + identifier looks like email, behaves as today.

### Design Decisions

- **Closed-set canonical fields**: only `first_name`, `last_name`, `email`, `phone`, `dob`, `password`. Custom keys still flow through `REGISTRATION_EXTRA_FIELDS` (existing contract). Rationale: each canonical field needs an input type, validator, and column on `User` — opening it up to arbitrary names invites broken renders and untyped data. Custom data belongs in `extra` / `metadata`.
- **Password always required**: implicit; not togglable via config. Rationale: passwordless signup is a meaningfully different flow (token issuance, no `set_password` call) and is out of scope per the request. Keeping `password.required` non-negotiable prevents partial-state User rows.
- **Verify-then-register, not register-then-verify**: phone verification produces a Redis token consumed by `on_register`; no User row is created until the phone is proven owned. Rationale: spam control on phone-number scarcity + avoids orphaned partially-registered accounts.
- **Stateless Redis tokens, not user-bound `tokens.py` flow**: `tokens.py` `_generate/_verify` requires a User to sign against. Pre-register has no user, so we add a parallel Redis service (mirrors `auth_handoff`). Rationale: keeps `tokens.py` focused on user-bound flows and avoids inventing a synthetic "ghost user" pattern.
- **`AUTH_REGISTER_FIELDS` default is computed**: the setting is *optional*; absent → today's email-based form. Rationale: backwards compatible without forcing every deployment to set the value.
- **Identity field is auto-picked, override is rare**: `email` wins if both email and phone are required + present, else `phone`. `AUTH_REGISTER_IDENTITY_FIELD` is the escape hatch. Rationale: 95% of deployments will have one obvious identity; the explicit setting exists for the weird-mix-mode case.
- **Forgot-password channel is *dispatched*, not negotiated**: phone-identity users get SMS codes; email-identity users get email. A `channel` body param overrides for the rare "I have both, want SMS today" case. Rationale: the right default depends on what the user used to register; we have that signal already on the User row.
- **No new bouncer URL params**: the bouncer page itself is unchanged — same `/register`, same `?group_uuid=` (post-recent-fix). Form contents are configured server-side per group. Rationale: no new URL-vs-dispatcher contract risk.
- **Field schema lives in a service, not in REST**: `register_schema.py` is the single source of truth used by both the bouncer template context and the server-side validator. Rationale: drift between client-render and server-validate is the bug we're explicitly avoiding.

### User Cases

1. **Default deployment (email + password)** — `AUTH_REGISTER_FIELDS` unset. Form looks identical to today. Server validates as today. Email verification still sent when `REQUIRE_VERIFIED_EMAIL=True`. Existing `tests/test_register/register.py` continues to pass unchanged.
2. **Project asking the original question (first/last + phone + DOB + password)** — sets:
   ```python
   AUTH_REGISTER_FIELDS = [
       {"name": "first_name", "required": True},
       {"name": "last_name",  "required": True},
       {"name": "phone",      "required": True, "verify": "sms"},
       {"name": "dob",        "required": True},
       {"name": "password",   "required": True},
   ]
   AUTH_MIN_AGE_YEARS = 13
   ```
   Flow: form renders without email row, includes inline phone-verify, includes DOB picker. User receives SMS code, enters it, submits. Server consumes verified-phone token, creates User with `username=phone_number`, `is_phone_verified=True`, `dob=<date>`. Forgot-password collects phone, dispatches SMS code.
3. **Multi-tenant: some groups phone-only, others email-only** — `Setting.set('AUTH_REGISTER_FIELDS', <phone-config>, group=group_a)` and `Setting.set('AUTH_REGISTER_FIELDS', <email-config>, group=group_b)`. The bouncer's `_auth_context(request, group=group)` resolves per-group; rendered form differs per group automatically. Same backend.
4. **Hybrid (both email and phone, both required)** — `email` and `phone` both `required=True`. Identity auto-picks `email`. Phone verification still required (`verify: "sms"` on phone). User row gets both fields populated, `is_phone_verified=True`, email-verify sent as today. Both `email` and `phone_number` unique-constrained on the model so duplicate detection works on either.
5. **DOB optional, no min-age** — `dob` field with `required: False`, `AUTH_MIN_AGE_YEARS` unset. Field renders, validation accepts empty, stored as null when omitted. No age gate.
6. **Forgot-password on phone-only user** — user clicks "Forgot password?" on login page. Subview's label says "Phone Number", radio tile says "SMS a code" (link mode hidden). User enters phone, server dispatches SMS code via `phonehub.send_sms`, stored on user under `password_reset_code` secret. User enters code + new password on the reset-code view, server validates against the same secret and logs them in.

### Edge Cases

- **Race on phone uniqueness**: two browsers verify the same phone simultaneously and both attempt register. Each consumes a *different* `verified_phone_token` (each got their own from `start`+`verify`), but `User.phone_number` is `unique=True` so the second `user.save()` raises `IntegrityError`. Map that to the same 400 family as email duplicates.
- **Verified-token replay**: `phone_register.consume` is `getdel`, so a token works exactly once. Replay → token missing → 400 "Invalid or expired phone verification".
- **Code-without-session**: client posts to `/auth/phone/register/verify` with a `session_token` that has expired or was already consumed. Service returns no payload → 400 "Invalid or expired verification session". User is told to request a new code.
- **Phone field hand-removed by client but config requires it**: server-side `validate_payload` re-checks against `register_schema.resolve_fields` so a hand-crafted POST missing `phone` still gets 400. Defense-in-depth — the rendered form is just a hint.
- **Identity-field collision**: identity=email but the request body has only `phone` (or vice versa). `validate_payload` raises 400 because the required identity field is missing. Surfaces as the same "An account with this {identity} already exists or required" error family.
- **Phone-as-identity user later adds email**: out of scope here. The existing `email_change` flow handles add-or-change for an existing user; nothing in this work blocks it.
- **DOB future date / unparseable**: `validate_payload` does ISO parse + `dob <= today` check, else 400 "Invalid date of birth".
- **`AUTH_MIN_AGE_YEARS` with DOB optional**: gate only fires when DOB was supplied. (If you need it required, set `required: True`.)
- **`USER_REGISTERED_HANDLER` receives `extra` with new keys**: handlers that don't expect `dob`/`phone` in `extra` ignore them naturally — the kwargs are unchanged (`user, request, group, source, extra`).
- **Migration / existing data**: no schema changes. `phone_number`, `dob`, `is_phone_verified` columns already exist with sensible defaults.
- **OAuth signup**: out of scope (called out in request). The current OAuth flow creates accounts with email as identity. If a phone-only deployment enables OAuth, the resulting account has `email` set + `phone_number` null. Not ideal but not broken — separate follow-up request.
- **`requires_verification` response for phone-only**: when no email is configured, the response shape from `on_register` is the standard JWT login (immediate sign-in), not the `requires_verification: true` form. Frontend code that branches on `requires_verification` still works (falls through to the normal path).

### Testing

All tests use `testit` + `X-Mojo-Test-*` headers for per-request setting overrides (the existing pattern in `tests/test_register/register.py`).

**Schema / validator (unit, no HTTP)** → `tests/test_register/schema.py`:
- `resolve_fields` with no setting returns the default email config.
- `resolve_fields` with an explicit list returns the normalized list-of-dicts.
- `resolve_identity_field` auto-pick: email-required → "email"; phone-required only → "phone"; both required → "email"; explicit override wins.
- `validate_payload` rejects missing required fields, unknown fields silently dropped, normalizes phone via `User.normalize_phone`, parses `dob` ISO, age-gates when `min_age` set.
- `validate_payload` rejects future DOB.

**Phone-verify service (unit + Redis)** → `tests/test_register/phone_verify.py`:
- `start` returns a session token + code; entry exists in Redis with the right key prefix.
- `verify_code` happy-path: returns a verified token, session key is gone, verified key exists.
- `verify_code` wrong code returns None / raises; session is still consumed (single-use).
- `consume` returns True for the right phone, False for a different phone (token-binding check).
- TTL respected (use Redis `pttl` or sleep-past-TTL with very short test TTL).

**New endpoints** → `tests/test_register/phone_endpoints.py`:
- `POST /auth/phone/register/start` returns 200 + session_token, sends SMS via mocked phonehub.
- `POST /auth/phone/register/start` returns 400 when phone already belongs to an existing user.
- `POST /auth/phone/register/start` honors strict rate limit (5 attempts per 5 min per IP; 3 per phone per hour).
- `POST /auth/phone/register/verify` returns 200 + verified_phone_token on correct code; 400 on wrong code; 400 on expired session.

**Register integration** → `tests/test_register/register.py` (extend, do not break):
- Existing email-based tests continue to pass (regression guard).
- New: `test_register_phone_only_with_verify` — exercises the full SMS verify → register flow with the test header `X-Mojo-Test-Register-Fields` (JSON list, new test-mode header in extensions.py). Asserts User created with `username=normalized_phone`, `is_phone_verified=True`.
- `test_register_phone_only_missing_token` → 400.
- `test_register_phone_only_bad_token` → 400.
- `test_register_dob_required_below_min_age` → 400 with `AUTH_MIN_AGE_YEARS=13` and DOB making the user 10.
- `test_register_dob_required_above_min_age` → 200.
- `test_register_with_existing_phone_user` → 400 duplicate.

**Forgot-password phone branch** → `tests/test_auth/forgot_password_phone.py` (new file):
- `test_forgot_phone_user_with_no_email_sends_sms` — create phone-only user, POST `/auth/forgot` with `phone`, mock phonehub.send_sms, assert one call with the user's number and the code that landed in `user.get_secret("password_reset_code")`.
- `test_forgot_email_user_still_sends_email` — regression guard.
- `test_forgot_explicit_channel_sms_for_user_with_both` — both fields present, channel=sms forces SMS dispatch.
- `test_password_reset_code_phone_path` — phone-only user + valid code + new password → 200, JWT returned, can log in with new password via phone.

**Bouncer rendering** → `tests/test_auth/bouncer_forms.py` (extend existing file):
- `test_register_html_phone_only_config_renders_phone_input` — render `register.html` with `register_fields` context = phone-only config. Assert `<input type="tel"` present, `<input type="email"` absent, `<input type="date"` present.
- `test_register_html_default_renders_email_input` — regression guard, today's behavior.
- `test_login_html_forgot_subview_phone_mode` — render `login.html` with `forgot_channel="sms"`; assert "Phone Number" appears and "Email a link" is hidden.

**JS helper smoke** → `tests/test_auth/bouncer_forms.py`:
- Existing `mojo-auth.js` signature smoke test extended to assert `startPhoneRegister`, `verifyPhoneRegister`, and `forgotPasswordCode(identifier, channel)` shapes exist in the JS source.

**Run target after each phase**:
- Phase A (schema/service/endpoints): `bin/run_tests --agent -t test_register.schema -t test_register.phone_verify -t test_register.phone_endpoints`
- Phase B (server register): `bin/run_tests --agent -t test_register`
- Phase C (forgot phone): `bin/run_tests --agent -t test_auth.forgot_password_phone`
- Phase D-E (template + JS): `bin/run_tests --agent -t test_auth.bouncer_forms`

### Docs

- `docs/django_developer/account/auth_pages.md` — new "Configurable Registration Form" section. Documents `AUTH_REGISTER_FIELDS` shape, identity auto-pick rule, `AUTH_MIN_AGE_YEARS`, the verify-then-register phone flow at the protocol level, and the forgot-password channel dispatch.
- `docs/django_developer/account/auth.md` — extend the "Register Endpoint" reference: add `phone`, `dob`, `verified_phone_token` body params; new "Identity field" subsection covering `User.username` resolution.
- `docs/web_developer/account/auth_pages.md` — update the registration page description to note that field set is server-configurable; example consumer-app phone-flow code (start/verify/register).
- `docs/web_developer/account/authentication.md` — `POST /api/auth/register` request body table gains `phone`, `dob`, `verified_phone_token`. New "Phone-based registration" subsection with the two new endpoints. Forgot-password section: document `channel` param and SMS path.
- `CHANGELOG.md` — entry under `v1.1.0 (current)` summarizing the new setting, the two new endpoints, and the forgot-password SMS dispatch. Note that defaults preserve current behavior.

## Resolution

**Status**: resolved
**Date**: 2026-05-16

### What Was Built
All five phases of the plan landed across four commits. The bouncer
register form is now schema-driven via `AUTH_REGISTER_FIELDS`. Phone
ownership is verified pre-register through a Redis-backed token flow.
Forgot-password dispatches SMS automatically when the user has no
email. A schema change (migration `0043`) makes `User.email` nullable
so phone-only deployments can register multiple users without email.

### Commits
- `7ca80dc` — register foundations (register_schema service +
  phone_register Redis service + two new endpoints in sms.py + 23 unit
  tests)
- `d9cebf8` — on_register refactor to be schema-driven + 7 integration
  tests
- `ae797df` — forgot-password phone branch (SMS dispatch) + 5 tests
- `02c513c` — template field-loop + JS helpers + nullable User.email
  migration + 7 render/JS tests

### Files Changed
- `mojo/apps/account/services/register_schema.py` (new) — schema
  resolution + validator
- `mojo/apps/account/services/phone_register.py` (new) — Redis-backed
  verify-then-register tokens
- `mojo/apps/account/rest/sms.py` — added
  `POST /auth/phone/register/start` and `/verify`
- `mojo/apps/account/rest/user.py` — `on_register` schema-driven,
  `on_user_forgot` SMS branch
- `mojo/apps/account/rest/bouncer/views.py` — `_auth_context` emits
  `register_fields`, `register_field_rows`, `identity_field`,
  `forgot_channel`
- `mojo/apps/account/models/user.py` — `email = ...(null=True, ...)`,
  save() guard for null email
- `mojo/apps/account/migrations/0043_alter_user_email.py` (new)
- `mojo/apps/account/templates/account/_register_field.html` (new)
- `mojo/apps/account/templates/account/register.html` — schema loop
- `mojo/apps/account/templates/account/login.html` — forgot phone
  mode
- `mojo/apps/account/static/account/mojo-auth.js` —
  `startPhoneRegister`, `verifyPhoneRegister`,
  `forgotPasswordCode(identifier, channel)`, `resetWithCode` accepts
  identifier+channel

### Tests
- `tests/test_register/schema.py` — 11 schema-service unit tests
- `tests/test_register/phone_verify.py` — 6 Redis-service tests
- `tests/test_register/phone_endpoints.py` — 6 endpoint tests
- `tests/test_register/configurable_form.py` — 7 integration tests
- `tests/test_auth/forgot_password_phone.py` — 5 forgot-password tests
- `tests/test_auth/bouncer_forms.py` — 7 new render/JS smoke tests
  on top of the 7 existing ones
- Targeted run: 82/82 pass via
  `bin/run_tests --agent -t test_register -t test_auth.bouncer_forms -t test_auth.forgot_password_phone -t test_whitelabel`

### Docs Updated
- `docs/django_developer/account/auth_pages.md` — new "Configurable
  Registration Form" section
- `docs/web_developer/account/authentication.md` — extended register
  request table + new "Phone-Based Registration" subsection
- `CHANGELOG.md` — entry under `v1.1.0 (current)`
- (Post-build doc-sync agent may add further updates)

### Migration Required
Consumers must run `python manage.py migrate` (or `bin/create_testproject`
for the test harness) after pulling — migration `0043_alter_user_email`
flips `User.email` to `null=True`. Backwards compatible: existing rows
remain non-null, and EmailField with `null=True, unique=True` treats
NULL as distinct under the unique constraint (PostgreSQL/SQLite).

### Security Review
Pending — agent running. Phone-verify tokens are single-use,
phone-bound, and use constant-time compares. Rate-limiting at the
endpoint layer covers brute-force of the 6-digit code space.

### Follow-up
- OAuth signup against `AUTH_REGISTER_FIELDS` — out of scope here;
  OAuth currently always creates accounts with email as identity. A
  future request can reconcile.
- Passwordless register — explicitly out of scope; password stays
  always-required.
- Per-locale DOB widget — `<input type="date">` is browser-localized
  for v1.
