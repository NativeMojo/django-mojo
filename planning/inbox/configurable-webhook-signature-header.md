---
# id is assigned by /scope on pickup — leave it blank
id:
type: feature
title: Configurable outbound webhook signature header & User-Agent
priority: P2
effort:
owner:
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
- [ ] A documented Django setting (name chosen by /scope — e.g.
      `WEBHOOK_SIGNATURE_HEADER` or `MOJO_WEBHOOK_SIGNATURE_HEADER`,
      default `"X-Mojo-Signature"`) controls the signature header name on
      outbound webhook requests.
- [ ] Inbound verification (`verify_signed_request` in
      `mojo/helpers/request.py:103`) honors the same setting as its default
      header, so send and verify agree without callers passing `header=`.
- [ ] A documented Django setting (e.g. `MOJO_WEBHOOK_USER_AGENT`, default
      `"Django-MOJO-Webhook/1.0"`) controls the outbound User-Agent.
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
<!-- PLAN PENDING — /scope fills this section. While this marker is present the item
is UNPLANNED and /build MUST refuse it. Delete this comment when the plan is complete. -->

_Write a complete, self-contained design here — enough that a fresh session can
`/build` it cold, without re-deriving anything. Fill every subsection._

### Goal
[One sentence.]

### Context — what exists
[The recon a builder would otherwise redo: relevant files with paths and
`file:line` refs, current behavior, key snippets, helpers/patterns to reuse.]

### Changes — what to do
1. `path` — [exact change and why]
2. `path` — [...]

### Design decisions
- [decision] — [rationale; alternatives rejected]

### Edge cases & risks
- [case] — [how it's handled]

### Tests
- [scenario] -> `test file`

### Docs
- `doc` — [what changes]

### Open questions
- Exact setting names (`WEBHOOK_SIGNATURE_HEADER` matching the constant, or
  `MOJO_`-prefixed) — /scope decides; follow existing naming precedent.

## Notes
[Scratch space — anything not part of the plan.]

## Resolution
- closed: YYYY-MM-DD
- branch:
- files changed:
- tests added:
