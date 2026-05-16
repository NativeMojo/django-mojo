# SMS Mojo Provider — delegate SMS sending to a remote mojo instance

**Type**: request
**Status**: resolved
**Date**: 2026-05-16
**Priority**: medium

## Description

Add a new `mojo` provider option to `phonehub.PhoneConfig` that lets a django-mojo instance delegate outbound SMS to another django-mojo instance over HTTP. The remote (provider) mojo holds the real Twilio/AWS credentials and exposes the existing `POST /api/phonehub/sms/send` endpoint; the calling mojo authenticates with an `account.ApiKey` token and stores the resulting SMS row locally with the provider-side message id.

## Context

Today `phonehub.models.sms.SMS.send()` calls `services.twilio` directly — there is no provider dispatch, even though `PhoneConfig.provider` already enumerates `twilio` and `aws`. Multiple mojo deployments in the same org want to share one set of Twilio credentials (single source of compliance, billing, A2P registration). Rather than each deployment re-implementing Twilio config, a "central SMS mojo" can act as the provider and others can authenticate to it like any other API client.

The existing `POST /api/phonehub/sms/send` endpoint (`mojo/apps/phonehub/rest/sms.py:16`) is already suitable as the provider-side surface — it accepts `to_number`, `body`, optional `from_number`, group, and metadata, and returns the SMS object. We just need a client-side provider that wraps it.

User confirmed scope decisions: new provider lives in PhoneConfig (not settings), auth is the existing `ApiKey` model, no status callbacks (caller does not track delivery beyond initial send), and inbound SMS is out of scope.

## Acceptance Criteria

- `PhoneConfig.PROVIDER_CHOICES` includes `('mojo', 'Mojo Remote Instance')`.
- `PhoneConfig` stores a remote base URL field and an encrypted API key secret for the mojo provider.
- `SMS.send()` dispatches by `PhoneConfig.provider` (resolved via `PhoneConfig.get_for_group(group)`), routing to twilio, aws (stub/existing), or a new `services/mojo_provider.py`.
- When `provider == 'mojo'`, `SMS.send()` POSTs to `<remote_url>/api/phonehub/sms/send` with `Authorization: apikey <token>`, body `{to_number, body, from_number?, metadata?}`.
- A successful remote call marks the local SMS row `sent` with `provider='mojo'`, `provider_message_id=<remote SMS id>`, and stores the remote SMS payload in `metadata['remote']`.
- A failed remote call (non-2xx, timeout, network error) marks the local SMS row `failed` with `error_code='remote_error'` (or `'timeout'` / `'http_<status>'`) and `error_message=<remote body or exception>`.
- `PhoneConfig.test_connection()` includes a `_test_mojo()` branch that hits a lightweight endpoint on the remote (e.g. `GET /api/account/me`) and validates the API key.
- Backwards-compatible: existing twilio-only deployments continue to work without any config change. `PhoneConfig` is still optional — if none exists, falls back to the current twilio path.
- Docs updated in both `docs/django_developer/phonehub/` and `docs/web_developer/phonehub/` (or equivalent) describing the new provider, required ApiKey perms, and the configuration steps for both caller and remote.

## Investigation

**What exists**
- `mojo/apps/phonehub/models/config.py:22-27` — `PROVIDER_CHOICES` already enumerates providers; just `twilio` + `aws` today. `MojoSecrets` base class supports adding encrypted secret fields (see `set_twilio_credentials` at line 104 for the pattern).
- `mojo/apps/phonehub/models/config.py:131` — `test_connection()` already dispatches per provider; ready to add a `_test_mojo()` branch.
- `mojo/apps/phonehub/models/sms.py:167-206` — `SMS.send()` is hard-coded to `services.twilio`. It does NOT currently consult `PhoneConfig.provider`. Dispatch logic must be added here.
- `mojo/apps/phonehub/rest/sms.py:16-49` — `POST /api/phonehub/sms/send` already accepts the needed fields and requires `send_sms` + `comms` perms. Reusable as the provider-side surface with no changes.
- `mojo/apps/account/models/api_key.py` — full ApiKey implementation with per-endpoint perms, rate limits, expiry. `Authorization: apikey <token>` header is recognized by `mojo/middleware/auth.py:16`.
- `mojo/apps/github/services/github_app.py` — example pattern for outbound HTTP using `requests` with bearer-style auth headers.

