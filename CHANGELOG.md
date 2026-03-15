## v1.0.56 - (current)

### New Features

- account: added `method` param to `POST /api/auth/verify/email/send` — pass `{ "method": "code" }` to send a 6-digit OTP to the user's inbox instead of a verification link; default `"link"` is fully backward-compatible (`mojo/apps/account/rest/verify.py`)
- account: added `POST /api/auth/verify/email/confirm` — authenticated endpoint to confirm email ownership by submitting the 6-digit OTP; mirrors `POST /api/auth/verify/phone/confirm` exactly; sets `is_email_verified=True` and emits `account:email:verified` realtime event (`mojo/apps/account/rest/verify.py`)
- account: added `method` param to `POST /api/auth/email/change/request` — pass `{ "method": "code" }` to send a 6-digit OTP to the new address instead of a confirmation link; default `"link"` is fully backward-compatible (`mojo/apps/account/rest/user.py`)
- account: extended `POST /api/auth/email/change/confirm` — now accepts `{ "code": "123456" }` (requires authentication) alongside the existing `{ "token": "ec:..." }` (unauthenticated, token is the credential); both paths commit the change, rotate `auth_key`, and return a fresh JWT (`mojo/apps/account/rest/user.py`)
- account: updated `POST /api/auth/email/change/cancel` — now clears both link-flow JTI and code-flow OTP in a single call, regardless of which method was used to initiate the change (`mojo/apps/account/rest/user.py`)
- account: added `generate_email_verify_code()` and `verify_email_verify_code()` to token infrastructure — 6-digit OTP stored in `mojo_secrets`, TTL controlled by `EMAIL_VERIFY_CODE_TTL` (default 10 min), single-use (`mojo/apps/account/utils/tokens.py`)
- account: added `generate_email_change_otp()` and `verify_email_change_otp()` to token infrastructure — 6-digit OTP stored in `mojo_secrets`, TTL controlled by `EMAIL_CHANGE_CODE_TTL` (default 10 min), single-use; mutually exclusive with the `ec:` link token so both paths cannot be active simultaneously (`mojo/apps/account/utils/tokens.py`)

### Docs

- docs: updated `docs/web_developer/account/email_verification.md` — added code flow section for `POST /api/auth/verify/email/send` and `POST /api/auth/verify/email/confirm`; updated write-protection table; added `EMAIL_VERIFY_CODE_TTL` to settings reference; updated realtime events section
- docs: updated `docs/web_developer/account/email_change.md` — added code flow for request and confirm; restructured confirm into Option A (code), Option B (link→API page), Option C (link→frontend); updated cancel, security notes, template requirements, and settings reference; added `email_change_code` template docs
- docs: updated `docs/django_developer/account/email_change.md` — added code flow token infrastructure reference; updated endpoint table; added confirm routing logic; documented `email_change_code` template; added cancel internals section; added settings reference table; expanded security design notes

## v1.0.55


### New Features

- account: added `GET /api/auth/email/change/confirm` — browser-friendly confirm endpoint for email change links; renders `account/email_change_confirm.html` on success or error; supports `?redirect=<url>` param for automatic redirect after 3 seconds on success (`mojo/apps/account/rest/user.py`, `mojo/apps/account/templates/account/email_change_confirm.html`)
- account: upgraded `GET /api/auth/verify/email/confirm` — now renders `account/email_verify_confirm.html` instead of returning JSON; supports `?redirect=<url>` param; handles error states (invalid token, disabled account) with descriptive template pages (`mojo/apps/account/rest/verify.py`, `mojo/apps/account/templates/account/email_verify_confirm.html`)
- account: added realtime WebSocket event `account:email:changed` — emitted to all active sessions after a successful email change confirm (both GET and POST paths); allows open sessions to react to the `auth_key` rotation cleanly (`mojo/apps/account/rest/user.py`)
- account: added realtime WebSocket event `account:email:verified` — emitted after `GET /api/auth/verify/email/confirm` succeeds (`mojo/apps/account/rest/verify.py`)
- account: added realtime WebSocket event `account:phone:verified` — emitted after `POST /api/auth/verify/phone/confirm` succeeds (`mojo/apps/account/rest/verify.py`)
- account: added `POST /api/auth/phone/change/request` — begin a self-service phone number change; requires `current_password`; sends a 6-digit OTP via SMS to the new number (`mojo/apps/account/rest/user.py`)
- account: added `POST /api/auth/phone/change/confirm` — commit a phone number change by submitting the session token and OTP; sets `is_phone_verified=True` on success (`mojo/apps/account/rest/user.py`)
- account: added `POST /api/auth/phone/change/cancel` — cancel a pending phone number change immediately; idempotent (`mojo/apps/account/rest/user.py`)
- account: added `KIND_PHONE_CHANGE` (`pc:`) token kind to the token infrastructure with `generate_phone_change_token()` and `verify_phone_change_token()`; TTL defaults to 10 minutes (`mojo/apps/account/utils/tokens.py`)

### Security / Bug Fixes

- account: `on_rest_pre_save` now normalizes and uniqueness-checks `phone_number` on every REST save, and resets `is_phone_verified=False` whenever the phone number changes — previously the verified flag was not cleared on a direct phone number update (`mojo/apps/account/models/user.py`)
- account: `_handle_existing_user_pre_save` now blocks direct REST replacement of an existing phone number for non-superusers — must use the `auth/phone/change/*` flow to prove ownership of the new number before it is committed (`mojo/apps/account/models/user.py`)

### Docs

