# Register Endpoint Extensibility (Group Context + Signal Hooks + Custom Fields)

**Type**: request
**Status**: resolved
**Date**: 2026-05-15
**Resolved**: 2026-05-15
**Priority**: high

## Description

Extend the password-based registration endpoint (`POST /api/auth/register`)
so consumer apps built on django-mojo can:

1. Register a user directly into a specific `account.Group` (operator /
   tenant) in a single request, with `GroupMember` auto-creation.
2. React to registrations via a new `user_registered` signal — the
   canonical hook for per-tenant bootstrap work (satellite records, async
   provisioning of external services, initial entitlements, etc.).
3. Accept additional registration fields beyond the current hardcoded
   `email`/`password`/`first_name`/`last_name` set, governed by an
   allowlist setting so the endpoint stays opinionated about what it
   accepts.
4. Override the rendered `register.html` form to collect those additional
   fields without duplicating the whole template.

The OAuth registration path (`mojo/apps/account/rest/oauth.py`) already
auto-creates `GroupMember` records in `_find_or_create_user`. This request
brings the same group-aware pattern to the password-based flow and adds
the missing extension points so downstream projects can attach their own
logic without forking the endpoint.

## Context

Consumer apps using django-mojo as a multi-tenant framework hit the same
wall today: a user registers, the framework creates a `User`, and then
the consumer needs to (a) attach that user to a specific operator group,
(b) create per-tenant satellite records, (c) bootstrap resources in
external systems, and (d) apply registration-time context (referral
codes, promo codes, acquisition channel) that arrived as additional
form fields.

The only workarounds today are:

- Wrap the framework's register endpoint with a custom view that calls
  it as a subroutine — duplicates rate limiting, bouncer-token validation,
  password-strength checks, MFA gating, email-verify flow, etc.
- Listen on `post_save` for `User` — doesn't carry request context (IP,
  group, extra form fields) and fires for every user mutation, not only
  registration.

A dedicated `user_registered` signal plus a `group` body param plus an
extras allowlist lets consumer apps stay declarative and out of the
framework's view code, while preserving every existing protection.

## Acceptance Criteria

- `POST /api/auth/register` accepts an optional `group` UUID body param.
  Valid + active Group → user is auto-added as a `GroupMember`. Missing
  Group → 400, no user created. Absent → existing behavior unchanged.
- A new `user_registered` signal is fired after the user (and
  GroupMember, if any) is created, before `jwt_login()` runs.
- Extra body params whose keys are listed in `REGISTRATION_EXTRA_FIELDS`
  are passed verbatim to the signal as `extra={...}`. Extras not in the
  allowlist are silently dropped (no 400; forward-compat for UIs that
  send extra context).
- The endpoint runs in `transaction.atomic` from "begin user create"
  through "signal fires". A signal handler raising rolls back the user
  row.
- A `PRE_REGISTER_VALIDATOR` setting accepts a dotted-path callable
  that runs before any DB writes; raising `ValueException` → 400, no
  user created, no signal fired.
- `register.html` exposes two new empty template blocks
  (`extra_fields`, `pre_submit_script`) so consumer apps can extend the
  form by overriding the template in their own templates dir.
- `MojoAuth.register()` in `mojo-auth.js` forwards arbitrary keys
  from its `payload` argument to the server instead of cherry-picking
  the hardcoded set.
- The signal docstring documents (a) handlers doing external-system
  work should enqueue background jobs rather than call synchronously,
  and (b) when `REQUIRE_VERIFIED_EMAIL=True` the signal still fires
  but no JWT session is active yet.
- All existing register-endpoint tests pass unchanged.

## Investigation

**What exists**:
- `on_register` — `mojo/apps/account/rest/user.py:228` (hardcoded
  fields, no group, no signal, no extension points).
- `_find_or_create_user` — `mojo/apps/account/rest/oauth.py:78`
  (reference pattern for group + member creation in the OAuth flow).
- `GroupMember` — `mojo/apps/account/models/member.py:23` (already
  supports `get_or_create(user=, group=)`).
- `jwt_login` — `mojo/apps/account/rest/user.py` (unchanged; stays
  the final step).
- `register.html` — `mojo/apps/account/templates/account/register.html`
  (no extension blocks today).
