---
# id is assigned by /scope on pickup — leave it blank
id: ITEM-015
type: feature
title: Configurable outbound webhook signature header & User-Agent
priority: P2
effort: S
owner: backend
opened: 2026-07-06
depends_on: []
related: []
links: []
---

# Configurable outbound webhook signature header & User-Agent

## What & Why
Outbound webhooks (sent via `mojo.apps.jobs.publish_webhook`) currently attach
two headers that reveal the backend is built on django-mojo:

- `X-Mojo-Signature` — the HMAC-SHA256 signature header; the name is hardcoded
  as `WEBHOOK_SIGNATURE_HEADER = "X-Mojo-Signature"` in
  `mojo/helpers/crypto/sign.py:7`.
- `User-Agent: Django-MOJO-Webhook/1.0` — hardcoded in
  `mojo/apps/jobs/__init__.py:384`.

Operators may not want to advertise their framework to third-party webhook
receivers (fingerprinting / recon aid). Both values should be overridable via
Django settings. Defaults stay exactly as they are today, so existing
deployments and webhook consumers are unaffected unless an operator opts in.

The original request named only `X-Mojo-Signature`; the User-Agent was found
during exploration and is included because renaming the signature header alone
would not achieve the stated goal (the User-Agent still says "Django-MOJO").
If that's unwanted, trim it at scope time.

## Acceptance Criteria
- [ ] The `WEBHOOK_SIGNATURE_HEADER` Django setting (default
      `"X-Mojo-Signature"`) controls the signature header name on outbound
      webhook requests.
- [ ] Inbound verification (`verify_signed_request` in
      `mojo/helpers/request.py:103`) honors the same setting as its default
      header, so send and verify agree without callers passing `header=`.
- [ ] The `JOBS_WEBHOOK_USER_AGENT` Django setting (default
      `"Django-MOJO-Webhook/1.0"`) controls the outbound User-Agent — this
      name is already documented in `docs/django_developer/jobs/settings.md:73`
      and `jobs/webhooks.md:213` but the code never reads it; honoring it
      fixes that doc/code mismatch.
- [ ] Webhook log sanitization masks the *configured* signature header name,
      not just the literal `x-mojo-signature`
      (`mojo/apps/jobs/handlers/webhook.py:269`).
- [ ] With no settings defined, behavior is byte-for-byte identical to today;
      all existing tests pass unchanged.
- [ ] Tests cover the override path (note testit server isolation — use
      `th.server_settings(...)`, not `override_settings`).
- [ ] Docs updated in both tracks (django_developer + web_developer webhook
      signing docs) to state the header name is configurable and warn that
      renaming it must be coordinated with webhook consumers.

## Investigation
**What exists**
- Constant definition: `mojo/helpers/crypto/sign.py:7`
  (`WEBHOOK_SIGNATURE_HEADER = "X-Mojo-Signature"`).
- Send site: `mojo/apps/jobs/handlers/webhook.py:109` —
  `headers[WEBHOOK_SIGNATURE_HEADER] = sign_for_group(group, signed_body)`;
  constant imported at line 14.
- Base headers (incl. User-Agent): `mojo/apps/jobs/__init__.py:382-387`.
- Verify site: `mojo/helpers/request.py:103-126` —
  `verify_signed_request(request, secret, header=WEBHOOK_SIGNATURE_HEADER)`;
  builds the `HTTP_*` META key from the header name.
- Log masking: `mojo/apps/jobs/handlers/webhook.py:269` — literal
  `'x-mojo-signature'` in a `sensitive_headers` set.
- Signature algorithm (unchanged by this feature): HMAC-SHA256 over canonical
  JSON (`sort_keys`, compact separators), per-group secret via
  `group.get_webhook_secret(auto_create=True)`
  (`mojo/helpers/crypto/sign.py:46-55`, `webhook.py:20-25`).
- Settings idiom: `from mojo.helpers import settings` →
  `settings.get_static(name, default)` for module-level constants,
  `settings.get(name, default)` at runtime
  (`mojo/helpers/settings/helper.py:145-163`). Direct precedent for a
  configurable header name: `mojo/middleware/cors.py:3` —
  `DUID_HEADER = settings.get_static('DUID_HEADER', 'X-Mojo-UID')`.
- Prior art: `planning/done/group_webhook_signing.md` designed the current
  signing scheme.