- docs: added `docs/web_developer/account/phone_change.md` — full REST API reference for the phone number change flow
- docs: updated `docs/web_developer/account/README.md` — added Phone Number Change link
- docs: updated `docs/web_developer/account/email_verification.md` — added Realtime Events section, Template Customisation section, and cross-reference to phone_change.md
- docs: updated `docs/web_developer/account/email_change.md` — documented GET confirm endpoint, Option A/B integration patterns, redirect param, Realtime Events section, and Template Customisation section

## v1.0.50 - March 15, 2026

fix local dev bugs for passkeys and uploads
added list graph for user


## v1.0.49 - March 14, 2026

support to get runners sysinfo


### New Features

- jobs: added `jobs.get_sysinfo(runner_id=None, timeout=5.0)` — collects live host system info (OS, CPU, memory, disk, network) from one or all active runners via the existing `broadcast_execute`/`execute_on_runner` control channel; always returns a list of reply dicts (`mojo/apps/jobs/__init__.py`, `mojo/apps/jobs/services/sysinfo_task.py`)
- jobs: added `GET /api/jobs/runners/sysinfo` — REST endpoint returning sysinfo from all active runners; accepts optional `timeout` query param (`mojo/apps/jobs/rest/jobs.py`)
- jobs: added `GET /api/jobs/runners/sysinfo/<runner_id>` — REST endpoint returning sysinfo for a specific runner; returns 404 when the runner does not respond (`mojo/apps/jobs/rest/jobs.py`)

### Tests

- tests: added `tests/test_jobs/test_sysinfo.py` — permission guard tests (always run), Python API shape tests, and live-runner tests (skipped via `TestitSkip` when no runners are active)

### Docs

- docs: updated `docs/django_developer/jobs/README.md` — added Runner Sysinfo section covering `get_sysinfo()` usage, return shape, and `psutil` requirement
- docs: updated `docs/web_developer/jobs/jobs.md` — added Runner Sysinfo section covering both REST endpoints, response shapes, and error reply format

## v1.0.48 - March 14, 2026

new aws metrics support


### Improvements

- aws: added `memory` category for EC2 — fetches `mem_used_percent` from the `CWAgent` namespace (requires the CloudWatch Agent installed on the instance; instances without the agent return all-zero values) (`mojo/helpers/aws/cloudwatch.py`)
- aws: added `disk` category for EC2 — fetches `disk_used_percent` from the `CWAgent` namespace, targeting the root filesystem (`path="/"`); rounds out the three core utilisation metrics alongside `cpu` and `memory` (requires the CloudWatch Agent; instances without the agent return all-zero values) (`mojo/helpers/aws/cloudwatch.py`)
- aws: added `CATEGORY_NAMESPACE_OVERRIDE` table — maps `(account, category)` pairs that require a non-default CloudWatch namespace; used as the extension point for any future categories that live outside their account's primary namespace (`mojo/helpers/aws/cloudwatch.py`)
- aws: added `CATEGORY_EXTRA_DIMENSIONS` table — maps `(account, category)` pairs that require additional fixed dimensions beyond the primary instance dimension (e.g. `disk` requires `path="/"` to target the root filesystem); appended automatically inside `fetch()` (`mojo/helpers/aws/cloudwatch.py`)
- aws: added `resolve_namespace(account, category)` helper — returns the correct CloudWatch namespace for a given account/category pair, consulting `CATEGORY_NAMESPACE_OVERRIDE` before falling back to `ACCOUNT_NAMESPACE`; `fetch()` now calls this instead of the bare `ACCOUNT_NAMESPACE` lookup (`mojo/helpers/aws/cloudwatch.py`)

### Bug Fixes

- aws: fixed CloudWatch `_fetch_values` returning all-zero values for every metric on live systems — two root causes (`mojo/helpers/aws/cloudwatch.py`):
  1. **Timezone mismatch**: boto3 returns CloudWatch `Timestamp` values as timezone-aware datetimes (`tzlocal()`); bucket keys were naive UTC. Added `replace(tzinfo=None)` to strip timezone before key lookup.
  2. **Period offset mismatch**: CloudWatch returns datapoints at an internal offset (e.g. `:17` past the hour) rather than on clean period boundaries. A plain `replace(second=0, microsecond=0)` was not sufficient — timestamps are now floored to the period boundary using `_align_to_period()` before being used as dict keys, matching how `_build_buckets` constructs the bucket list.

### Tests

- tests: added `cw_fetch_ec2_memory` — verifies the `memory` category returns a valid `200` response with correct shape; non-zero assertion is conditional on the CloudWatch Agent being present (all-zero is legitimate when the agent is not installed) (`tests/test_aws/cloudwatch.py`)
- tests: added `cw_fetch_ec2_disk` — verifies the `disk` category returns a valid `200` response with correct shape; same conditional non-zero pattern as memory (`tests/test_aws/cloudwatch.py`)

### Docs

- docs: updated `docs/django_developer/aws/cloudwatch.md` — added `memory` to category table with CWAgent footnote, documented `CATEGORY_NAMESPACE_OVERRIDE` and `resolve_namespace()` under a new Namespace Resolution section, updated module-level helper examples
- docs: updated `docs/web_developer/aws/cloudwatch.md` — added `memory` to EC2-only category table with CWAgent footnote and install link

---

## v1.0.54

### Improvements