- `MojoAuth.register` — `mojo/apps/account/static/account/mojo-auth.js:218`
  (hardcoded payload shape).

**What changes**:
- `mojo/apps/account/rest/user.py` — extend `on_register`: accept
  `group`, validate, create `GroupMember`, fire signal, wrap in atomic,
  run `PRE_REGISTER_VALIDATOR` first.
- `mojo/apps/account/signals.py` — new file defining `user_registered`
  with documented kwargs.
- `mojo/apps/account/templates/account/register.html` — add
  `{% block extra_fields %}{% endblock %}` between password and terms,
  add `{% block pre_submit_script %}{% endblock %}` in the page script.
- `mojo/apps/account/static/account/mojo-auth.js` — `register()`
  forwards all keys in the payload.

**Constraints**:
- Backward compatibility is non-negotiable. Today's clients posting
  only `email`/`password`/`first_name`/`last_name` without a `group`
  must continue to work identically. Every new feature is opt-in.
- `@md.requires_bouncer_token('registration')`, the rate limiter, and
  the `ALLOW_USER_REGISTRATION` gate run *before* any new logic.
  Nothing in this change weakens existing protections.
- `REQUIRE_VERIFIED_EMAIL=True` path is unaffected — signal still
  fires (user record exists, consumer apps need to react), but
  `jwt_login` does not run.
- Group lookup accepts UUID only (consistent with OAuth flow; avoids
  leaking internal pks).

**Related files**:
- `mojo/apps/account/rest/user.py`
- `mojo/apps/account/rest/oauth.py`
- `mojo/apps/account/models/member.py`
- `mojo/apps/account/templates/account/register.html`
- `mojo/apps/account/static/account/mojo-auth.js`

## Endpoints

| Method | Path | Description | Permission |
|---|---|---|---|
| POST | `/api/auth/register` | Extended: now accepts optional `group` (UUID) and any extras listed in `REGISTRATION_EXTRA_FIELDS`. Fires `user_registered` signal after success. | Public, bouncer-token + rate-limited (unchanged) |

## Settings

| Setting | Default | Purpose |
|---|---|---|
| `REGISTRATION_EXTRA_FIELDS` | `[]` | List of additional body-param keys the endpoint will accept and forward to the `user_registered` signal in `extra`. Keys not in this list are silently dropped. |
| `PRE_REGISTER_VALIDATOR` | `""` (unset) | Dotted path to a callable `(email, password, group, request, extra) -> None` that runs before any DB writes. Raise `ValueException` to reject. |
| `REQUIRE_GROUP_ON_REGISTRATION` | `False` | When `True`, register endpoint 400s if `group` is absent. Lets strict multi-tenant deployments enforce membership at signup. |
| `ALLOW_USER_REGISTRATION` | (existing) | Unchanged. Still gates registration on/off entirely. |
| `REQUIRE_VERIFIED_EMAIL` | (existing) | Unchanged. Signal still fires; `jwt_login` does not. |

## Tests Required

- Register with no `group` → user created, no `GroupMember`, signal
  fires with `group=None`.
- Register with valid active `group` → user created, `GroupMember`
  exists, signal fires with the right group.
- Register with unknown `group` UUID → 400, no user row, signal not
  fired.
- Register with `is_active=False` group → 400, no user row, signal
  not fired.
- Register with extras in `REGISTRATION_EXTRA_FIELDS` → signal
  receives them under `extra`.
- Register with extras *not* in the allowlist → silently dropped,
  signal `extra` does not contain them, no 400.
- Synchronous signal handler raises → `transaction.atomic` rolls back;
  no `User` row exists after the failed call.
- `PRE_REGISTER_VALIDATOR` raises `ValueException` → 400, no user
  created, signal not fired.
- `REQUIRE_GROUP_ON_REGISTRATION=True` + no `group` → 400.
- `REQUIRE_VERIFIED_EMAIL=True` happy path → user created, signal
  fires, response has `requires_verification=True`, no JWT.

## Out of Scope

- Saving extras directly onto `User` columns. Consumer apps that want
  this can do it in their own signal handler. Revisit if a pattern
  emerges across multiple consumers.
- Multi-handler validation via a `user_registering` pre-signal.
  Single `PRE_REGISTER_VALIDATOR` callable is enough for v1; graduate
  to a signal if multiple consumers need to validate independently.