**What changes**
- `mojo/apps/phonehub/models/config.py` — add `'mojo'` choice; add `mojo_remote_url = CharField`; add `set_mojo_api_key()` / `get_mojo_api_key()` helpers backed by `MojoSecrets`; add `_test_mojo()`.
- `mojo/apps/phonehub/models/sms.py` — refactor `SMS.send()` to resolve `PhoneConfig.get_for_group(group)` and dispatch to the right provider service. Preserve existing twilio behavior when no PhoneConfig exists or when `provider == 'twilio'`.
- `mojo/apps/phonehub/services/mojo_provider.py` — NEW. Exposes `PROVIDER = 'mojo'`, `send_sms(body, to_number, from_number, config)` that does the HTTP POST with `requests`, returns a response object matching the shape used by `services.twilio.send_sms` (`.sent`, `.id`, `.code`, `.error`) so the dispatcher can stay uniform.
- Migration: new fields on `PhoneConfig`. Run `bin/create_testproject` after model change per `.claude/rules/core.md`.
- `docs/django_developer/phonehub/README.md` (and `models.md`, `rest.md` where relevant) — document the new provider, config steps, ApiKey setup on the remote.
- `docs/web_developer/phonehub/` — note that delegated sending happens server-side; web clients still call `/api/phonehub/sms/send` the same way.
- `CHANGELOG.md` — entry for the new provider.

**Constraints**
- Secrets handling: API key MUST be stored via `MojoSecrets` (encrypted), never as a plain CharField — matches existing `twilio_auth_token` / `aws_secret_access_key` patterns.
- The remote URL field must be validated (https in production) and stripped of trailing slashes before use.
- HTTP timeout MUST be bounded (e.g. 10s default, configurable via setting `SMS_REMOTE_TIMEOUT`). A hung remote must not block the caller's request.
- No silent fallbacks: if `provider == 'mojo'` and the remote call fails, mark the SMS `failed` — do not silently try twilio. Failover requires explicit design.
- `PhoneConfig` is org-scoped today; the same scoping carries through unchanged (group → its mojo provider, no group → system default).
- Per `.claude/rules/core.md`, no `import logging` — use `mojo.helpers.logit`. Use `request.DATA` (already done in existing endpoint).
- Per `.claude/rules/performance.md`, the dispatcher should avoid re-querying `PhoneConfig` inside `SMS.send` if it has already been resolved.

**Related files**
- `mojo/apps/phonehub/models/config.py`
- `mojo/apps/phonehub/models/sms.py`
- `mojo/apps/phonehub/services/twilio.py` (reference for response shape)
- `mojo/apps/phonehub/services/__init__.py` (export new module)
- `mojo/apps/phonehub/rest/sms.py` (provider-side surface — unchanged)
- `mojo/apps/account/models/api_key.py` (provider-side auth)
- `mojo/middleware/auth.py` (apikey scheme recognition)
- `docs/django_developer/phonehub/README.md`
- `tests/test_phonehub_*` (existing phonehub tests for the pattern)

## Endpoints

No NEW endpoints. The existing endpoint on the **remote (provider) mojo** is the integration surface:

| Method | Path | Description | Permission |
|---|---|---|---|
| POST | `/api/phonehub/sms/send` | Provider-side endpoint the caller posts to. Already exists at `mojo/apps/phonehub/rest/sms.py:16`. Caller must hold an `ApiKey` granting `send_sms` + `comms`. | `send_sms`, `comms` |

Optionally, a lightweight verification endpoint for `_test_mojo()` — recommend reusing an existing endpoint like `GET /api/account/me` rather than adding a new one.

## Settings

| Setting | Default | Purpose |
|---|---|---|
| `SMS_REMOTE_TIMEOUT` | `10` (seconds) | HTTP timeout for outbound calls to the remote mojo SMS endpoint. |

(No settings-level URL/key — those live on `PhoneConfig` so they can be per-group and encrypted.)