- aws: CloudWatch `fetch()` now resolves friendly names for chart slugs — EC2 instances use their `Name` tag value (e.g. `"web-server-1"`) instead of the raw AWS instance ID (e.g. `"i-0abc1234"`); RDS and ElastiCache identifiers are already human-readable and are used as-is (`mojo/helpers/aws/cloudwatch.py`)
- aws: `fetch()` `slugs` input parameter now accepts either friendly names or raw AWS IDs — both are resolved to the underlying instance ID before the CloudWatch call is made (`mojo/helpers/aws/cloudwatch.py`)
- aws: added `CloudWatchHelper.list_resource_slugs(account)` — returns `[{id, slug}]` for a given account type; used internally by `fetch()` for id↔slug mapping and available directly for callers that need to enumerate resources with their display names (`mojo/helpers/aws/cloudwatch.py`)
- aws: `GET /api/aws/cloudwatch/resources` now includes a `slug` field on every resource entry — the same friendly name that will appear in chart labels; use `slug` (not `id`) as input to the `fetch` endpoint's `slugs` parameter (`mojo/apps/aws/rest/cloudwatch.py`)

### Tests

- tests: updated `cw_resources_list` — asserts that each resource entry now includes a non-empty `slug` field; stashes both `ec2_id` (raw AWS ID) and `ec2_slug` (friendly name) for downstream tests (`tests/test_aws/cloudwatch.py`)
- tests: updated single-slug and per-resource tests to pass the friendly slug (not the raw AWS ID) as the `slugs` parameter, matching production usage
- tests: added `cw_fetch_ec2_slug_is_name` — verifies end-to-end that when an EC2 instance has a `Name` tag the returned `slug` in the response matches the friendly name advertised by the `resources` endpoint, and does not look like a raw instance ID (`tests/test_aws/cloudwatch.py`)

### Docs

- docs: updated `docs/django_developer/aws/cloudwatch.md` — documented friendly-slug behavior in `fetch()`, updated examples to use friendly names, documented `list_resource_slugs()`, clarified `slugs` parameter accepts names or IDs
- docs: updated `docs/web_developer/aws/cloudwatch.md` — added friendly-name overview, updated `resources` response shape to show `slug` field, updated `fetch` query parameter description and all response examples

---

## v1.0.53

### Tests

- tests: `login_with_phone_e164`, `login_with_phone_unformatted`, and `login_with_phone_wrong_password` now raise `TestitSkip` when `ALLOW_PHONE_LOGIN=False` on the server — these tests require phone-as-username login to be enabled and were failing unconditionally on servers where it is not (`tests/test_accounts/accounts.py`)

---

## v1.0.52

### Bug Fixes

- account: `REQUIRE_VERIFIED_EMAIL` gate was incorrectly blocking logins where the identifier was a plain **username** — the gate now only fires when `source == "email"` (i.e. the user submitted an email address as their login identifier). Username-based logins are never gated by email verification status (`mojo/apps/account/rest/user.py`)

### Tests

- tests: fixed `test_email_gate_blocks_unverified`, `test_email_gate_allows_verified`, and `test_email_gate_wrong_password_returns_401` — all three were posting with `username=TEST_USER` (a plain username), which would no longer trigger the gate; they now use the email address as the login identifier to correctly exercise the gate path
- tests: added `test_email_gate_does_not_block_username_login` — asserts that a user with an unverified email can still log in via username when `REQUIRE_VERIFIED_EMAIL=True`

### Docs

- docs: updated `docs/web_developer/account/email_verification.md` — clarified that `REQUIRE_VERIFIED_EMAIL` only gates email-identifier logins; username logins are not affected
- docs: updated `docs/web_developer/account/authentication.md` — added explicit callout that the email gate does not apply to plain username logins

---

## v1.0.51

### AWS CloudWatch Monitoring

- aws: added `CloudWatchHelper` in `mojo/helpers/aws/cloudwatch.py` — boto3 wrapper for fetching live time-series metrics from CloudWatch for EC2 instances (`AWS/EC2`), RDS DB instances (`AWS/RDS`), and ElastiCache clusters (`AWS/ElastiCache`)
- aws: high-level `CloudWatchHelper.fetch(account, category, slugs, ...)` mirrors the metrics app API exactly — same `account`/`category`/`slugs` parameters, same `periods` + `data` response shape; existing frontend chart components work without modification
- aws: when `slugs` is omitted, all instances for the account type are discovered automatically via `list_instance_ids(account)` — no need to specify IDs for the common case
- aws: mapping tables in `cloudwatch.py` — `ACCOUNT_NAMESPACE`, `ACCOUNT_DIMENSION`, `CATEGORY_METRIC`, `GRANULARITY_SECONDS`, `STAT_MAP` — drive all account/category/granularity resolution; invalid combos raise `ValueError` (REST layer converts to `400`)
- aws: two REST endpoints under `manage_aws` permission (`mojo/apps/aws/rest/cloudwatch.py`):
  - `GET /api/aws/cloudwatch/resources` — list EC2, RDS, and ElastiCache resource IDs (use as `slugs`)
  - `GET /api/aws/cloudwatch/fetch` — time-series metric data; params: `account`, `category`, `slugs` (optional), `dt_start`, `dt_end`, `granularity` (`minutes`/`hours`/`days`), `stat` (`avg`/`max`/`min`/`sum`)
- aws: gap buckets (no CloudWatch datapoints) filled with `0.0` so `periods` and `values` are always the same length and cover the full requested range
- aws: `CloudWatchHelper` exported from `mojo/helpers/aws/__init__.py`
- docs: added `docs/django_developer/aws/cloudwatch.md` — helper usage, category reference table, IAM policy, and testing guide
- docs: added `docs/web_developer/aws/cloudwatch.md` — endpoint reference, category tables by account type, response shape, granularity guide, and error codes
- docs: updated both README indexes to include the new AWS CloudWatch section

