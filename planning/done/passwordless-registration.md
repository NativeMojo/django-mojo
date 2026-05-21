# Passwordless Registration (Phone + SMS Code)

**Type**: request
**Status**: resolved
**Date**: 2026-05-21
**Priority**: medium

## Description

Allow a group to register users **without a password**. When a group's auth
config is set up for phone-based, SMS-verified signup, the registration form
should not collect a password at all — the account is created with an unusable
password, and the user logs in afterward with an **SMS code** (and may
additionally enrol a passkey).

Today registration hard-requires a password in three places, so a user of a
passkey/SMS-only app is still forced to create a password they will never use.
This request closes that gap.

**Product rule:** a passwordless account **must have a verified phone**.
No-password registration is allowed **only** for phone-identity signups where
the phone is SMS-verified, so the account always has a working login path
(the SMS code). Email-only passwordless registration (magic-link-only
accounts) is explicitly out of scope.

## Context

The auth-config feature (shipped — see `planning/done/auth-portal-config.md`)
lets a group restrict login to SMS code / passkey only. But registration still
forces a password. The original feature deliberately scoped passwordless
registration out — `register_schema.py` even carries the comment *"Password is
always required — passwordless register is a separate flow and out of scope."*
Now that login can be SMS/passkey-only, that scoping is the wrong default and
creates exactly the friction this request removes.

The model layer already supports it: `User.set_unusable_password()` exists and
OAuth registration (`rest/oauth.py`) already creates passwordless users. The
SMS-code login path (`/api/auth/sms/login` → `/api/auth/sms/verify`) already
works for any phone-verified user. The phone verify-then-register flow
(`verified_phone_token`) already proves phone ownership at signup. This request
mostly removes the artificial "password always required" constraint and adds a
validation guard so a passwordless config cannot produce an unloginnable
account.

## Acceptance Criteria

- A `registration.fields` schema may omit `password` entirely.
- When `password` is omitted, the schema **must** include `phone` with
  `verify: "sms"`, and phone must be the identity field. `validate_auth_config`
  / `register_schema.validate_fields_config` rejects a passwordless schema that
  lacks an SMS-verified phone, with a clear error message.
- `on_register` accepts a request with no `password` when the resolved schema
  is passwordless, and creates the user via `User.set_unusable_password()`.
- A passwordless register still consumes the `verified_phone_token` and sets
  `is_phone_verified = True` (existing phone-verify flow, unchanged).
- After a passwordless register, the user can log in via
  `POST /api/auth/sms/login` → `POST /api/auth/sms/verify`.
- The password strength check is skipped when there is no password.
- `@md.requires_params("password")` is removed from `on_register`; password
  presence is validated against the resolved schema instead.
- The hosted `/register` page renders no password field for a passwordless
  schema (it is already schema-driven — verify the submit handler tolerates an
  absent password input).
- The default schema (email + password) and every existing password-based
  registration flow are unchanged (regression-safe).
- A passwordless account may still enrol a passkey via `registration.passkey_prompt`
  (additive — no special handling needed).

## Investigation

**What exists**

- `rest/user.py` `on_register`:
  - decorated `@md.requires_params("password")`.
  - `password = sanitized["password"]` (hard key access).
  - `User(email=...).check_password_strength(password)`.
  - `user.set_password(password)` inside the atomic block.
  - Phone-verify: when the schema marks `phone` with `verify="sms"`, a
    `verified_phone_token` is consumed before the atomic block and
    `is_phone_verified = True` is set.
  - Phone-identity username generation already exists
    (`generate_username_from_names(fallback=phone)`).
- `services/register_schema.py`:
  - `_normalize_entry` force-sets `password` to `required=True`.
  - `_normalize_field_list` appends a required `password` field when the schema
    omits it.
  - `validate_payload` raises `"password is required"` unconditionally and
    always writes `out["password"]`.
  - `validate_fields_config` requires `email` or `phone`.
  - `DEFAULT_FIELDS` includes a required `password` (the default stays
    password-based).
- `User.set_unusable_password()` exists; `rest/oauth.py` `_find_or_create_user`
  already uses it for OAuth-created accounts.
- `rest/sms.py` `on_sms_login` / `on_sms_verify` — passwordless SMS-code login
  already works for a phone-verified user; the standalone verify path issues a
  JWT via `jwt_login(source="sms")`.

**What changes**