**What changes (file-level)**
- `mojo/helpers/crypto/sign.py` — derive `WEBHOOK_SIGNATURE_HEADER` from
  settings with the current value as default.
- `mojo/apps/jobs/__init__.py:384` — User-Agent from settings.
- `mojo/apps/jobs/handlers/webhook.py` — send site follows the constant
  (line 109 via the import at line 14); sensitive-headers mask (line 269) must
  include the configured name.
- `mojo/helpers/request.py` — default `header=` param follows the setting.
- Tests: `tests/test_jobs/test_signed_webhook.py`,
  `tests/test_account/test_webhook_signer.py` — add override coverage;
  existing fixtures (`HTTP_X_MOJO_SIGNATURE`, lines 77/100/115/154) keep
  exercising the default.
- Docs: `docs/django_developer/account/webhook_signing.md`,
  `docs/django_developer/jobs/publishing.md`,
  `docs/django_developer/helpers/crypto.md`,
  `docs/web_developer/account/webhook_signing.md`, `CHANGELOG.md`.

**Constraints**
- Backwards compatibility is hard: defaults must remain `X-Mojo-Signature` and
  `Django-MOJO-Webhook/1.0`.
- Import-time capture: `webhook.py:14` does
  `from mojo.helpers.crypto.sign import ... WEBHOOK_SIGNATURE_HEADER`, and
  `verify_signed_request` binds it as a default arg at definition time. If the
  constant is resolved via `get_static` at import, the setting is effectively
  deploy-time (fine for a header name — but /scope should decide static-at-import
  vs. runtime `settings.get()` lookup and make tests match; testit's
  `th.server_settings` reloads the server process, which re-imports, so either
  approach is testable).
- This is branding/obscurity, not a security boundary — signature computation
  and verification logic are unchanged. Do not weaken verification defaults.
- Renaming the header is an operator-facing contract change with *their*
  webhook consumers; docs must say the receiver has to read the same name.

**Out of scope**
- Inbound/API headers (`X-Mojo-UID` is already configurable via `DUID_HEADER`).
- Webhook payload contents.
- Changing the signing algorithm or secret handling.

## Plan

### Goal
Let operators override the outbound-webhook signature header name and
User-Agent via Django settings (`WEBHOOK_SIGNATURE_HEADER`,
`JOBS_WEBHOOK_USER_AGENT`); with neither setting defined, behavior is
byte-for-byte identical to today.

### Context — what exists
- `mojo/helpers/crypto/sign.py` — already does
  `from mojo.helpers.settings import settings` (line 4). Line 7:
  `WEBHOOK_SIGNATURE_HEADER = "X-Mojo-Signature"` — the only definition.
  Contains `generate_signature` (HMAC-SHA256, hex), `verify_signature`
  (constant-time compare), `sign_for_group(group, body_bytes)` (auto-mints the
  Group webhook secret via `get_webhook_secret(auto_create=True)`).
- `mojo/apps/jobs/handlers/webhook.py` — line 14:
  `from mojo.helpers.crypto.sign import sign_for_group, WEBHOOK_SIGNATURE_HEADER`.
  `_canonical_body` (lines 20-25): sorted-keys compact JSON, same bytes hashed
  and sent. Signed path ~95-115: `headers[WEBHOOK_SIGNATURE_HEADER] =
  sign_for_group(group, signed_body)` (line 107), then
  `job.metadata['headers_sent'] = _sanitize_headers(headers)`.
  `_sanitize_headers` (lines 252-283): hardcoded `sensitive_headers` set
  including the literal `'x-mojo-signature'` (line 269); masked values render
  as `value[:4]...value[-4:]`.
- `mojo/apps/jobs/__init__.py` — `publish_webhook()` (lines 304-426). Header
  defaults at 382-385 hardcode `'User-Agent': 'Django-MOJO-Webhook/1.0'`;
  caller `headers` merged over defaults at 386-387 (caller wins). Job payload
  at 395-402: `{url, data, headers, timeout, webhook_id, sign_group_id}`.
  Enqueues via `publish(func='mojo.apps.jobs.handlers.webhook.post_webhook',
  payload=payload, ...)` at line 414. Both settings idioms already used in
  this module (`settings.get_static` at module top for `JOBS_CHANNELS` /
  `JOBS_WEBHOOK_MAX_RETRIES`; runtime `settings.get('JOBS_WEBHOOK_MAX_TIMEOUT',
  300)` at line 409). Docstring line ~329 and example ~359 mention
  `X-Mojo-Signature` by name.