### Tests

- tests: added `tests/test_aws/cloudwatch.py` — permission guard, missing-param, invalid account, invalid category, and wrong-account-for-category validation tests always run (no AWS credentials needed); live resource-list and metric fetch tests skip gracefully via `TestitSkip` when `AWS_KEY` is not configured on the server

---

## v1.0.50

### Bug Fixes

- rest: `on_rest_save_related_field` now calls `_set_field_change` before every `setattr` — FK assignments (e.g. `org`) previously never appeared in `changed_fields`, so guards like `MANAGE_USERS_ONLY_FIELDS` silently did nothing when a relation was set via a raw PK integer (`mojo/models/rest.py`)
- settings: `SettingsHelper.get()` now reads from the live `django.conf.settings` proxy on every call instead of caching `self.root` — the cached reference went stale under Django's `override_settings`, causing settings changes to be ignored (`mojo/helpers/settings/helper.py`)
- account: `on_email_change_request` now reads `ALLOW_EMAIL_CHANGE` at call time via `settings.get()` instead of using the module-level constant frozen at import time (`mojo/apps/account/rest/user.py`)

### Phone Gate

- account: confirmed `REQUIRE_VERIFIED_PHONE` gate applies symmetrically to password login when the identifier is a phone number (`ALLOW_PHONE_LOGIN=True`) — `lookup_from_request_with_source` returns `source="phone_number"` which flows into `_check_verification_gate` via `jwt_login`; no code change was required, only test coverage was missing

### Phone Verification Endpoints

- account: `POST /api/auth/verify/phone/send` — authenticated; sends a 6-digit OTP to the user's `phone_number` on file; no-ops with 200 if already verified; returns 400 if no phone number is set (`mojo/apps/account/rest/verify.py`)
- account: `POST /api/auth/verify/phone/confirm` — authenticated; submits the 6-digit code to set `is_phone_verified=True`; code is single-use and expires after `PHONE_VERIFY_CODE_TTL` seconds (default 10 min); does not issue a new JWT (`mojo/apps/account/rest/verify.py`)
- tokens: `generate_phone_verify_code(user)` / `verify_phone_verify_code(user, code)` — stores code + timestamp in user secrets, same pattern as SMS OTP (`mojo/apps/account/utils/tokens.py`)
- docs: updated `docs/web_developer/account/email_verification.md` — replaced "coming soon" note with full endpoint reference, added `PHONE_VERIFY_CODE_TTL` to settings table, updated write-protection table with the new confirm endpoint

### Email Template Seeds

- aws: added seed JSON files for all account email templates — `email_verify.json`, `email_verify_link.json`, `email_change_confirm.json`, `email_change_notify.json` (`mojo/apps/aws/seeds/email_templates/`)
- aws: `seed_email_templates` command skips existing records by default; `--update-existing` is an explicit opt-in — safe to re-run at any time

### Tests

- tests: fixed `test_accounts.verification` and `test_accounts.email_change` suites — all tests now pass
- tests: gate tests (`REQUIRE_VERIFIED_EMAIL`, `REQUIRE_VERIFIED_PHONE`, `ALLOW_PHONE_LOGIN`, `ALLOW_EMAIL_CHANGE`) now read the live server setting and raise `TestitSkip` with a descriptive message when the required setting is not active — `override_settings` has no effect across the testit process boundary
- tests: added three new phone-gate tests covering the password-login-via-phone-identifier path (off by default, blocks unverified, allows verified)
- tests: fixed email change REST tests — replaced invalid `user_id=opts.user_id` kwarg pattern (silently ignored by `requests`) with explicit `opts.client.login()` / `opts.client.logout()` calls
- tests: fixed `resp.json()` → `resp.json` throughout email change tests — `json` is a plain `objict` attribute on the testit `RestClient` response, not a callable
- tests: write-protect and field-protect test actors now created with `is_email_verified=True` in setup; individual tests ensure the target user's `is_email_verified` is restored before each login so the email gate does not block test logins when `REQUIRE_VERIFIED_EMAIL=True` is active
- tests: `sms auto-verify` standalone test now skips when `REQUIRE_VERIFIED_PHONE=True` — the gate correctly fires before auto-verify in that configuration, and the gate behavior is already covered by the dedicated gate tests

## v1.0.49

### Self-Service Email Change

- account: added `POST /api/auth/email/change/request` — authenticated, password-confirmed request to change email; sends a confirmation link to the new address and a notification to the old address; current email is unchanged until confirmed
- account: added `POST /api/auth/email/change/confirm` — public token-exchange endpoint; commits new email, sets `is_email_verified=True`, rotates `auth_key` (invalidates all prior sessions), and issues a fresh JWT in one step
- account: added `POST /api/auth/email/change/cancel` — authenticated cancel; clears `pending_email` and nulls the stored `ec:` JTI so the outstanding link is dead immediately, before its 1-hour TTL expires; idempotent (no-op when no change is pending)
- account: `username` is automatically mirrored to the new email address on confirm when it matched the old email address
- account: email availability is re-checked at confirm time to guard against the race where another account registers the target address in the 1-hour window
- tokens: added `KIND_EMAIL_CHANGE = "ec"` token (1-hour TTL, configurable via `EMAIL_CHANGE_TOKEN_TTL`) with `generate_email_change_token(user, new_email)` / `verify_email_change_token(token)` — stores `pending_email` in user secrets alongside the JTI (same pattern as `magic_login_channel`)
- account: added `ALLOW_EMAIL_CHANGE` setting (default `True`) — set to `False` to disable the entire self-service email change flow; request endpoint returns 403 when disabled