- `services/register_schema.py`:
  - `_normalize_entry` — stop forcing `password` to required (a configured
    `password` entry keeps its own `required` flag; password is never
    auto-promoted).
  - `_normalize_field_list` — do **not** append a `password` field when it is
    absent. A schema without `password` stays passwordless.
  - `validate_payload` — require/validate `password` only when `password` is in
    the field set; otherwise leave it out of the sanitized dict.
  - `validate_fields_config` — when `password` is absent from the normalized
    fields, require that `phone` is present with `verify == "sms"`; raise a
    clear `ValueException` otherwise.
- `rest/user.py` `on_register`:
  - drop `@md.requires_params("password")`.
  - derive `has_password = "password" in by_name`.
  - `password = sanitized.get("password")`.
  - if `has_password`: run `check_password_strength` + `user.set_password(...)`
    as today; else `user.set_unusable_password()`.
  - all other logic (identity resolution, phone-verify consumption,
    GroupMember, `USER_REGISTERED_HANDLER`, email-verify side effects,
    `jwt_login`) unchanged.
- Hosted `register.html` — already schema-driven; confirm the submit handler's
  `REG_FIELDS` loop and any password-specific JS tolerate an absent password
  input. No password field renders for a passwordless schema.
- Docs — `docs/django_developer/account/auth_config.md` and `auth_pages.md`
  (both tracks): document passwordless registration, the phone + `verify:"sms"`
  requirement, and that the login path is the SMS code.

**Constraints**

- A passwordless account with no verified phone and no passkey would be
  unloginnable — hence the SMS-verified-phone requirement, enforced at
  config-validation time (`validate_fields_config`), not just at register time.
- Email-only passwordless registration (magic-link-only accounts) is out of
  scope — passwordless requires a phone identity.
- Backward compatibility: the default schema and any schema that includes
  `password` are completely unchanged. Only a schema that explicitly omits
  `password` triggers the new path.
- Remove the now-inaccurate `register_schema` comment that calls passwordless
  register "a separate flow and out of scope".
- No model or schema migration — `User` already supports unusable passwords.

**Related files**

- `mojo/apps/account/rest/user.py` (`on_register`)
- `mojo/apps/account/services/register_schema.py`
- `mojo/apps/account/services/auth_config.py` (`validate_auth_config` →
  `register_schema.validate_fields_config`)
- `mojo/apps/account/templates/account/register.html`
- `mojo/apps/account/rest/sms.py` (login path — verification only, no change
  expected)
- `docs/django_developer/account/auth_config.md`,
  `docs/django_developer/account/auth_pages.md`,
  `docs/web_developer/account/auth_config.md`,
  `docs/web_developer/account/auth_pages.md`

## Endpoints

No new endpoints. Behavior change only:

| Method | Path | Change |
|---|---|---|
| POST | `/api/auth/register` | `password` no longer mandatory; omitted when the resolved schema is passwordless. Creates the user with an unusable password. |

## Tests Required

- `validate_fields_config` / `validate_auth_config` accepts a passwordless
  schema (phone + `verify:"sms"`, no password).
- `validate_fields_config` rejects a passwordless schema with no phone, or a
  phone without `verify:"sms"`.
- `on_register` with a passwordless schema + a valid `verified_phone_token`
  creates a user with `has_usable_password() is False` and
  `is_phone_verified is True`.
- `on_register` with a passwordless schema and no phone / no token is rejected
  and creates no user.
- The created passwordless user completes `/api/auth/sms/login` →
  `/api/auth/sms/verify` and receives a JWT.
- Default email + password registration still succeeds (regression).
- `register.html` renders no password input for a passwordless schema and the
  default schema still renders one.

## Out of Scope

- Email-only passwordless registration / magic-link-only accounts.
- Changes to OAuth registration (already passwordless via
  `set_unusable_password()`).
- The web-mojo Auth Config editor UI — tracked in the web-mojo request
  `groupview-auth-config-editor.md`. **Note:** that request currently assumes
  the `registration.fields` password row is "always included + required —
  render locked"; once this ships, the password row becomes optional and that
  note must be updated.

## Plan

**Status**: planned
**Planned**: 2026-05-21

### Objective
Let `registration.fields` omit `password` to create passwordless accounts —
permitted only when the schema has an SMS-verified phone, so the account always
has a working login path (the SMS code).