- `mojo/helpers/request.py` — line 6:
  `from mojo.helpers.crypto.sign import verify_signature, WEBHOOK_SIGNATURE_HEADER`.
  `verify_signed_request(request, secret, header=WEBHOOK_SIGNATURE_HEADER)`
  (lines 103-126): derives META key
  `"HTTP_" + header.replace("-", "_").upper()` (line 120), falls back to
  `request.headers.get(header)`; returns False (never raises) on missing
  secret/header/mismatch.
- Settings machinery: `settings.get_static(name, default)` reads live
  `django.conf.settings` on every call, file-based only (no DB/Redis);
  `settings.get(...)` adds a DB-backed lookup first. Configurable-header
  precedent: `mojo/middleware/cors.py:3` —
  `DUID_HEADER = settings.get_static('DUID_HEADER', 'X-Mojo-UID')`.
- `JOBS_WEBHOOK_USER_AGENT` is already documented as a setting
  (`docs/django_developer/jobs/settings.md:73`,
  `docs/django_developer/jobs/webhooks.md:213`, reference file
  `mojo/apps/jobs/settings.py:39`) but nothing reads it — doc/code mismatch
  this item fixes.
- Tests are in-process (no test server, no `th.server_settings`):
  `tests/test_jobs/test_signed_webhook.py` builds stub Jobs via `_build_job`
  and `mock.patch.object(webhook_handler, "requests")`, asserting on
  `mock_requests.post.call_args` (see `test_handler_injects_signature_header`,
  lines 78-135). `tests/test_account/test_webhook_signer.py` uses a
  `_FakeRequest(body, META)` helper and calls `verify_signed_request`
  directly; fixtures use `HTTP_X_MOJO_SIGNATURE` META keys.
- Audit result: the signature header name and the User-Agent are the ONLY
  framework identifiers on outbound webhooks — payload envelope and body
  contain no mojo strings; no other `X-Mojo-*` header is sent outbound.

### Changes — what to do
1. `mojo/helpers/crypto/sign.py` — keep the line-7 constant (back-compat for
   existing importers). Add an accessor (no type hints — core rule):
   ```python
   def get_signature_header():
       """Effective webhook signature header name.

       Overridable via the WEBHOOK_SIGNATURE_HEADER Django setting; falls
       back to the X-Mojo-Signature default when unset or empty.
       """
       return settings.get_static("WEBHOOK_SIGNATURE_HEADER", WEBHOOK_SIGNATURE_HEADER) or WEBHOOK_SIGNATURE_HEADER
   ```
2. `mojo/apps/jobs/handlers/webhook.py` —
   - line 14: import `get_signature_header` instead of the constant
     (`from mojo.helpers.crypto.sign import sign_for_group, get_signature_header`;
     line 107 is the constant's only use in this file).
   - line 107: `headers[get_signature_header()] = sign_for_group(group, signed_body)`.
   - `_sanitize_headers`: after building the literal set, add
     `sensitive_headers.add(get_signature_header().lower())` — keep the
     literal `'x-mojo-signature'` entry so default-named logs stay masked
     across a rename.
3. `mojo/helpers/request.py` —
   - line 6: import `get_signature_header` instead of the constant.
   - line 103: `def verify_signed_request(request, secret, header=None):` and
     resolve `if header is None: header = get_signature_header()` first thing;
     explicit `header=` still wins. Update the docstring to mention the
     setting.
4. `mojo/apps/jobs/__init__.py` —
   - line 384: `'User-Agent': settings.get_static('JOBS_WEBHOOK_USER_AGENT', 'Django-MOJO-Webhook/1.0')`.
   - Docstring ~329 / example ~359: note the signature header name defaults to
     `X-Mojo-Signature` and is configurable via `WEBHOOK_SIGNATURE_HEADER`.
5. Tests — see Tests section.
6. Docs — see Docs section.
7. `CHANGELOG.md` — entry under unreleased: signature header + User-Agent now
   configurable; `JOBS_WEBHOOK_USER_AGENT` now actually honored.

### Design decisions
- Setting named `WEBHOOK_SIGNATURE_HEADER` (bare, no prefix) — matches the
  existing constant name for discoverability and follows the `DUID_HEADER`
  precedent (a configurable header name, bare). Rejected:
  `JOBS_WEBHOOK_SIGNATURE_HEADER` (signing/verify spans helpers + jobs +
  account, not jobs-only); `MOJO_WEBHOOK_SIGNATURE_HEADER` (breaks symmetry
  with the constant and the DUID precedent).
- `JOBS_WEBHOOK_USER_AGENT` — already the documented name; honoring it fixes
  the doc/code mismatch. Rejected: inventing a new name.
- Per-call `get_static` resolution, not import-time capture — identical in
  production (Django settings are process-constant) but keeps send, verify,
  and masking on one source of truth and makes the override testable
  in-process without module-reload hacks. `get_static` (file-only) over
  `get` (DB-backed): branding is deploy-time config, and this avoids a
  DB/Redis round-trip on every webhook send (performance rule).
- Keep the `WEBHOOK_SIGNATURE_HEADER` constant exported — existing importers
  (request.py, tests, downstream user code) keep working; the constant now
  documents the default while the accessor gives the effective value.
- `header=None` sentinel in `verify_signed_request` rather than binding the
  accessor at def-time — resolves per call, keeping verify's default in sync
  with the send side under any setting.
- Masking adds the configured name and keeps the literal — logs written under
  either name stay masked; costs one set-add per send.
- Caller-supplied `headers=` still overrides the User-Agent default (merge
  order at 386-387 unchanged) — the setting only replaces the built-in
  default.

### Edge cases & risks
- Setting present but empty/None → accessor's `or` fallback returns the
  default; an empty header name can never be emitted (fail-safe).
- Custom names flow through META derivation fine — request.py:120 handles any
  hyphenated name (`X-Acme-Sig` → `HTTP_X_ACME_SIG`).
- Mid-queue rename deploy: User-Agent is baked into the job payload at
  publish time; the signature header is resolved at delivery time. Each
  reflects the setting active in its process at that moment — acceptable
  (settings changes imply redeploy); documented, not coded around.
- Renaming the header is an operator↔consumer contract change —
  web_developer docs must warn consumers to confirm the actual name with the
  API operator.
- Security unchanged: algorithm, secret handling, and fail-closed verify
  behavior untouched; log masking is strictly widened, never narrowed.
- Existing tests/fixtures importing the constant or using
  `HTTP_X_MOJO_SIGNATURE` remain valid because defaults are unchanged.

### Tests
Override idiom for these in-process tests: set the attribute directly on
`django.conf.settings` inside the test with try/finally restore
(`delattr`) — the real `get_static` path then reads it. These tests never
touch the test server, matching both files' existing in-process mock style
(`django.test.override_settings` stays unused per testing rules). Every
assert carries a descriptive message.

- `tests/test_jobs/test_signed_webhook.py::test_signature_header_setting_override`
  — with `WEBHOOK_SIGNATURE_HEADER = "X-Acme-Signature"`, reuse the
  mocked-`requests` flow from `test_handler_injects_signature_header`:
  assert `X-Acme-Signature` present with HMAC equal to
  `generate_signature(expected_body, secret)`; assert `X-Mojo-Signature`
  absent; assert `job.metadata['headers_sent']['X-Acme-Signature']` is masked
  (truncated `xxxx...yyyy` form, not the full hex).
- `tests/test_jobs/test_signed_webhook.py::test_publish_webhook_user_agent_setting`
  — `mock.patch("mojo.apps.jobs.publish")` (bare name in that module's
  namespace) to capture the enqueued payload without Redis. With
  `JOBS_WEBHOOK_USER_AGENT = "Acme-Hooks/2.0"` →
  `payload['headers']['User-Agent'] == "Acme-Hooks/2.0"`; after restore →
  default `Django-MOJO-Webhook/1.0`; with explicit
  `headers={'User-Agent': 'Caller/1.0'}` the caller value wins over the
  setting.
- `tests/test_account/test_webhook_signer.py::test_verify_uses_configured_header`
  — with setting `X-Acme-Signature`: a `_make_request` carrying
  `HTTP_X_ACME_SIGNATURE` verifies True with no `header=` arg; a request
  carrying only `HTTP_X_MOJO_SIGNATURE` verifies False (proves the default
  tracks the setting).
- Defaults are already locked by the existing tests in both files — no
  changes to them. Baseline suite run before any edit per
  `.claude/rules/build-baseline.md`.

### Docs
- `docs/django_developer/helpers/settings_reference.md` — add
  `WEBHOOK_SIGNATURE_HEADER` entry; also add the currently-missing
  `JOBS_WEBHOOK_USER_AGENT` row (webhook settings live near lines 194-196).
- `docs/django_developer/account/webhook_signing.md` — header name is
  configurable via `WEBHOOK_SIGNATURE_HEADER`, default `X-Mojo-Signature`.
- `docs/django_developer/jobs/settings.md` — UA row exists (line 73); add a
  `WEBHOOK_SIGNATURE_HEADER` cross-reference near the webhook table.
- `docs/django_developer/jobs/webhooks.md` and
  `docs/django_developer/jobs/publishing.md` — update `X-Mojo-Signature`
  mentions to "default, configurable".
- `docs/django_developer/helpers/crypto.md` — document
  `get_signature_header()` beside the constant (lines ~59-62).
- `docs/web_developer/account/webhook_signing.md` — consumer-facing note: the
  header defaults to `X-Mojo-Signature` but the operator may configure a
  different name; confirm with your provider.
- `CHANGELOG.md` — behavior/config change entry.

### Open questions
- none

## Notes
- 2026-07-06 (build baseline): `bin/run_tests --agent` → `var/test_failures.json`
  status=passed, total 2292, passed 2236, failed 0, skipped 56. GREEN before any
  edit. (Terminal table lists test_incident 243 / test_security 82 as "failed",
  but those are opt-in `--full`-only modules — 0.0s, not run in the default suite;
  the JSON records them as failed:0 and the build-baseline rule excludes them.)
- 2026-07-06 (build): implemented per plan. Post-build test-runner GREEN
  (total 2292→2295, passed 2236→2239 — the +3 are the new tests; skipped 56
  unchanged, failures=[]). security-review PASSED (fail-closed verify preserved;
  log-masking strictly widened; malformed header-name setting is caught by
  `requests` → failed job, and is operator config not attacker input).
- 2026-07-06 (build): test-override mechanism refined vs. the acceptance-criteria
  note. That note said use `th.server_settings`, but both target test files are
  pure in-process (no `opts.client`, no server), so `th.server_settings` (which
  reloads the *server* process) would not affect the in-process `get_static`
  read. Used a local `_override_setting` contextmanager doing direct
  setattr/restore on `django.conf.settings` (the Plan's Tests section specified
  this). `override_settings` stays unused per the testing rules.
- 2026-07-06 (build): docs-updater caught extra mentions the first commit missed
  — `helpers/request.md` (verify signature `header=None`), the self-contradicting
  Escape Hatch code sample in `account/webhook_signing.md`,
  `account/webhook_subscriptions.md`, `web_developer/account/README.md`, and the
  `mojo/apps/jobs/settings.py` reference module. Committed in a983d3e.
- 2026-07-06 (scope): audit also found mojo-identifying User-Agents on
  outbound *scraper* requests — `Mojo-Assistant/1.0`
  (`mojo/apps/assistant/services/tools/web.py:17`,
  `.../tools/docs.py:13`) and `MojoLinkPreview/1.0`
  (`mojo/apps/shortlink/services/scraper.py:18`). User decided to leave
  those as-is (not webhooks, not a concern). Out of scope here.

## Resolution
- closed: 2026-07-06
- branch: main
- commits: 1bb2a9a (feat + tests + docs), a983d3e (docs sweep)
- files changed: (this item only — see the itemized list below; ITEM-015 commits 1bb2a9a + a983d3e)
  - mojo/helpers/crypto/sign.py (new get_signature_header accessor)
  - mojo/helpers/request.py (verify_signed_request header=None → accessor)
  - mojo/apps/jobs/handlers/webhook.py (send site + log-mask follow the accessor)
  - mojo/apps/jobs/__init__.py (User-Agent from JOBS_WEBHOOK_USER_AGENT; docstring)
  - mojo/apps/jobs/settings.py (reference module: doc the two settings)
  - docs: django_developer/{helpers/crypto.md, helpers/settings_reference.md,
    helpers/request.md, jobs/settings.md, jobs/webhooks.md, jobs/publishing.md,
    account/webhook_signing.md, account/webhook_subscriptions.md};
    web_developer/account/{webhook_signing.md, README.md}
  - CHANGELOG.md
- tests added:
  - tests/test_jobs/test_signed_webhook.py::test_signature_header_setting_override
  - tests/test_jobs/test_signed_webhook.py::test_publish_webhook_user_agent_setting
  - tests/test_account/test_webhook_signer.py::test_verify_uses_configured_header