### Tests

- tests: added `tests/test_accounts/email_change.py` — token unit tests (prefix, pending_email storage, verify tuple return, single-use, kind-mismatch rejection, expiry, auth-key rotation, re-request invalidation, garbage rejection) and REST endpoint tests for all three endpoints (request happy path, auth required, wrong password, missing password, same-email, duplicate-email, invalid format, setting disabled, confirm commit, auth-key rotation, username mirroring, inactive user, race condition, token single-use, kind mismatch, cancel clears pending + JTI, cancel no-op, cancel-then-confirm rejected)

### Docs

- docs/web: added `docs/web_developer/account/email_change.md` — full reference for all three endpoints, recommended UI flow, security notes, and settings reference
- docs/web: `docs/web_developer/account/README.md` already lists the new doc (entry was pre-existing)

## v1.0.48

### Email & Phone Verification

- account: added `REQUIRE_VERIFIED_EMAIL` setting (default `False`) — blocks password/email-based logins until `is_email_verified=True`
- account: added `REQUIRE_VERIFIED_PHONE` setting (default `False`) — blocks SMS-based logins until `is_phone_verified=True`
- account: login gate returns structured `{"error": "email_not_verified"}` / `{"error": "phone_not_verified"}` 403 so clients can prompt appropriately rather than showing a generic error
- account: added `POST /api/auth/email/verify/send` — sends a verification link; anti-enumeration (always 200, inactive users silently ignored)
- account: added `POST /api/auth/email/verify` — exchanges `ev:` token, sets `is_email_verified=True`, issues JWT in one step
- account: added `POST /api/auth/invite/accept` — exchanges `iv:` invite token, sets `is_email_verified=True`, issues JWT
- tokens: added `KIND_INVITE = "iv"` token (7-day TTL, configurable via `INVITE_TOKEN_TTL`) with `generate_invite_token` / `verify_invite_token`
- account: `send_invite` now issues a purpose-specific `iv:` token instead of the legacy `pr:` password-reset alias
- account: added `User.lookup_from_request_with_source()` — returns `(user, source)` where source is `"email"`, `"phone_number"`, or `"username"`; used to select the correct verification gate at login
- sms: standalone SMS OTP verify (`/api/auth/sms/verify` without `mfa_token`) now auto-sets `is_phone_verified=True` on success — phone receipt proves ownership
- sms: MFA-step SMS verify does **not** auto-set `is_phone_verified` (completing your own 2FA is not a verification act)

### User Model Field Security

- account: `is_email_verified` and `is_phone_verified` are now superuser-only via REST (both create and update paths)
- account: `requires_mfa`, `last_activity`, `auth_key` added to superuser-only field guard
- account: `is_active` and `org` now require `manage_users` permission to write via REST; owners can no longer deactivate/reactivate their own accounts or self-assign an org
- account: `SUPERUSER_ONLY_FIELDS` and `MANAGE_USERS_ONLY_FIELDS` extracted to module-level `frozenset` constants for audibility
- account: removed `creds_changed` flag passed between `on_rest_pre_save` and `_handle_existing_user_pre_save`; logic now lives where it belongs

### Tests

- tests: added `tests/test_accounts/verification.py` covering token unit tests (prefix, single-use, expiry, auth-key rotation, resend invalidation, cross-user rejection), REST endpoint correctness and security, verification gate (email + phone), SMS auto-verify, and full field write-protection matrix for all newly protected fields

### Docs

- docs/web: added `docs/web_developer/account/email_verification.md` — full reference for verification endpoints, invite flow, phone verification, UI flow, and settings
- docs/web: updated `authentication.md` with Email Verification Gate section
- docs/web: updated `account/README.md` index with link to new verification doc

## v1.0.45 - March 14, 2026
## v1.0.47 - March 14, 2026

support for a user saving to /api/user/me


## v1.0.46 - March 14, 2026

user level security for metrics
improved shorten for bots


## v1.0.45 - March 14, 2026

fixing shortlink permissions



- docs/agenting: synchronized `Agent.md` and `CLAUDE.md` with current repo structure and rules
- prompts: expanded `prompts/planning.md` and `prompts/building.md` with explicit mode routing and preflight steps
- process: added mandatory new-thread startup protocol (read `Agent.md` + `CLAUDE.md`, then choose planning vs building mode)
- conventions: removed stale doc-path references and reinforced framework constraints (no migrations, no project-level test execution in this repo)
- process: restored `memory.md` as an explicit source of thread-to-thread context and added a repository memory template
- process: added memory hygiene rules to keep `memory.md` compact, pruned, and decision-focused
- process: aligned startup preflight across `Agent.md`, `CLAUDE.md`, and prompt modes to read `memory.md` before planning/building
- docs: linked root developer documentation tracks to each other for clearer source-of-truth navigation
- docs/auth: added explicit frontend token storage guidance (`localStorage`) and page-reload session validation/refresh flow to web authentication docs
- docs/web: added `frontend_starter.md` and linked it from web root/core docs for a single frontend bootstrap guide
- docs/shortlink: clarified owner permissions for shortlink CRUD endpoints and documented that click-history remains `manage_shortlinks` scoped
- shortlink: expanded bot user-agent detection to cover Apple Messages and major chat/mail preview clients (Signal, Teams/Outlook preview, Google Chat/Gmail preview, Yahoo Mail, Thunderbird, Spark, Notion, Linear, Zoom)
- tests: expanded shortlink bot detection/OG preview tests to cover new user-agent signatures and preserve browser redirect behavior
- shortlink/metrics: resolve now records global click metric only (removed per-source metric) and optionally records user-scoped per-link metrics when `track_clicks=True` and `user` is set
- metrics: `metrics.record()` now supports `expires_at` override and `disable_expiry` for per-call retention control
- tests/docs: added shortlink metric behavior tests and updated metrics/shortlink developer docs for new retention/account behavior
- metrics/security: unified account permission checks across metrics endpoints and added `user-<id>` account enforcement (self-access by default, deny other-user access)
- tests: added metrics API coverage for `user-<id>` account read/write permissions
- docs/web-shortlink: added explicit metrics retrieval guide for global and user-scoped shortlink analytics (`shortlink:click` and `sl:click:<code>`)
- tests: added metrics API coverage confirming `group-<id>` account permissions still work (authorized member allowed, outsider denied)
- docs/frontend: added incident/event reporting guidance for uncaught errors, promise rejections, and auth/session anomalies in frontend starter