### Steps
1. `mojo/apps/account/services/register_schema.py`
   - `_normalize_field_list` — remove the block (lines ~106-108) that appends a
     `password` field when absent. A schema without `password` stays passwordless.
   - `_normalize_entry` — keep forcing a *present* `password` entry to
     `required=True, verify=None` (no "optional password" state); update the
     stale "passwordless register is a separate flow and out of scope" comment.
   - `validate_payload` — gate the password block (lines ~228-232) on
     `"password" in by_name`: require + emit `out["password"]` only then.
   - `validate_fields_config` — after the email/phone check, when `password` is
     absent from the normalized fields, require `phone` present with
     `verify == "sms"`; else raise a clear `ValueException`.
   - Update the module docstring (drop "forces password to required").
2. `mojo/apps/account/rest/user.py` — `on_register`
   - Remove the `@md.requires_params("password")` decorator.
   - After `by_name` is built (line ~311): defensive guard — if
     `"password" not in by_name` and the schema lacks `phone` with
     `verify=="sms"`, raise `ValueException` (the global `AUTH_CONFIG` setting
     and the `X-Mojo-Test-Register-Fields` header both bypass `validate_auth_config`).
   - `password = sanitized.get("password")`; `has_password = "password" in by_name`.
   - Run `check_password_strength` only when `has_password`.
   - Atomic block: `user.set_password(password)` when `has_password`, else
     `user.set_unusable_password()`.
   - Update the docstring (password no longer unconditionally required).
3. `mojo/apps/account/templates/account/register.html` +
   `templates/account/_register_field.html` — verify only; the form is
   schema-driven (`REG_FIELDS` loop), so a passwordless schema renders no
   password field. No change expected; remove any hardcoded password reference
   if found.
4. Docs — see Docs section.

No new endpoints, no model/schema change, no migration.

### Design Decisions
- **Two-state password, no "optional" half-state**: `password` is either in
  the schema (→ required) or absent (→ passwordless). `_normalize_entry` keeps
  forcing `required=True` when present; only the auto-append is removed. Simpler
  mental model than honoring a per-entry `required:false`.
- **Double guard**: the phone+`verify:"sms"` requirement is enforced at
  config-write time (`validate_fields_config`, via `Group.on_rest_pre_save`)
  *and* defensively in `on_register` — because the deployment-wide `AUTH_CONFIG`
  setting and the test header never pass through `validate_auth_config`.
- **`set_unusable_password()`** — reuse the existing model method (OAuth
  registration already creates passwordless users this way); no new concept.
- **Phone-only**: passwordless requires a phone identity; email-only
  passwordless (magic-link-only) stays out of scope — SMS code is the
  guaranteed login path.

### User Cases
- Default single-tenant (email + password) — unchanged; `DEFAULT_FIELDS` keeps
  password.
- Group with phone+SMS schema *with* password — unchanged; password required.
- Group with phone+SMS schema *without* password — passwordless: register
  collects verified phone (+ optional names/dob), account created with an
  unusable password, JWT returned; subsequent logins via SMS code.
- Passwordless group also setting `passkey_prompt: required` — after signup the
  user is sent to `/passkey` to also enrol a passkey (additive, already works).
- Ops sets a bad passwordless `AUTH_CONFIG` (no SMS phone) — `on_register`
  defensive guard rejects the signup with a clear error.

### Edge Cases
- Passwordless schema lacking an SMS-verified phone reaches `on_register` (via
  test header / unvalidated `AUTH_CONFIG`) — defensive guard rejects it.
- `set_unusable_password()` makes `check_password()` always fail — password
  login is genuinely impossible for these accounts, as intended.
- `REQUIRE_VERIFIED_EMAIL` never triggers for passwordless (no email in schema)
  — `on_register` returns a JWT immediately, same as today's phone registers.
- `request.DATA.pop("password", None)` already tolerates an absent password.
- Duplicate check + username generation already have a phone-identity path.

### Testing
- `validate_fields_config` accepts a passwordless schema (phone+`verify:sms`,
  no password) → `tests/test_register/passwordless.py`
- `validate_fields_config` rejects passwordless schema with no phone, and with
  phone but `verify != "sms"` → `tests/test_register/passwordless.py`
- `_normalize_field_list` no longer appends `password` for a passwordless raw
  config → `tests/test_register/passwordless.py`
- `validate_payload` does not require `password` when it is absent from the
  field set → `tests/test_register/passwordless.py`
