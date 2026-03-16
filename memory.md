# Django-MOJO Working Memory

Use this file as a lightweight running log between AI threads.

## Memory Hygiene Rules

- Keep this file compact and current.
- Keep only active/recent context in main sections.
- Cap each section to 5 active bullets max.
- Prefer outcomes and decisions over long narrative notes.
- Remove stale items once completed or no longer relevant.
- If historical context is still useful, move it to a short dated "Archive" section.

## Current Focus

- **UserAPIKey** — per-user JWT API tokens with per-key signing secret. Implementation in progress (needs migration + test run in downstream project).

## Key Decisions

- `REQUIRE_VERIFIED_EMAIL` / `REQUIRE_VERIFIED_PHONE` default to `False` — opt-in only.
- **OAuth is a trusted second factor** — bypasses MFA gate entirely.
- **Never use `override_settings` in testit tests.** Use `settings.get()` + `TestitSkip` instead.
- FK assignments in `on_rest_save_related_field` must call `_set_field_change` before `setattr`.
- **Notification preferences default is allow** — only suppress on explicit opt-out. System/transactional emails never suppressed.
- **UserAPIKey uses `token_type="user_api_key"`** (not `"api_key"` — that's the group-scoped `ApiKey`). Per-key `auth_key` in `mojo_secrets`. Revoke via `POST_SAVE_ACTIONS`. `User.validate_jwt` handles the `user_api_key` branch directly (overrides mixin).
- **Dynamic URL segments go at the end only.** Per-instance actions use `POST_SAVE_ACTIONS` + `on_action_<name>`.

## In-Progress Work

- **UserAPIKey** — new model + migration needed in downstream project. Files changed:
  - `mojo/apps/account/models/user_api_key.py` — new model: `create_for_user()`, `on_action_revoke()`
  - `mojo/apps/account/models/user.py` — added `user_api_key` branch in `validate_jwt`; removed `generate_api_token()`
  - `mojo/apps/account/rest/user_api_key.py` — moved generate endpoints here; revoke via `POST_SAVE_ACTIONS`
  - `mojo/apps/account/rest/user.py` — removed generate endpoints
  - `mojo/apps/account/models/__init__.py` + `rest/__init__.py` — added imports
  - Run: `python manage.py makemigrations && python manage.py migrate`
- **Awaiting user test run** for v1.0.58 sprint features (notification_prefs, totp_recovery, username_change, session_revoke, deactivation, security_events, oauth)

## Handoff Notes — v1.0.58 Sprint (7 requests)

All 7 requests implemented. Files changed per feature:

### 1. Notification Preferences
- `mojo/apps/account/services/notification_prefs.py` — new: `is_notification_allowed()`, `get_preferences()`, `set_preferences()`
- `mojo/apps/account/rest/notification_prefs.py` — new: GET + POST endpoints
- `mojo/apps/account/rest/__init__.py` — added import
- `mojo/apps/account/models/notification.py` — wired `is_notification_allowed` in `Notification.send()`
- `mojo/apps/account/models/user.py` — added `kind` param to `send_template_email()` and `push_notification()`
- `tests/test_accounts/notification_prefs.py` — new test file

### 2. TOTP Recovery Codes
- `mojo/apps/account/models/totp.py` — added `generate_recovery_codes()`, `get_masked_recovery_codes()`, `verify_and_consume_recovery_code()`
- `mojo/apps/account/rest/totp.py` — modified `on_totp_confirm`, added GET recovery-codes, POST regenerate, POST recover
- `tests/test_accounts/totp_recovery.py` — new test file
- `docs/web_developer/account/mfa_totp.md` — updated

### 3. Username Change
- `mojo/apps/account/rest/user.py` — added `on_username_change`
- `tests/test_accounts/username_change.py` — new test file

### 4. Session Revoke
- `mojo/apps/account/rest/user.py` — added `on_sessions_revoke`
- `tests/test_accounts/session_revoke.py` — new test file

### 5. Account Deactivation
- `mojo/apps/account/utils/tokens.py` — added `KIND_DEACTIVATE`, `generate_deactivate_token()`, `verify_deactivate_token()`
- `mojo/apps/account/rest/user.py` — added `on_account_deactivate`, `on_account_deactivate_confirm`
- `tests/test_accounts/deactivation.py` — new test file

### 6. Security Events Log
- `mojo/apps/account/rest/user.py` — added `on_account_security_events` with kind→summary mapping
- `tests/test_accounts/security_events.py` — new test file

### 7. Linked OAuth Accounts + set_unusable_password fix
- `mojo/apps/account/rest/oauth.py` — added `set_unusable_password()` in path 3, added CRUD + custom DELETE with lockout guard
- `tests/test_accounts/oauth.py` — extended with connection management tests
- `docs/web_developer/account/oauth.md` — added Managing Connections section

### Shared docs updated
- `docs/web_developer/account/user_self_management.md` — 5 new sections (11-15), renumbered, quick reference table updated
- `docs/web_developer/account/notifications.md` — added preferences section
- `docs/web_developer/account/authentication.md` — added session revoke + security events cross-references
- `docs/django_developer/account/user.md` — added new settings to table
- `CHANGELOG.md` — v1.0.58 entry

## Open Questions

- **Verification gate scope — needs more thought.** Current behaviour: `REQUIRE_VERIFIED_EMAIL` only gates email-identifier logins. A broader `REQUIRE_VERIFIED_EMAIL_ALL_LOGINS` may be needed for some deployments. Needs design before implementation.

## Archive

- 2026-04-01: 7-request sprint specs completed and ready for implementation.
- 2026-03-14: OAuth email-verified fix + docs (v1.0.57).
- 2026-03-14: CloudWatch monitoring work completed (v1.0.51-v1.0.54).
- 2026-03-14: Shortlink + metrics work completed.
- 2026-03-14: Email/phone verification + email change work completed (v1.0.48-v1.0.50).