## v0.1.3 - May 29, 2025
## v1.0.44 - March 14, 2026

new shortlink management


## v1.0.43 - March 13, 2026

new shortlink app for url shortening


## v1.0.43 - March 13, 2026

- NEW: shortlink app — URL shortener with OG previews, file linking, metrics, and opt-in click tracking
- shortlink: bot detection for rich link previews (Slack, Twitter, Facebook, WhatsApp, Android/iOS Messages)
- shortlink: async OG metadata scraping via jobs system
- shortlink: bot_passthrough flag to skip preview rendering for transactional links
- shortlink: is_protected flag to prevent auto-deletion by cleanup job
- shortlink: cron job to prune expired links after 7-day grace period


## v1.0.42 - March 13, 2026

fileman cleanup, bug fixes


## v1.0.42 - March 13, 2026

- fileman: full audit of REST API, docs, and tests; fixed backend path handling, rendition.get_setting, missing import, removed nonexistent is_upload_expired property
- account: add user.pii_anonymize() for GDPR right-to-erasure compliance
- magic login: SMS channel support via method=sms on /api/auth/magic/send; channel tracked in mojo_secrets and cleared after verify
- bugfix: MojoSecrets.refresh_from_db now clears _exposed_secrets cache to prevent stale reads after DB reload


## v1.0.41 - March 12, 2026

sms mappings


## v1.0.40 - March 12, 2026

support sms to fake numbers mappings


## v1.0.39 - March 12, 2026

new notification system made easy


## v1.0.38 - March 12, 2026

bug fix in refresh token not have correct expiry


## v1.0.37 - March 12, 2026

fixing bug in sms login, fixing bug in tests


## v1.0.36 - March 12, 2026

typo fix


## v1.0.35 - March 12, 2026

support username in sms login


## v1.0.34 - March 12, 2026

bug fixes, more security patches


## v1.0.33 - March 12, 2026

improve MFA support


## v1.0.32 - March 11, 2026

ability to login with phonenumber


## v1.0.31 - March 11, 2026

new rate limiting login


## v1.0.30 - March 11, 2026

proper phone hub endpoints


## v1.0.29 - March 11, 2026

making some common phone apis publlic


## v1.0.28 - March 08, 2026

don't save when only doing model actions


## v1.0.27 - March 08, 2026

fixing api key permission checks
fixing false test


## v1.0.26 - March 08, 2026

bugfix for metrics decorators


## v1.0.25 - March 07, 2026

streamlined response with simile dicts now


## v1.0.24 - March 04, 2026

NEW django cache support to deal with collisions using django-redis-cache


## v1.0.23 - March 03, 2026

save api keys
- Added first-party Django Redis cache backend: `mojo.cache.MojoRedisCache` (replaces `redis_cache.RedisCache` usage).
- Added migration docs for cache backend settings and dependency cleanup.


## v1.0.22 - March 03, 2026

New feature to send and wait for events to come back


## v1.0.21 - March 01, 2026

new content guard


## v1.0.20 - February 27, 2026

* superuser rightfully has all permissions


## v1.0.19 - February 26, 2026

new oauth flows


## v1.0.18 - February 24, 2026

- New API KEYs support, new rate limit decorators, and metrics decorators


## v1.0.17 - February 12, 2026

BUGFIX for OneToOne fields


## v1.0.16 - February 12, 2026

NEW FILEVAULT APP


## v1.0.15 - February 10, 2026

* ADDED auto email templates
* Cleanup of filemaner and is_public check


## v1.0.14 - February 07, 2026

* Major cleanup of domain utils


## v1.0.13 - February 01, 2026

* BUGFIX USPS requires caps on states


## v1.0.12 - February 01, 2026

* Fixing Phone lookup for international numbers


## v1.0.11 - February 01, 2026

* Improved API key access
* better docs for realtime and metricsw
* new improved ability to have absolute routing ie prefix with /
* major bug fix in cron parsing of multiple times
* new domain helper utility


## v1.0.10 - December 24, 2025

bug fix for issue when multiple people access IoT lock


## v1.0.9 - December 17, 2025

bug fix when using list helpers
allow incidents to ignore rules


## v1.0.8 - December 11, 2025

fix for iso format null


## v1.0.7 - December 09, 2025

fixing rule field to text


## v1.0.6 - December 06, 2025

fixing bug when lock syncs via realtime more then once


## v1.0.5 - December 06, 2025

fixing realtime debugging


## v1.0.4 - December 04, 2025

bug fix when using isnull=False


## v1.0.3 - December 03, 2025

bug fix in sync of metadata


## v1.0.2 - December 03, 2025