- OAuth-side parity (the OAuth flow already does group + member; it
  doesn't fire `user_registered` today). Adding the signal to the
  OAuth path is a follow-up — call it out, but not part of this
  ticket.
- Any consumer-app-side handler implementation. This request defines
  the framework hooks only; downstream apps implement their handlers
  in their own repos.

## Open Questions

1. **Signal name**: `user_registered` reads cleanly; alternatives like
   `user_signup_completed` disambiguate from "registration started"
   states. Prefer the shorter name unless a `user_signup_initiated`
   companion is planned.
2. **Should the signal fire for OAuth registrations too?** Out of
   scope for this request (see Out of Scope), but worth deciding the
   long-term direction before naming. If yes, the signal becomes the
   universal "new user landed" hook regardless of auth method —
   stronger contract.

## Plan

**Status**: planned
**Planned**: 2026-05-15

### Objective
Add four extension points (`PRE_REGISTER_VALIDATOR`, `USER_REGISTERED_HANDLER`, `USER_LOGIN_HANDLER`, allowlisted extras + group context on register) so consumer apps can run their own logic without forking framework endpoints.

### Hook Pattern Decision
All hooks use the established mojo pattern: a single dotted-path callable loaded via `mojo.helpers.modules.load_function()`. No `django.dispatch`. Matches `SMS_INBOUND_HANDLER`, `AUTH_BEARER_HANDLERS`, and `PRE_REGISTER_VALIDATOR` as already specced. v1 supports one handler per deployment; multi-handler graduates to chained handlers if a real second consumer appears.

### Fact Correction
Investigation in the original request claims OAuth's `_find_or_create_user` "already auto-creates `GroupMember` records" — this is incorrect. It creates `User` + `OAuthConnection` only. Password-register will be the **first** flow with group-aware membership. OAuth path gains GroupMember creation as part of this change, sourced from the existing `state_data["group_uuid"]` channel that already preserves group context for branding.

### Hook Signatures (kwargs-only invocation)
All handlers are invoked with keyword arguments only, so future additions don't break consumer handlers. Consumers should declare `**kwargs` in their handler signatures or accept the documented kwargs explicitly.

- `pre_register_validator(*, email, group, request, extra) -> None` — raise `ValueException` to reject (400). **Plaintext password is intentionally NOT passed** — strength check runs framework-side; if a consumer needs password-aware policy they wrap the endpoint in their own view.
- `user_registered_handler(*, user, request, group, source, extra) -> None` — runs inside the atomic block. Raise to roll back. `source` ∈ `{"password", "oauth"}` in v1 (future: `"saml"`, `"scim"`, `"invite"`).
- `user_login_handler(*, user, request, source, is_new_user) -> None` — wrapped by framework in try/except; runtime exceptions are logged + swallowed and never block authentication. `source` ∈ `{"password", "oauth", "magic", "email_verify", "invite", "password_reset", "totp_mfa", "passkey", "sessions_revoke", "handoff"}` after the source-backfill in step 4.

### Steps
1. **`mojo/apps/account/services/extensions.py`** (new) — three resolvers backed by a module-level cache `{setting_path: callable_or_None}`:
   - `run_pre_register_validator(*, email, group, request, extra)`
   - `fire_user_registered(*, user, request, group, source, extra)`
   - `fire_user_login(*, user, request, source, is_new_user)`

   Behavior:
   - Empty/unset setting → no-op fast-path (no import attempt).
   - Cache reset triggers when the setting *value* changes (so test `server_settings` overrides take effect).
   - Configuration-time `ImportError` → log + incident, cache `None`, return as no-op. Misconfig must not break the user flow.
   - **Register / validator handler runtime errors propagate** → caller's `transaction.atomic` rolls back. Documented contract: register handlers must be fast-path or enqueue-and-return; raising aborts the registration.
   - **Login handler runtime errors are caught + logged + reported as incident, then swallowed.** A failing analytics/SIEM handler must never lock a user out.

2. **`mojo/apps/account/rest/user.py:228` (`on_register`)** — restructure with explicit atomic boundary:
   ```
   # OUTSIDE atomic — pre-validation, no DB writes
   - Parse email/password/first_name/last_name/group; gather extras from REGISTRATION_EXTRA_FIELDS (silent-drop unknown keys)
   - Honour REQUIRE_GROUP_ON_REGISTRATION
   - Resolve Group by UUID (unknown→400, inactive→400)
   - run_pre_register_validator(...)  ← may raise ValueException → 400

   # INSIDE transaction.atomic
   - User(email=email, ...) ; user.set_password(password) ; user.save()
   - GroupMember.objects.get_or_create(user=user, group=group) if group
   - fire_user_registered(source="password")  ← raise here rolls back

   # OUTSIDE atomic — side effects safe to retry, must not roll back the user
   - tokens.generate_email_verify_token(user)
   - user.send_template_email("email_verify", ...)
   - user.report_incident(..., "register:success")

   # OUTSIDE atomic — auth handoff
   - if REQUIRE_VERIFIED_EMAIL: return verification-required response
   - return jwt_login(request, user, source="password", is_new_user=True)
   ```
   The split is intentional: SMTP hiccup must not destroy a user whose register-handler already fired side effects in the consumer's downstream systems.

3. **`mojo/apps/account/rest/user.py:345` (`jwt_login`)** — add `is_new_user=False` kwarg (positional-safe; current callers all use kwargs after `legacy=`). Call `fire_user_login(user=user, request=request, source=source, is_new_user=is_new_user)` after the JWT is built and the response data assembled, before returning. The framework wraps this call in try/except internally — login never breaks if the handler errors.

4. **Backfill `source` on every `jwt_login()` caller** so login analytics is meaningful from day one. Audit and update:
   - `user.py:453` `on_user_password_reset_code` → `source="password_reset"`
   - `user.py:476` `on_user_password_reset_token` → `source="password_reset"`
   - `user.py:526` `on_magic_login_complete` → `source="magic"` (channel-aware: `source="magic_email"` / `"magic_sms"` if the channel matters; pick one, default `"magic"`)
   - `user.py:566` `on_email_verify` → `source="email_verify"`
   - `user.py:587` `on_invite_accept` → `source="invite"`
   - `user.py:846` `on_email_change_confirm` (POST) → `source="email_change"`
   - `user.py:1177` `on_sessions_revoke` → `source="sessions_revoke"`
   - `oauth.py:287` `on_oauth_complete` → already passes `extra`; pass `source="oauth"` and `is_new_user=created`
   - `mfa/totp.py` (verify endpoints) → `source="totp_mfa"`
   - `mfa/passkeys.py` (verify endpoint) → `source="passkey"`
   - `auth/handoff` exchange (`user.py:225`) → `source="handoff"`

   Done in this same change. Any other `jwt_login()` call sites discovered during build receive the same treatment.

5. **`mojo/apps/account/rest/oauth.py:78` (`_find_or_create_user`)** — change signature to `_find_or_create_user(provider_name, profile, state_data, request)`. Both new params are required to thread the OAuth state and request context to the register-handler:
   - When path 3 (new user) runs, look up Group from `state_data.get("group_uuid")`.
   - Create GroupMember if active group resolves; log warning + skip if invalid/inactive (do not 400 — the auth attempt would be lost).
   - `fire_user_registered(user=user, request=request, group=resolved_group, source="oauth", extra={"provider": provider_name})` — runs OUTSIDE any atomic block here (OAuth doesn't currently wrap in atomic; not adding that scope this change).
   - **`oauth.py:274`** caller — pass `state_data` and `request` through.
   - **`oauth.py:287`** `jwt_login` call — add `source="oauth"`, `is_new_user=created`.

6. **`mojo/apps/account/templates/account/register.html`** — two new empty blocks:
   - `{% block extra_fields %}{% endblock %}` between password row and terms-checkbox.
   - `{% block pre_submit_script %}{% endblock %}` immediately before the form-submit handler in `{% block page_script %}`.

7. **`mojo/apps/account/static/account/mojo-auth.js:218` (`register`)** — replace the cherry-picked payload with `Object.assign({}, payload)` then `_withDevice(...)`. Existing callers continue to work unchanged. `_withDevice` injects `bouncer_token` and `duid` (already expected server-side); silent-drop on the server bounds any unintended client-state leakage.

8. **`mojo/apps/account/settings.py`** (account-app defaults) — add:
   - `REGISTRATION_EXTRA_FIELDS = []`
   - `PRE_REGISTER_VALIDATOR = ""`
   - `REQUIRE_GROUP_ON_REGISTRATION = False`
   - `USER_REGISTERED_HANDLER = ""`
   - `USER_LOGIN_HANDLER = ""`

### Design Decisions
- **Single handler, not Django signals**: matches every other extension point in mojo. v1 needs only one handler per deployment.
- **Kwargs-only handler invocation** so we can extend signatures without breaking consumers.
- **Atomic block scope is narrow and explicit**: `[user.save → GroupMember → fire_user_registered]` only. Verify-email send, incident logging, and `jwt_login` all run OUTSIDE — a failed SMTP send or JWT issuance must NOT roll back a user that genuinely exists and whose register-handler has already fired.
- **Two different error contracts for the two handler types**:
  - **Register / validator** raise → rollback + propagate (5xx). Handlers must be fast-path or enqueue-and-return; raising is "I want this rejected".
  - **Login** raise → log + swallow. Auth must never break because of analytics. Documented loudly so consumers don't assume a login handler can veto a login.
- **`source` is required on every `jwt_login` call**: backfilled in this change so the login handler receives meaningful values from day one. Without this, the handler is wired but useless.
- **Extras allowlist is silent-drop, not 400**: forward-compat with UIs that send extra context the deployment hasn't opted into yet.
- **PRE_REGISTER_VALIDATOR does not receive plaintext password**: avoids leaking credentials to consumer code. Strength check stays framework-side.
- **`User.org` is NOT touched by register**: framework today treats `User.org` and `GroupMember.group` as independent (OAuth doesn't set `User.org` either). Consumer handlers can set `user.org` themselves if their model demands it. Documented.
- **OAuth GroupMember uses `state_data["group_uuid"]`**: this channel already exists for branding context; reusing it avoids new params on `/oauth/begin` and stays consistent with how OAuth preserves group identity.
- **Handler caching**: module-level dict keyed by setting *value*. Setting change → cache miss → re-resolve. Bad path caches `None` so we don't re-attempt the import on every request, but a settings change to a different value (or the empty string) resets the entry naturally.

### Use Cases
- Per-tenant satellite-record creation on signup.
- Strict tenant deployments require group at signup (`REQUIRE_GROUP_ON_REGISTRATION=True`).
- Referral / promo / acquisition-channel capture via extras.
- Email-domain restrictions via `PRE_REGISTER_VALIDATOR`.
- Login analytics / SIEM forwarding (handler errors don't break login).
- First-login onboarding flow gating on `is_new_user`.
- Custom signup form fields via template block override.

### Edge Cases
- **Register-handler raises mid-atomic** → User row rolled back; request returns 500 with handler exception. No partial state. Verify-email is not yet sent. GroupMember rolled back too.
- **Login-handler raises** → login proceeds, JWT returned, error logged + incident reported. Consumer's analytics has a gap; user has a session. This asymmetry is intentional and documented.
- **`PRE_REGISTER_VALIDATOR` raises `ValueException`** → 400, no DB write, no handlers fired.
- **Unknown / inactive group UUID** (password path) → 400 before any write.
- **`REQUIRE_GROUP_ON_REGISTRATION=True` + missing group** → 400.
- **Extras outside allowlist** → silently dropped, no error.
- **Handler dotted-path misconfigured** → registration succeeds, incident logged. `None` cached so we don't retry import on every request.
- **`REQUIRE_VERIFIED_EMAIL=True`** → User + GroupMember + register-handler still fire; `jwt_login` does not run, so login-handler does NOT fire. Handler can detect via absence of JWT context if needed.
- **OAuth state has no `group_uuid`** → user created without GroupMember, register-handler still fires with `group=None`.
- **OAuth state `group_uuid` invalid/inactive** → log warning, create user without GroupMember (do not 400 — OAuth callback already passed; failing here loses the auth attempt). Register-handler still fires with `group=None`.
- **Verify-email send fails after `fire_user_registered` succeeded** → user exists, register-handler ran (consumer side effects committed), email send is a logged warning. User can request a new verification email.

### Testing — `tests/test_account/test_register.py` (new)
Server-process isolation rules out `mock.patch`; tests register module-level capture functions and wire them via `th.server_settings(USER_REGISTERED_HANDLER="tests.test_account.test_register._capture_register")`.

**Register flow:**
- No `group` → user created, no GroupMember, register-handler fires with `group=None`, login-handler fires with `is_new_user=True`, `source="password"`.
- Valid active group UUID → user + GroupMember exist, register-handler receives the group.
- Unknown group UUID → 400, no user, no register-handler fire.
- Inactive group → 400, no user, no register-handler fire.
- Allowlisted extras → register-handler receives `extra={...}`.
- Non-allowlisted extras → silently dropped, register-handler `extra` empty for those keys, 200 returned.
- `REQUIRE_GROUP_ON_REGISTRATION=True` + no group → 400.
- `REQUIRE_VERIFIED_EMAIL=True` happy path → user + register-handler fire, response has `requires_verification=True`, no JWT, login-handler does NOT fire.

**Atomic boundary:**
- Register-handler raises → atomic rollback, no User row, no GroupMember row.
- Verify-email send simulated to fail (handler-of-handler captures send failure) → user row STILL exists, register-handler effects STILL committed (proves verify-email is outside atomic).

**Validator contract:**
- `PRE_REGISTER_VALIDATOR` raises `ValueException` → 400, no user, no register-handler fire.
- `PRE_REGISTER_VALIDATOR` receives `email`, `group`, `request`, `extra` kwargs only — assert `password` is NOT in the kwargs (security regression guard).

**Handler error contracts:**
- Misconfigured register-handler dotted-path (bad import) → registration succeeds, incident logged.
- Misconfigured login-handler dotted-path → login succeeds, incident logged.
- **Login-handler raises at runtime → login STILL succeeds, JWT returned, error logged.** (Critical: asserts the asymmetry.)
- Register-handler raises at runtime → 5xx, no user, atomic rollback.

**Login-handler `source` backfill (one assertion per source):**
- Password login → `source="password"`.
- Magic-link complete → `source="magic"` (or channel-specific if implemented).
- Email-verify token → `source="email_verify"`.
- Invite-accept → `source="invite"`.
- Password-reset (code) → `source="password_reset"`.
- Password-reset (token) → `source="password_reset"`.
- Email-change confirm → `source="email_change"`.
- Sessions revoke → `source="sessions_revoke"`.
- TOTP MFA verify → `source="totp_mfa"`.
- Passkey verify → `source="passkey"`.
- Handoff exchange → `source="handoff"`.
- OAuth complete → `source="oauth"`, `is_new_user` reflects the `created` flag.

**Refresh exclusion:**
- `/auth/token/refresh` does NOT fire login-handler.

**OAuth path:**
- OAuth `state.group_uuid` valid+active → GroupMember created, register-handler fires with the group.
- OAuth `state.group_uuid` absent → user created, no GroupMember, register-handler fires with `group=None`.
- OAuth `state.group_uuid` invalid → user created, no GroupMember, register-handler fires with `group=None`, warning logged (auth attempt not lost).

### Docs
- `docs/django_developer/account/auth.md` — extend "Registration / Onboarding Patterns": new `group` param, `REGISTRATION_EXTRA_FIELDS`, `PRE_REGISTER_VALIDATOR`, `USER_REGISTERED_HANDLER`, `USER_LOGIN_HANDLER` with full handler signatures and the "enqueue background work, don't block" guidance. Add note that OAuth registrations also fire `USER_REGISTERED_HANDLER` with `source="oauth"`.
- `docs/web_developer/account/auth.md` — document new `group` body param and the silently-dropped extras behavior on `POST /api/auth/register`.
- `CHANGELOG.md` — entry under next unreleased section.

### Out of Scope (carried from request)
- Saving extras directly to User columns.
- Multi-handler validation (graduate `PRE_REGISTER_VALIDATOR` to a chain only if needed).
- Bringing OAuth's `_find_or_create_user` to an explicit-param group-aware pattern (uses state-channel only in v1).

## Resolution

**Status**: resolved
**Date**: 2026-05-15
**Commits**: `fa3ad79` (feature) + `849ed80` (security hardening)

### What Was Built
Three dotted-path extension hooks (`PRE_REGISTER_VALIDATOR`, `USER_REGISTERED_HANDLER`, `USER_LOGIN_HANDLER`) plus group-aware registration via a new `group_uuid` body param, an allowlisted-extras mechanism (`REGISTRATION_EXTRA_FIELDS`), and `REQUIRE_GROUP_ON_REGISTRATION` enforcement. OAuth registrations now also fire `USER_REGISTERED_HANDLER` (with `source="oauth"`) and create a GroupMember from `state_data["group_uuid"]`. Verification gate moved out of `jwt_login()` and into `on_user_login()` so the `source` kwarg is now strictly the auth-flow identifier (no overload with lookup-channel values). Backfilled `source` on every internal `jwt_login()` call site so login analytics is meaningful from day one. Two new template blocks (`extra_fields`, `pre_submit_script`) in `register.html` and `MojoAuth.register()` forwards the full payload.

### Files Changed
- `mojo/apps/account/services/extensions.py` — new module with the three hook resolvers, asymmetric error handling, and cached dotted-path loading.
- `mojo/apps/account/rest/user.py` — restructured `on_register` with explicit atomic boundary (verify-email send outside), added `is_new_user` kwarg to `jwt_login`, fires the login hook, moved verification gate into `on_user_login`, backfilled `source` on every caller, hard-strips plaintext password from `request.DATA` before invoking `PRE_REGISTER_VALIDATOR`.
- `mojo/apps/account/rest/oauth.py` — changed `_find_or_create_user(provider_name, profile, state_data=None, request=None)`, added Group resolution + GroupMember creation + `fire_user_registered` on new-user path, passes `source="oauth"` and `is_new_user=created` to `jwt_login`.
- `mojo/apps/account/rest/totp.py`, `passkeys.py`, `sms.py` — `source` backfill (`totp_mfa`, `totp_recovery`, `totp`, `passkey`, `sms`, `sms_mfa`).
- `mojo/apps/account/templates/account/register.html` — two new empty template blocks.
- `mojo/apps/account/static/account/mojo-auth.js` — `register()` forwards the full payload via `Object.assign`.

### Tests
- `tests/test_register/__init__.py` — new package, marked `serial: True`.
- `tests/test_register/_capture.py` — module-level capture handlers (filesystem JSON to bridge test process ↔ server process).
- `tests/test_register/register.py` — 15 tests covering group flows, extras, validator contract, atomic rollback, asymmetric error contracts, source backfill, refresh-token exclusion, and a defense-in-depth probe that the validator cannot reach the password via `request.DATA`.
- Run: `bin/run_tests --agent -t test_register.register`

### Docs Updated
- `docs/django_developer/account/auth.md` — new "Registration Extension Hooks" section with handler signatures, asymmetric error contract, settings table.
- `docs/django_developer/account/user.md` — six new rows in the Settings table.
- `docs/web_developer/account/authentication.md` — `group_uuid` body param and silent-drop extras behavior.
- `CHANGELOG.md` — entry under the unreleased section.

### Security Review
Two findings, both addressed:
1. **PRE_REGISTER_VALIDATOR password exposure** — fixed in `849ed80`. The validator now cannot read the plaintext password via `request.DATA` either (popped for the call duration). Test asserts this via direct probe.
2. **Verification gate move from `jwt_login()` to `on_user_login()`** — flagged as "could other flows now bypass the gate?". Empirically verified: in the prior code, the gate only fired when `source` ∈ {"email", "phone_number"}; the only caller passing those values was `on_user_login`. All other callers passed auth-flow strings that never triggered the gate. No behavior change for non-password flows. Whether password-reset / magic-link / OAuth should ALSO enforce `REQUIRE_VERIFIED_EMAIL` is an independent policy question and out of scope for this ticket.

### Test Results
Full suite green after both commits: 1870 passed, 0 failed, 56 skipped (skips are opt-in slow modules + environment-conditional).

### Follow-up
- OAuth `on_oauth_begin` already accepts `group_uuid` query param and stores it in state. No further plumbing needed.
- Consider adding a `USER_REGISTERED_HANDLER` fire on `on_invite_accept` if invite acceptance should count as "new user landed" for handlers — currently treats existing user (registered via invite-send earlier).
- Consider explicit gate enforcement on password-reset endpoints if `REQUIRE_VERIFIED_EMAIL=True` deployments want to block unverified accounts from recovering via reset.
- Settings reference docs (`docs/django_developer/helpers/settings_reference.md` if it exists) should list the five new settings.