## New `PhoneConfig` fields

| Field | Type | Notes |
|---|---|---|
| `mojo_remote_url` | `CharField(max_length=255, blank=True, null=True)` | Base URL of the remote mojo (no trailing slash), e.g. `https://sms.example.com`. |
| `mojo_api_key` (secret) | stored in `mojo_secrets` | Set via `set_mojo_api_key(token)` / read via `get_mojo_api_key()`. NEVER exposed in graphs. |

## Tests Required

- `SMS.send()` with `PhoneConfig.provider='mojo'` and a mocked successful HTTP response → local SMS row is `sent`, `provider='mojo'`, `provider_message_id` matches remote id, `metadata['remote']` populated.
- `SMS.send()` with `PhoneConfig.provider='mojo'` and HTTP 401 from remote → local SMS row is `failed`, `error_code='http_401'`, error_message populated.
- `SMS.send()` with `PhoneConfig.provider='mojo'` and a timeout → local SMS row is `failed`, `error_code='timeout'`.
- `SMS.send()` with `PhoneConfig.provider='mojo'` but `mojo_remote_url` or `mojo_api_key` missing → SMS row is `failed`, `error_code='config_error'`, no HTTP call attempted.
- `SMS.send()` with `PhoneConfig.provider='twilio'` (or no PhoneConfig at all) → unchanged behavior, calls existing twilio path. Regression guard.
- `PhoneConfig.test_connection()` for the mojo provider with a valid mocked response → `success=True`; with a 401 → `success=False, error='invalid_credentials'`.
- `mojo_secrets` integration: `set_mojo_api_key('abc')` then `get_mojo_api_key()` round-trips; the value never appears in `default` or `full` graph output.
- Auth round-trip on the remote: an integration test that uses testit's API key flow against the existing `POST /api/phonehub/sms/send` to confirm the perms (`send_sms`, `comms`) are still enforced.

## Out of Scope