fixing bug in fetching category data


## v1.0.1 - December 03, 2025

we are ready for 1.0 release


## v0.1.141 - December 03, 2025

fixing bug in category


## v0.1.140 - December 03, 2025

* adding scope to security events


## v0.1.139 - December 03, 2025

fixing bug in calculating totals


## v0.1.138 - December 01, 2025

missing fileman migrations


## v0.1.137 - December 01, 2025

* improvements to file handling
* improvements to metrics labeling weekly


## v0.1.136 - November 25, 2025

BUGFIX search


## v0.1.135 - November 25, 2025

BUGFIX: membership not propogating


## v0.1.134 - November 23, 2025

* missing migration file


## v0.1.133 - November 23, 2025

BUGFIX in bundling incidents by rules


## v0.1.132 - November 21, 2025

BUGFIX is broadcast messages


## v0.1.131 - November 21, 2025

publish broadcast async


## v0.1.130 - November 21, 2025

Adding server name to incidents so cyber engine can do action on one server


## v0.1.129 - November 19, 2025

syntax error


## v0.1.128 - November 19, 2025

BUGFIX in permissions for member invites


## v0.1.127 - November 19, 2025

* BUGFIX tier level access for a platform vs kyc customer


## v0.1.126 - November 19, 2025

BUGFIX when publishing templates with non native types


## v0.1.125 - November 19, 2025

fixing issue when inviting kyc client vs customer


## v0.1.124 - November 18, 2025

HOTFIX cyber report downloads failing in csv format


## v0.1.123 - November 18, 2025

* ADDED logic for improved date handling in relation to government ids"


## v0.1.122 - November 17, 2025

* CRITICAL FIX in log permissions fail gracefully
* sysinfo in correct fields
* improved email template handling


## v0.1.120 - November 01, 2025

No auth required for address suggestions


## v0.1.119 - October 31, 2025

Updating geo location


## v0.1.118 - October 30, 2025

Another TYPO


## v0.1.117 - October 30, 2025

TYPO in fcm (push notifications)


## v0.1.116 - October 30, 2025

ability to log Push notifications for debugging


## v0.1.115 - October 28, 2025

New Phonehub, qrcode, improved testit


## v0.1.114 - October 26, 2025

Advanced Compliance features


## v0.1.113 - October 24, 2025

NEW phonehub which provide detailed compliance for phone numbers


## v0.1.112 - October 22, 2025

BUGFIX searching for group members


## v0.1.111 - October 22, 2025

bugfix allow user to subscribe to self


## v0.1.110 - October 21, 2025

more socket cleanup


## v0.1.109 - October 21, 2025

Custom FCM implementation to work around issues


## v0.1.108 - October 21, 2025

Cleanup of FCM


## v0.1.107 - October 21, 2025

* BUGFIXES in rules and events


## v0.1.105 - October 17, 2025

* New incident engine cleanup


## v0.1.104 - October 16, 2025

Missing key migrations


## v0.1.103 - October 16, 2025

HOTFIX raw json lists in posts not handled correctly


## v0.1.102 - October 15, 2025

Update geo ip for forensics


## v0.1.101 - October 15, 2025

Config to allow incident and rule deletion


## v0.1.100 - October 15, 2025

* Cleanup and debugging of rules and incidents


## v0.1.99 - October 15, 2025

HOTFIX - shared context bug with requests


## v0.1.98 - October 14, 2025

Invite tokens


## v0.1.97 - October 13, 2025

HOTFIX don't show protected fields in changes


## v0.1.96 - October 13, 2025

Invalidate user login tokens when after a TTL


## v0.1.95 - October 13, 2025

Fixing broken login flows


## v0.1.94 - October 11, 2025

BUGFIX automated email setup


## v0.1.93 - October 11, 2025

Fixing aws email auto config


## v0.1.92 - October 11, 2025

test fails to catch syntax error


## v0.1.91 - October 11, 2025

FIXING SES Audit


## v0.1.90 - October 11, 2025

BUGFIX filestore for each user + group


## v0.1.89 - October 11, 2025

BUGFIX filemanager creating empty


## v0.1.87 - October 11, 2025

fix user upload


## v0.1.86 - October 11, 2025

Fixing file uploads for group


## v0.1.85 - October 10, 2025

group support


## v0.1.84 - October 10, 2025

simple group data


## v0.1.83 - October 10, 2025

dump all even lists


## v0.1.82 - October 10, 2025

* LOGIT_DEBUG_ALL for all logging


## v0.1.81 - October 08, 2025

Better logging


## v0.1.80 - October 08, 2025

Bugfix non str id in redis pool


## v0.1.79 - October 08, 2025

BUGfix geolocated


## v0.1.78 - October 08, 2025

* BUGFIX geoip no provider


## v0.1.77 - October 06, 2025

Syntax error tests failed


## v0.1.76 - October 06, 2025

Fixing cloud messaging mobile registration


## v0.1.75 - October 06, 2025

legacy login support debug


## v0.1.74 - October 06, 2025

Legacy login


## v0.1.73 - October 06, 2025

HOTFIX channels package removal


## v0.1.72 - October 05, 2025

Robustness of redis pools


## v0.1.71 - October 05, 2025

FIXES in aws email sending


## v0.1.70 - October 05, 2025

ADDED missing stats helper


## v0.1.69 - October 05, 2025

* mroe debug


## v0.1.68 - October 05, 2025

* trying to fix cluster bug


## v0.1.67 - October 05, 2025

* Bug fix in redis cluster mode


## v0.1.66 - October 05, 2025

* Fixes to group level permissions


