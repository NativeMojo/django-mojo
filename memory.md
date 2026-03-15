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

- No active task. Phone number change flow + security fixes shipped (v1.0.55).

## Key Decisions

- `REQUIRE_VERIFIED_EMAIL` / `REQUIRE_VERIFIED_PHONE` default to `False` — opt-in only. No breaking change for existing deployments.
- Verification gate fires inside `jwt_login` via `source` param — single choke point for all login paths (password, SMS, phone-as-identifier, magic link).
- `REQUIRE_VERIFIED_EMAIL` gate fires **only when `source == "email"`** — plain username logins (`source == "username"`) are never gated. Bug fix: `"username"` was incorrectly included in the gate condition.

- `REQUIRE_VERIFIED_PHONE` gate is symmetric with the email gate: if the login identifier is a phone number (`ALLOW_PHONE_LOGIN=True`), the phone gate applies on password login too.
- **Never use `override_settings` in testit tests.** Testit hits a real HTTP server in a separate process — `override_settings` only patches the calling process. Setting-dependent tests must read the live setting via `settings.get()` and raise `TestitSkip` when the required setting is not active.
- FK assignments in `on_rest_save_related_field` must call `_set_field_change` before `setattr` so the field appears in `changed_fields` and guards like `MANAGE_USERS_ONLY_FIELDS` fire correctly.

## In-Progress Work

- None.



## Open Questions

- **Verification gate scope — needs more thought.** Current behaviour: `REQUIRE_VERIFIED_EMAIL` only gates logins where the identifier is an email address; username logins always pass through. There is probably a valid use case where a project wants to require verified email (or phone) before *any* login is allowed, regardless of identifier type (e.g. a sign-up flow where all new accounts must verify before using the app). This would need a separate setting — something like `REQUIRE_VERIFIED_EMAIL_ALL_LOGINS` — so the current default stays safe and non-breaking. Needs design before implementation.


## Handoff Notes

- phone change (v1.0.55): three new endpoints in `mojo/apps/account/rest/user.py`:
  - `POST /api/auth/phone/change/request` — requires `current_password`; sends 6-digit OTP to new number; returns `session_token`.
  - `POST /api/auth/phone/change/confirm` — requires `session_token` + `code`; commits change, sets `is_phone_verified=True`; no JWT rotation (phone not a JWT signing input).
  - `POST /api/auth/phone/change/cancel` — idempotent; kills pending state and JTI immediately.
  - `KIND_PHONE_CHANGE` (`pc:`) added to token infrastructure in `tokens.py`; TTL=10min; `generate_phone_change_token(user, new_phone)` → `(session_token, otp)`, `verify_phone_change_token(token, code)` → `(user, new_phone)`.
  - Security fix: `on_rest_pre_save` now normalizes phone, checks uniqueness, and resets `is_phone_verified=False` on any phone number change.
  - Security fix: `_handle_existing_user_pre_save` blocks direct REST replacement of an existing phone number for non-superusers — must use change flow.
  - First-time set and clearing to `null` are still allowed via plain profile update.
  - Docs: `docs/web_developer/account/phone_change.md` added and linked from account README and email_verification.md.
  - `ALLOW_PHONE_CHANGE` setting (default `True`) gates the feature; `PHONE_CHANGE_TOKEN_TTL` (default `600`) controls OTP lifetime.

- jobs sysinfo (v1.0.55): `jobs.get_sysinfo(runner_id=None, timeout=5.0)` added to `mojo/apps/jobs/__init__.py`.
  - Broadcasts `mojo.apps.jobs.services.sysinfo_task.collect_sysinfo` via `broadcast_execute` (all runners) or `execute_on_runner` (single runner).
  - Always returns a list of reply dicts: `{runner_id, func, status, timestamp, result}`.
  - REST: `GET /api/jobs/runners/sysinfo` (all) and `GET /api/jobs/runners/sysinfo/<runner_id>` (one, 404 on timeout).
  - Both endpoints accept optional `?timeout=` query param (default `5.0`).
  - Tests: `tests/test_jobs/test_sysinfo.py` — permission guards always run; live-runner tests skip via `TestitSkip` when no runners active.
  - Requires `psutil` installed in runner environment.
  - Run in downstream project: `python manage.py testit test_jobs.test_sysinfo`

