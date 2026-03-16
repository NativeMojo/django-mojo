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

- No active items.

## Key Decisions

- `REQUIRE_VERIFIED_EMAIL` / `REQUIRE_VERIFIED_PHONE` default to `False` — opt-in only.
- **OAuth is a trusted second factor** — bypasses MFA gate entirely.
- **Never use `override_settings` in testit tests.** Mutate `django.conf.settings` directly and restore in `finally`. `SettingsHelper.get()` re-reads live settings every call.
- FK assignments in `on_rest_save_related_field` must call `_set_field_change` before `setattr`.
- **Notification preferences default is allow** — only suppress on explicit opt-out. System/transactional emails never suppressed.
- **`metadata.protected` is framework-protected.** `on_rest_update_jsonfield` in `mojo/models/rest.py` blocks writes to the `protected` sub-key for non-superusers. Use for system-set fields: `registration_source`, `invited_by_id`, `invited_to_group_id`.
- **Transactional token URLs** use `build_token_url(flow, token, request, user, group)` from `mojo/apps/account/utils/webapp_url.py`. Single frontend auth endpoint: `{base_url}{auth_path}?flow={flow}&token={token}`. Config via `group.metadata["webapp_base_url"]` and `group.metadata["webapp_auth_path"]` (no deploy needed). Settings `WEBAPP_BASE_URL` / `WEBAPP_AUTH_PATH` as fallbacks.
- **`maybe_shorten_url`** in `mojo/apps/shortlink/__init__.py` — wraps any URL if shortlink app is installed; no extra setting needed.
- **`jwt_login(extra=None)`** — `extra` dict merged into response `data`, not into `token_package` (JWT payload stays clean). OAuth uses `extra={"is_new_user": True}` for path 3.
- **`get_protected_metadata(key, default=None)` / `set_protected_metadata(key, value)`** on `User` — safe read/write to `metadata["protected"]` without clobbering other keys. `jwt_login` records `orig_webapp_url` (first login, never overwritten) and `last_webapp_url` (updated every login). `orig_webapp_url` is step 5 in `get_webapp_base_url` lookup chain.

## In-Progress Work

- **UserAPIKey** — new model + migration needed in downstream project. Files changed:
  - `mojo/apps/account/models/user_api_key.py` — new model: `create_for_user()`, `on_action_revoke()`
  - `mojo/apps/account/models/user.py` — added `user_api_key` branch in `validate_jwt`; removed `generate_api_token()`
  - `mojo/apps/account/rest/user_api_key.py` — moved generate endpoints here; revoke via `POST_SAVE_ACTIONS`
  - `mojo/apps/account/rest/user.py` — removed generate endpoints
  - `mojo/apps/account/models/__init__.py` + `rest/__init__.py` — added imports
  - Run: `python manage.py makemigrations && python manage.py migrate`

## Open Questions

- **Verification gate scope — needs more thought.** Current behaviour: `REQUIRE_VERIFIED_EMAIL` only gates email-identifier logins. A broader `REQUIRE_VERIFIED_EMAIL_ALL_LOGINS` may be needed for some deployments. Needs design before implementation.

## Archive

- 2026-04-01: v1.0.58 sprint (7 features): notification prefs, TOTP recovery codes, username change, session revoke, account deactivation, security events log, linked OAuth accounts + lockout guard — all implemented, tested, documented.
- 2026-03-16: OAuth registration gate (`OAUTH_ALLOW_REGISTRATION`), `is_new_user` signal, transactional token URL building (`build_token_url`), `maybe_shorten_url`, webapp URL helpers, `get/set_protected_metadata`, `orig_webapp_url`/`last_webapp_url` — all implemented, tested, and documented.
- 2026-03-16: Passkeys discoverable credentials fix (username optional on login begin).
- 2026-04-01: 7-request sprint specs completed and ready for implementation.
- 2026-03-14: OAuth email-verified fix + docs (v1.0.57).
- 2026-03-14: CloudWatch monitoring work completed (v1.0.51-v1.0.54).
- 2026-03-14: Shortlink + metrics work completed.
- 2026-03-14: Email/phone verification + email change work completed (v1.0.48-v1.0.50).