## v0.1.65 - October 03, 2025

* ADDED advanced permissions via group/child/parent chaining


## v0.1.64 - October 02, 2025

* Bug in managing group members


## v0.1.63 - October 01, 2025

* ADDED ticket status changes to notes


## v0.1.62 - September 30, 2025

* Ticket bug fix


## v0.1.61 - September 30, 2025

* Fixing int fields


## v0.1.60 - September 29, 2025

* FIX no more raising redis timeout in pools


## v0.1.59 - September 28, 2025

* Bug fixes in realtime


## v0.1.58 - September 28, 2025

* more realtime logic


## v0.1.57 - September 26, 2025

* Atomic save bug


## v0.1.56 - September 26, 2025

HOTFIX atomic commits


## v0.1.55 - September 25, 2025

BUGFIX checking group member permission


## v0.1.54 - September 25, 2025

ossec fixes


## v0.1.53 - September 25, 2025

debug ossec


## v0.1.52 - September 25, 2025

* FIX ossec alerts not parsing


## v0.1.51 - September 25, 2025

* FIX password without current password


## v0.1.50 - September 25, 2025

* realtime disconnect dead connections


## v0.1.49 - September 25, 2025

* REWRITE of realtime


## v0.1.47 - September 24, 2025

debug


## v0.1.46 - September 24, 2025

debug


## v0.1.45 - September 24, 2025

debug


## v0.1.44 - September 24, 2025

* more robust error handling on channels


## v0.1.43 - September 24, 2025

* debug


## v0.1.42 - September 24, 2025

* debugging channels


## v0.1.41 - September 24, 2025

* REALTIME support


## v0.1.40 - September 24, 2025

* ADDED Channels


## v0.1.39 - September 24, 2025

* CRITICAL FIX: potential credential leakage


## v0.1.38 - September 24, 2025

* Added ticket category


## v0.1.37 - September 23, 2025

* FIX job reaper falsely kill done jobs


## v0.1.36 - September 23, 2025

* fixing filtering on no related models


## v0.1.35 - September 23, 2025

* FIX cron scheduling


## v0.1.34 - September 22, 2025

* Fixed advanced filtering


## v0.1.33 - September 22, 2025

* Ticket bug fix


## v0.1.32 - September 21, 2025

* Added new auto security checks on rest end points


## v0.1.31 - September 18, 2025

* Last fix did not take


## v0.1.30 - September 18, 2025

* ANother bug fix in jobs claiming jobs it cannot run


## v0.1.29 - September 18, 2025

* BUGFIX infinite retries on import func errors


## v0.1.28 - September 18, 2025

* BUGFIX job select_for_update bug


## v0.1.27 - September 18, 2025

* Debugging for jobs engine


## v0.1.26 - September 17, 2025

* Minor fixes in metrics and activity tracking


## v0.1.25 - September 16, 2025

* Added: more helpers to testit
* Added: more logic for redis pool and "with syntax"


## v0.1.24 - September 12, 2025

* New status commands


## v0.1.23 - September 12, 2025

* BUGFIX saving metrics perms


## v0.1.22 - September 10, 2025

* FIX for serverless/clusters


## v0.1.21 - September 10, 2025

* More servless bug fixes


## v0.1.20 - September 10, 2025

* BUG fixing serverless valkey/redis


## v0.1.19 - September 09, 2025

* attempting to fix pipeline bugs


## v0.1.18 - September 09, 2025

fixing redis auth


## v0.1.17 - September 09, 2025

* Fix pyright auto importing wrong modules


## v0.1.16 - September 09, 2025



## v0.1.15 - September 09, 2025

  * Major cleanup and new features see docs


## v0.1.14 - July 08, 2025

  CLEANUP and UnitTests for tasks


## v0.1.13 - June 09, 2025

   ADDED fileman app, a complete filemanager for django with rendition support and multiple backends and renderers
   UPDATED simple serializer greatly improved and new advanced serializer with support for other output formats
   UPDATED incidents subsystem for handling system events, rules and incidents
   


## v0.1.10 - June 06, 2025

   CHANGED license from MIT to Apache 2.0
   ADDED to new fileman app with file storage
   ADDED new notify framework that support mail, sms, etc
   ADDED crypto support for hmac signing and verifying
   ADDED more tests
   NOTE framework is not ready for primetime yet, but soon


## v0.1.9 - June 04, 2025

   UPDATE moved mojo tests into mojo project root, but still require a django project to run
   FIXED crypto encrypt,decrypt, and hash with proper tests
   ADDED incident system for report events and having them trigger incidents, including rules engine
   ADDED MojoSecrets which allows storing of secret encrypted data into a model
   ADDED helper scripts for talking to godaddy api and automating SES setup
   ADDED new mail handling system (work in progress)


## v0.1.8 - June 01, 2025

  Updaing version info and tagging release


## v0.1.7 - June 01, 2025

   Updating version info and release


## v0.1.4 - May 30, 2025

  ADDED: lots of improvements to making metrics cleaner and passing all tests
  ADDED: mojo JsonResponse to use ujson and ability to add future logic for custom handling of certain data


## v0.1.3 - May 29, 2025

  ADDED support to ignore github release and use tags


## v0.1.3 - May 29, 2025

  ADDED: more robust publishing, including github releases



  CLEANUP: moved django apps into apps folder to be more readable
  ADDED: more utility functions and trying to use more builting functions and less custom
  ADDED: useragent parsing and remote ip
  ADDED: support for nested apps
  ADDED: version info to default api
  ADDED: testit support for django_unit_setup and django_unit_test in django env