- Email gate bug fix: `_check_verification_gate` in `mojo/apps/account/rest/user.py` — removed `"username"` from gate condition. Gate now only fires for `source == "email"`. Username logins always pass through regardless of `REQUIRE_VERIFIED_EMAIL`.
- Gate tests updated in `tests/test_accounts/verification.py` — block/allow/wrong-password tests now submit the email address as the identifier (not the username) to correctly exercise the gate. Added new test: username login must not be blocked when gate is on.
- Docs updated: `docs/web_developer/account/email_verification.md` and `docs/web_developer/account/authentication.md` — clarified gate only applies to email-identifier logins.
- Phone login tests (`login_with_phone_e164`, `login_with_phone_unformatted`, `login_with_phone_wrong_password`) now raise `TestitSkip` when `ALLOW_PHONE_LOGIN=False` — they require the setting to be enabled on the server and were failing unconditionally without it.
- CloudWatch (v1.0.51–v1.0.54): two endpoints — `GET /api/aws/cloudwatch/resources` and `GET /api/aws/cloudwatch/fetch`.
- `fetch` mirrors the metrics app exactly: `account` = resource type, `category` = metric shortname, `slugs` = friendly names or AWS IDs (auto-discovered when omitted).
- **Slugs are friendly names** (v1.0.54): EC2 uses the `Name` tag value (falls back to instance ID); RDS/ElastiCache identifiers are already human-readable. `slugs` input accepts either friendly names or raw IDs — resolved internally.
- `resources` endpoint now includes a `slug` field on every entry — use this as input to `fetch`'s `slugs` parameter.
- `CloudWatchHelper.list_resource_slugs(account)` returns `[{id, slug}]` for the given account type.
- IAM policy needed: `cloudwatch:GetMetricStatistics`, `ec2:DescribeInstances`, `rds:DescribeDBInstances`, `elasticache:DescribeCacheClusters`.
- Run in downstream project: `python manage.py testit test_aws.cloudwatch`

## Archive

- 2026-03-14: CloudWatch monitoring work completed (v1.0.51) + friendly-slug resolution (v1.0.54).
  - `CloudWatchHelper` with `fetch()` mirrors metrics app API: `account`/`category`/`slugs` params, `periods`+`data` response.
  - Two REST endpoints: `GET /api/aws/cloudwatch/resources` + `GET /api/aws/cloudwatch/fetch`.
  - `slugs` omitted → all instances auto-discovered for the account type.
  - Slugs in responses are friendly names (EC2 Name tag, or ID fallback; RDS/ElastiCache IDs are already human-readable).
  - `slugs` input accepts friendly names or raw AWS IDs — both resolved to instance ID before CloudWatch call.
  - `list_resource_slugs(account)` → `[{id, slug}]`; `resources` endpoint exposes `slug` field on each entry.
  - Mapping tables (`CATEGORY_METRIC`, `ACCOUNT_NAMESPACE`, etc.) live in `cloudwatch.py` as plain dicts.
  - Tests skip gracefully when `AWS_KEY` not configured; permission/param/invalid-category tests always run.
  - Docs added for both developer tracks; both README indexes updated.

- 2026-03-14: Shortlink + metrics work completed.
  - Global shortlink analytics (`shortlink:click`) always on; per-source metrics removed.
  - User-scoped per-link analytics when `track_clicks=True` + `link.user` set.
  - `metrics.record()` supports `expires_at` override and `disable_expiry`.
  - Unified metrics REST permission checks; added `user-<id>` enforcement.
  - Expanded bot UA detection for Apple Messages, major chat/mail preview clients.
  - All shortlink + metrics tests and docs updated.

- 2026-03-14: Email/phone verification + email change work completed (v1.0.48–v1.0.50).
  - `REQUIRE_VERIFIED_EMAIL` / `REQUIRE_VERIFIED_PHONE` gates on all login paths.
  - Token infrastructure: `ev:`, `iv:`, `ec:` kinds with JTI rotation and auth-key binding.
  - Self-service email change: request / confirm / cancel endpoints.
  - `SUPERUSER_ONLY_FIELDS`, `MANAGE_USERS_ONLY_FIELDS`, `NO_SAVE_FIELDS` on User model.
  - Full test suites: `tests/test_accounts/verification.py` (80+ tests), `tests/test_accounts/email_change.py`.
  - Docs for both web developer and Django developer audiences.
  - Bug fixes: `on_rest_save_related_field` FK tracking, `SettingsHelper` live-read, `ALLOW_EMAIL_CHANGE` call-time read, testit auth pattern (`login()`/`logout()` not `user_id=`), `resp.json` not `resp.json()`.
  - Phone gate confirmed symmetric with email gate on password+phone-identifier login path.
  - Email template seeds shipped in `mojo/apps/aws/seeds/email_templates/`.