- `on_register` with a passwordless schema + valid `verified_phone_token`
  creates a user with `has_usable_password() is False` and
  `is_phone_verified is True` → `tests/test_register/passwordless.py`
- `on_register` with a passwordless schema and no phone-verify token is
  rejected, no user created → `tests/test_register/passwordless.py`
- `on_register` defensive guard rejects a passwordless schema with no
  SMS-verified phone (set via `X-Mojo-Test-Register-Fields`)
  → `tests/test_register/passwordless.py`
- Full round-trip: passwordless register → `/api/auth/sms/login` →
  `/api/auth/sms/verify` issues a JWT → `tests/test_register/passwordless.py`
- Regression: default email + password registration still succeeds
  → `tests/test_register/passwordless.py` (or existing `configurable_form.py`)
- `register.html` renders no password input for a passwordless schema
  → `tests/test_register/passwordless.py` (template-render assertion)

### Docs
- `docs/django_developer/account/auth_config.md` — `registration.fields`
  section: omitting `password` makes registration passwordless; requires
  `phone` with `verify:"sms"`; login is via SMS code.
- `docs/django_developer/account/auth_pages.md` — registration section note.
- `docs/web_developer/account/auth_config.md` +
  `docs/web_developer/account/auth_pages.md` — consumer-facing: passwordless
  registration and SMS-code login.
- `CHANGELOG.md` — entry under the current version.
- Follow-up (not this build): web-mojo `GroupAuthConfigSection.js` locks the
  password row in its registration-fields grid — needs a change to allow
  excluding `password` (only when phone+`verify:sms` is configured).

## Resolution

**Status**: resolved
**Date**: 2026-05-21

### What Was Built
A registration field schema may now omit `password`. When it does, signup is
passwordless: the account is created with `User.set_unusable_password()` and
the user logs in by SMS code. Permitted only when the schema includes a
`phone` field with `verify: "sms"`, so a passwordless account always has a
working login path. The default email + password schema is unchanged.

### Files Changed
- `mojo/apps/account/services/register_schema.py` — `_normalize_field_list`
  no longer auto-appends a `password` field; `_normalize_entry` still forces a
  *present* `password` to required (no optional-password state);
  `validate_payload` requires `password` only when it is in the schema;
  `validate_fields_config` rejects a no-password schema lacking a phone with
  `verify="sms"`.
- `mojo/apps/account/rest/user.py` — `on_register`: dropped
  `@md.requires_params("password")`; added a defensive passwordless guard
  after schema resolution; skips the strength check and calls
  `set_unusable_password()` when no password is supplied.
- `tests/test_register/passwordless.py` *(new)* — passwordless test coverage.
- `tests/test_register/schema.py` — updated the outdated
  `resolve_fields`-auto-appends-password test to the new contract.

### Tests
- `tests/test_register/passwordless.py` — `validate_fields_config` accept/reject
  cases, `_normalize_field_list` no auto-append, `validate_payload` no-password
  path, `on_register` passwordless creation (unusable password + verified
  phone), missing-token rejection, the defensive guard, the full passwordless
  register → SMS-code login round-trip, and a default-schema regression.
- Run: `bin/run_tests -t test_register` — 80/80 pass.
- Full suite: 2140 passed, 2 failed (pre-existing, unrelated `test_jobs`
  scheduled-task tests), 56 skipped.

### Docs Updated
- `docs/django_developer/account/auth_config.md` — new "Passwordless
  Registration" section (guard, `set_unusable_password`, SMS login path).
- `docs/django_developer/account/auth_pages.md` — registration section +
  passwordless `AUTH_CONFIG` example.
- `docs/web_developer/account/auth_config.md`,
  `docs/web_developer/account/auth_pages.md` — consumer-facing passwordless
  registration + SMS-code login flow.
- `CHANGELOG.md` — v1.2.21 bullet.

### Security Review
No concerns. Phone ownership is still proven by the single-use, constant-time
`verified_phone_token` (a token for phone A cannot register phone B); the
`on_register` defensive guard fires before any user row is created;
`set_unusable_password()` genuinely blocks password login (no blank-password
bypass); default password registration is unweakened; the
`X-Mojo-Test-Register-Fields` header remains test-mode gated.

### Follow-up
- web-mojo `GroupAuthConfigSection.js` locks the password row in its
  registration-fields grid — a follow-up there should allow excluding
  `password` (only when phone + `verify:"sms"` is configured) so the new
  capability is reachable from the admin UI.