- Inbound SMS forwarding from the provider mojo to the caller (no webhook back to caller, no `MO` routing logic).
- Delivery status callbacks / status webhooks from provider mojo back to caller.
- Caller polling of remote SMS rows to refresh status (can be added later as a separate request).
- Failover between providers (e.g. mojo → twilio on failure). Single-provider routing only.
- Multi-tenant routing on the provider side (one ApiKey == one caller group; sharding/quota is left to the provider's existing ApiKey rate-limit machinery).
- Phone number lookups via remote mojo (`PhoneConfig.lookup_enabled` continues to use the local provider).
- AWS SNS implementation (already a separate stub; not addressed here).

## Plan

**Status**: planned
**Planned**: 2026-05-16

### Objective
Add a `mojo` provider option to `phonehub.PhoneConfig` so one django-mojo instance can delegate outbound SMS to another over HTTP, authenticating with an `account.ApiKey` token — mirroring the geoip mojo-as-provider pattern.

### Steps
1. `mojo/apps/phonehub/models/config.py` — add `('mojo', 'Mojo Remote Instance')` to `PROVIDER_CHOICES` (line 22); add `mojo_remote_url = CharField(max_length=255, blank=True, null=True)`; add `set_mojo_api_key()` / `get_mojo_api_key()` backed by `MojoSecrets` (mirrors `set_twilio_credentials` at line 104); add `_test_mojo()` branch to `test_connection()` (line 131) that hits `GET <url>/api/account/me` with the apikey header; normalize trailing slash on `mojo_remote_url`.
2. `mojo/apps/phonehub/services/mojo_provider.py` — NEW. `PROVIDER = "mojo"`; `send_sms(body, to_number, from_number, base_url, api_key, timeout=None)` returns an `objict` shaped like `services/twilio.py:_send_sms` (`.sent`, `.id`, `.code`, `.error`, `.status`); uses `requests.post` to `<base_url>/api/phonehub/sms/send` with `Authorization: apikey <token>`; error mapping → `timeout` / `http_<status>` / `remote_error` / `remote_failed`; default timeout from `settings.get_static("SMS_REMOTE_TIMEOUT", 10)`; logs via `mojo.helpers.logit`.
3. `mojo/apps/phonehub/models/sms.py` — refactor `SMS.send()` (line 167): resolve `PhoneConfig.get_for_group(group)` once; keep `+1555…` test-number short-circuit BEFORE provider dispatch; branch on `config.provider` (`'mojo'` → `mojo_provider.send_sms(...)`, set `provider='mojo'`, stash remote payload in `metadata['remote']`; otherwise existing twilio path unchanged); config-error short-circuit when `provider='mojo'` but URL or key missing → row `failed` with `error_code='config_error'`, no HTTP call; update `provider` field `help_text` to `"twilio, aws, or mojo"`.
4. `bin/create_testproject` — regenerate test project migrations after the model change (per `.claude/rules/core.md`).
5. `tests/test_phonehub/sms_mojo_provider.py` — NEW. `@th.django_unit_test()` tests calling `SMS.send()` directly with `unittest.mock.patch('mojo.apps.phonehub.services.mojo_provider.requests.post')`. Setup deletes any existing `PhoneConfig` for the test group before creating one.
6. `docs/django_developer/phonehub/README.md` — extend Provider Configuration (line ~141) and Settings table (line ~119) for the `mojo` provider and `SMS_REMOTE_TIMEOUT`.
7. `docs/django_developer/phonehub/models.md` — extend PhoneConfig section (line ~168) with new fields, helpers, and provider value.
8. `docs/django_developer/phonehub/rest.md` — note that `POST /api/phonehub/sms/send` (line ~121) doubles as the provider-to-provider integration surface.
9. `CHANGELOG.md` — Unreleased `phonehub` entry.

### Design Decisions
- Mirror `mojo/helpers/geoip/mojo.py` precedent: `Authorization: apikey <token>`, bounded `requests` timeout, `logit` for failures, normalized return shape — same federation idiom as geoip.
- Reuse the existing `POST /api/phonehub/sms/send` endpoint as the provider surface; no new endpoints, no schema change on the remote.
- Backwards-compatible: if no `PhoneConfig` exists OR `provider != 'mojo'`, the existing twilio path runs unchanged. The refactor only adds a dispatch branch.
- Test number short-circuit (`+1555…`) stays ahead of provider dispatch so dev/test runs never hit the network even when configured for `mojo`.
- No silent failover: `provider='mojo'` failure marks the SMS `failed`. Explicit failover is out of scope.
- `from_number` only forwarded if the caller passed one — otherwise let the remote use its own default; local row stores whatever the remote echoes back.
- Lazy import of `mojo_provider` inside `SMS.send()` matches the existing `from mojo.apps.phonehub.services import twilio` lazy-import pattern.
- API key stored via `MojoSecrets` (encrypted) — never as a plain CharField. The `default`/`full` graphs on `PhoneConfig` already exclude `mojo_secrets` (config.py:67, 73), so the new secret rides on the existing exclusion.

### User Cases
- **Caller deployment with no Twilio creds** delegates all SMS to a central SMS mojo: `PhoneConfig(provider='mojo', mojo_remote_url=..., mojo_api_key=...)`, no Twilio settings needed.
- **Mixed deployment**: system-default `PhoneConfig(group=None, provider='twilio')` + per-group `PhoneConfig(group=X, provider='mojo')` routes group X's SMS through a different remote (e.g., a tenant-specific SMS mojo).
- **Existing twilio-only deployment** with no `PhoneConfig` or `PhoneConfig(provider='twilio')`: zero behavioral change.
- **Developer sending to `+1555…`**: works locally without ever calling the remote, regardless of provider config.
- **Operator validating credentials**: `PhoneConfig.test_connection()` returns success/failure without sending a real SMS, using a cheap GET to `/api/account/me` on the remote.
- **Compliance/billing centralization**: one mojo holds Twilio A2P registration + billing; all other deployments are downstream callers identified by distinct ApiKey rows (per-key rate limits enforced by existing ApiKey machinery on the remote).

### Edge Cases
- Remote returns 2xx with `{status: false, error: ...}` → `failed`, `error_code='remote_failed'`, error_message from body.
- Remote returns 2xx with non-JSON body → `error_code='remote_error'`, error_message contains the parse error.
- `mojo_remote_url` with trailing slash → stripped at save AND defensively at use time.
- Caller's ApiKey lacks `send_sms`/`comms` perms → remote 403 → `error_code='http_403'`, no retry.
- `+1555…` test number with `provider='mojo'` → handled locally, no HTTP call.
- Long remote latency → bounded by `SMS_REMOTE_TIMEOUT` (default 10s); SMS row goes `failed` with `error_code='timeout'`.
- Existing rows with `provider='twilio'` and existing twilio path → dispatch refactor preserves byte-for-byte behavior; covered by regression test.
- `MojoSecrets` accidentally surfaced in graph output → already excluded; new helper rides on existing graph exclusion. Test asserts non-exposure.
- DNS / connection refused → caught as `requests` exception → `error_code='remote_error'`, message includes exception text.

### Testing
- Success path → `tests/test_phonehub/sms_mojo_provider.py` (`test_send_mojo_provider_success`)
- HTTP 401 → `tests/test_phonehub/sms_mojo_provider.py` (`test_send_mojo_provider_http_401`)
- Timeout (`requests.Timeout`) → `tests/test_phonehub/sms_mojo_provider.py` (`test_send_mojo_provider_timeout`)
- Missing URL or api key → `tests/test_phonehub/sms_mojo_provider.py` (`test_send_mojo_provider_config_error`)
- Twilio regression (no config / `provider='twilio'`) → `tests/test_phonehub/sms_mojo_provider.py` (`test_send_twilio_path_unchanged`)
- `+1555…` short-circuit with mojo provider → `tests/test_phonehub/sms_mojo_provider.py` (`test_send_mojo_provider_test_number_short_circuit`)
- Secrets round-trip + graph non-exposure → `tests/test_phonehub/sms_mojo_provider.py` (`test_mojo_api_key_secret_roundtrip`)
- `PhoneConfig.test_connection()` mojo branch (success + 401) → `tests/test_phonehub/sms_mojo_provider.py` (`test_phone_config_test_connection_mojo`)

Run: `bin/run_tests --agent -t test_phonehub.sms_mojo_provider`.

### Docs
- `docs/django_developer/phonehub/README.md` — Provider Configuration section and Settings table get `mojo` provider entry + `SMS_REMOTE_TIMEOUT`.
- `docs/django_developer/phonehub/models.md` — PhoneConfig table gets `mojo_remote_url` field + `set_mojo_api_key`/`get_mojo_api_key` helpers + `'mojo'` provider value.
- `docs/django_developer/phonehub/rest.md` — annotation on `POST /api/phonehub/sms/send` noting it's also the provider-to-provider surface; ApiKey auth + required perms (`send_sms`, `comms`).
- `docs/web_developer/phonehub/README.md` — NO changes (client contract unchanged).
- `CHANGELOG.md` Unreleased — `phonehub` entry summarizing the new provider.

## Resolution

**Status**: resolved
**Date**: 2026-05-16
**Commits**: `aa49fbd` (feature), `f86138e` (security hardenings)

### What Was Built
Added a `mojo` provider option to `PhoneConfig` that delegates outbound SMS to a remote django-mojo instance over HTTP, authenticating with an `account.ApiKey` token. `SMS.send()` now dispatches by `PhoneConfig.provider`; twilio/aws/no-config paths are byte-for-byte unchanged. Mirrors the existing geoip mojo-as-provider precedent.

### Files Changed
- `mojo/apps/phonehub/models/config.py` — `'mojo'` choice on `PROVIDER_CHOICES`, new `mojo_remote_url` field, `set_mojo_api_key`/`get_mojo_api_key` helpers backed by `MojoSecrets`, `save()` strips trailing slash, `_test_mojo()` branch on `test_connection()` that validates the api key via `GET /api/account/me` with `allow_redirects=False`. Exception details are logged via `logit` and not echoed to callers.
- `mojo/apps/phonehub/services/mojo_provider.py` — NEW. POST client returning a twilio-shaped objict. `allow_redirects=False`. Error bodies sanitized to structured JSON `error`/`message` keys (or generic `HTTP <status>`) before being surfaced as `SMS.error_message`. Default timeout from `SMS_REMOTE_TIMEOUT` (10s).
- `mojo/apps/phonehub/models/sms.py` — `SMS.send()` dispatches by `PhoneConfig.get_for_group(group)`. `+1555` test-number short-circuit runs ahead of provider dispatch in both branches. Provider help_text updated to `"twilio, aws, or mojo"`.
- `mojo/apps/phonehub/migrations/0003_phoneconfig_mojo_remote_url_and_more.py` — generated migration.
- `tests/test_phonehub/sms_mojo_provider.py` — NEW, 8 tests, all green.
- `docs/django_developer/phonehub/{README.md, models.md, rest.md}` — provider config, fields, helpers, REST endpoint annotation, `SMS_REMOTE_TIMEOUT`.
- `docs/web_developer/phonehub/README.md` — note on mojo-provider `error_code` values in the SMS response.
- `CHANGELOG.md` — phonehub entry under Unreleased.

### Tests
- `tests/test_phonehub/sms_mojo_provider.py` — success, HTTP 401, timeout, missing-config short-circuit (no HTTP call), twilio regression, +1555 short-circuit with mojo provider configured, secret round-trip + graph non-exposure, `test_connection()` success/401/missing-credentials, `allow_redirects=False` assertion.
- Run: `bin/run_tests --agent -t test_phonehub.sms_mojo_provider` — 8/8 passing.
- Full suite: 2018 passing; 1 pre-existing flaky test in `test_realtime.ws_manager_online_status` (unrelated; due to shared Redis state between parallel modules; passes in isolation).

### Docs Updated
- `docs/django_developer/phonehub/README.md` — Provider Configuration list, new "Mojo Remote SMS Provider" subsection with end-to-end setup steps, `SMS_REMOTE_TIMEOUT` in Settings table.
- `docs/django_developer/phonehub/models.md` — `PhoneConfig` fields table, credential management examples, expanded `test_connection()` docstring, SMS `provider` field updated.
- `docs/django_developer/phonehub/rest.md` — annotation on `POST /api/phonehub/sms/send` noting it's the provider-to-provider integration surface.
- `docs/web_developer/phonehub/README.md` — mojo-provider error codes documented.
- `CHANGELOG.md` — Unreleased phonehub entry.

### Security Review
Three findings actioned in commit `f86138e`:
- **SSRF (Med)**: `allow_redirects=False` added to both the outbound POST in `mojo_provider.send_sms` and the GET in `_test_mojo`, so an attacker-controlled redirect on the upstream cannot widen the surface to internal addresses (e.g. IMDS at `169.254.169.254`). Operator-controlled URL surface remains (requires `manage_phone_config` / `manage_groups` to write `PhoneConfig`); URL allowlist deferred unless a multi-tenant deployment scenario emerges.
- **Error message leakage (Low)**: Remote error bodies are no longer copied verbatim into `SMS.error_message`. Only structured JSON `error`/`message` fields are surfaced (capped at 200 chars); raw body is logged via `logit` for operators. Eliminates the risk of Django debug pages / stack traces / internal hostnames leaking to anyone with `view_sms`.
- **`_test_mojo` exception leakage (Low)**: `str(e)` from `requests` exceptions no longer flows into the response `details` field. Generic message returned; full exception logged via `logit`.

Secret handling (encrypted `mojo_api_key` via `MojoSecrets`, graph exclusion verified by test), timeout enforcement on all paths, receiving-endpoint auth (unchanged), and input validation (JSON-encoded body forwarding) all rated **acceptable as-is** with **High confidence** by the reviewer.

### Follow-up
- None blocking. If a multi-tenant deployment ever lets group admins write `PhoneConfig`, consider adding a URL allowlist or RFC-1918 reject in `mojo_provider.send_sms` and `_test_mojo` as defense in depth on top of the redirect-blocking already in place.
- Status callbacks (provider mojo → caller webhook on delivered/failed) and caller-side polling for status refresh remain explicitly out of scope; file a follow-up request when needed.
