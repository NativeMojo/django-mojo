# Bouncer Public Messages (Contact Us / Support)

**Type**: request
**Status**: resolved
**Date**: 2026-04-22
**Priority**: medium

## Description

Add a public message intake (contact-us / support-request) built into the existing bouncer gate. A visitor lands on a bouncer-served HTML page, completes the standard bouncer challenge, and submits a form. The submission is persisted to a new `PublicMessage` model, a notification email is sent to any user flagged to receive public messages, and admins can list/read submissions through a normal RestMeta endpoint.

The form is driven by `kind` — initially `contact_us` and `support`, but extensible. Fields differ per kind; storage uses a shared model with a JSON `metadata` blob for kind-specific fields.

## Context

Right now there is no way for an unauthenticated visitor to reach us through the app itself — contact pages live on external marketing sites. The bouncer already does all the heavy lifting (bot gate, challenge, token issuance, white-label group resolution, rendered HTML page pipeline at `/auth` and `/register`), so layering a contact/support form onto that same flow gives real anti-spam for free without standing up a parallel framework.

Two kinds on day one:
- `contact_us` — generic "get in touch" form (name, email, company, message)
- `support` — general problem report (name, email, category, severity, message)

Group-scoped: if the bouncer resolves a group (via hostname or `?group=<uuid>`), the message is attached to that group and admins of that group are the ones notified.

## Acceptance Criteria

- A new model `account.PublicMessage` is migrated in with group FK (nullable), kind, common fields (name, email, subject, message), and a JSON `metadata` blob for kind-specific fields.
- A bouncer-gated HTML page serves the contact/support form at a configurable path (default `/contact`), reusing the bouncer challenge + token flow. `kind` is taken from the query string and switches which fields render.
- A public JSON POST endpoint accepts submissions. It requires a valid single-use bouncer token (`TokenManager.validate_and_consume`) in addition to rate limiting. Invalid/missing/consumed tokens are rejected.
- Per-kind server-side field validation runs before save. Unknown kinds return 400.
- Group resolution mirrors the existing bouncer pattern (`_resolve_group`) — hostname first, `?group=<uuid>` fallback.
- On save:
  - `incident.report_event` fires a low-level event (`security:bouncer:public_message`) for audit.
  - A notification email is sent to every `User` where `metadata.protected.notify_public_messages` is truthy (scoped to the resolved group if one is set; otherwise all system-wide flagged users).
  - A metric is recorded (`bouncer:public_messages:<kind>`).
- Admin RestMeta endpoint lists and reads submissions, gated by `VIEW_PERMS=["view_support", "security", "support"]` / `SAVE_PERMS=["manage_support", "security", "support"]`. No reply/resolve workflow in v1 — just status field (`open`, `closed`) and list/read.
- The `content_guard` helper runs against name / subject / message to reject spammy or abusive content at submit time (non-blocking log if the helper errors — fail-open).
- Text length limits enforced on all free-text fields to prevent large payload abuse.

## Investigation

**What exists**:
- Bouncer gate pattern at [views.py](mojo/apps/account/rest/bouncer/views.py): `_resolve_group`, `_auth_context`, `_serve_challenge`, `_serve_login`, shared challenge/pass-cookie flow used by both `/auth` and `/register` — same scaffolding applies here.
- Token flow at [assess.py](mojo/apps/account/rest/bouncer/assess.py) and [token_manager.py](mojo/apps/account/services/bouncer/token_manager.py). Tokens are signed, single-use via Redis nonce, IP + duid bound, default TTL 900s. `TokenManager.validate_and_consume` is the production call.
- Rate limiting: `@md.rate_limit` (fixed-window) and `@md.strict_rate_limit` (sliding-window). Existing bouncer endpoints use `ip_limit=60`. For public message submit, a stricter limit is appropriate (see below).
- `incident.report_event` is how both `assess.py` and `event.py` push events into the incident pipeline.
- `content_guard` helper (`mojo.helpers.content_guard`) exposes `check_text()` — drop-in for name/subject/message validation.
- `Ticket` model at [mojo/apps/incident/models/ticket.py](mojo/apps/incident/models/ticket.py) is the closest existing pattern (title/description/status/category/group) but is bound to the security/incident domain. Keeping public messages separate avoids polluting incident tooling with unauthenticated contact-form data.

**What changes**:
- `mojo/apps/account/models/public_message.py` — new model (see schema below).
- `mojo/apps/account/models/__init__.py` — export the new model.
- `mojo/apps/account/rest/bouncer/views.py` — add `on_contact_page` GET handler, serving `account/contact.html` through the same challenge/pass-cookie gate as `/auth`. Add per-kind template context helpers.
- `mojo/apps/account/rest/bouncer/public_message.py` — new file for the submit endpoint + admin RestMeta endpoint.
- `mojo/apps/account/rest/bouncer/__init__.py` — re-export the new module.
- `mojo/apps/account/templates/account/contact.html` — new template, rendered server-side, wired to `mojo-auth.js`-style bouncer client for token refresh.
- `mojo/apps/account/services/public_message.py` — helper for the notification fan-out (email + incident event + metric). Keeps the REST handler thin.
- `mojo/apps/account/migrations/` — regenerated via `bin/create_testproject`.
- `docs/django_developer/account/bouncer.md` (or equivalent) — describe the new endpoint, settings, and notification pattern.
- `docs/web_developer/` — document the new REST endpoint and query-param usage for the form page.
- `CHANGELOG.md` — log the addition.

**Constraints**:
- Bouncer tokens are **single-use**. The page must include a fresh token in the form; after submit it's burned. Repeat submits from the same page require either a page reload (new challenge) or a soft token-refresh endpoint — v1 requires reload, which matches how `/auth` already behaves.
- `notify_public_messages` lives under `metadata.protected` — this is user-editable JSON; the `protected` namespace has a convention of only being writable by admins. Confirm the flag sits under `protected` (per user's instruction) and that admin tooling updates it rather than end-users.
- No Python type hints. Use `request.DATA`. No `import logging` — use `logit`.
- Fail-open on geo, content_guard, and notification errors — a failed email must not block a submission. Hard-fail on token validation, rate limit, and kind validation.

**Related files**:
- `mojo/apps/account/rest/bouncer/views.py`
- `mojo/apps/account/rest/bouncer/assess.py`
- `mojo/apps/account/rest/bouncer/event.py`
- `mojo/apps/account/services/bouncer/token_manager.py`
- `mojo/apps/incident/models/ticket.py` (reference pattern only, not extended)
- `mojo/helpers/content_guard/`
- `docs/django_developer/core/rate_limiting.md`

## Model

`account.PublicMessage` (`mojo/apps/account/models/public_message.py`):

| Field | Type | Notes |
|---|---|---|
| `created` | `DateTimeField(auto_now_add=True, db_index=True)` | |
| `modified` | `DateTimeField(auto_now=True, db_index=True)` | |
| `group` | `FK(account.Group, null=True, SET_NULL)` | Resolved by bouncer, nullable for single-tenant |
| `kind` | `CharField(max_length=32, db_index=True)` | `contact_us`, `support`, future extensible |
| `name` | `CharField(max_length=120)` | Submitter name |
| `email` | `EmailField(max_length=254, db_index=True)` | Submitter email |
| `subject` | `CharField(max_length=255, blank=True)` | Optional per kind |
| `message` | `TextField()` | Free-form body, length-capped (e.g. 4000 chars) |
| `metadata` | `JSONField(default=dict, blank=True)` | Kind-specific fields (company, category, severity, etc.) |
| `status` | `CharField(max_length=32, default='open', db_index=True)` | `open`, `closed` |
| `ip_address` | `GenericIPAddressField(null=True)` | Captured at submit |
| `user_agent` | `CharField(max_length=512, blank=True)` | Captured at submit |

`RestMeta`:
- `VIEW_PERMS = ["view_support", "security", "support"]`
- `SAVE_PERMS = ["manage_support", "security", "support"]`
- `DELETE_PERMS = ["manage_support"]`
- `CAN_DELETE = True`
- `SEARCH_FIELDS = ["name", "email", "subject", "message"]`
- Graphs: `default` with `group: basic`

## Kind Field Schemas

Validated server-side. Unknown kinds → 400. Unknown fields → silently dropped into `metadata`.

**`contact_us`**:
- `name` (required, 1–120 chars)
- `email` (required, valid email)
- `company` (optional, ≤120 chars) → stored in `metadata.company`
- `message` (required, 1–4000 chars)

**`support`**:
- `name` (required, 1–120 chars)
- `email` (required, valid email)
- `category` (required, enum: `billing`, `account`, `bug`, `other`) → stored in `metadata.category`
- `severity` (required, enum: `low`, `normal`, `high`) → stored in `metadata.severity`
- `message` (required, 1–4000 chars)

Kind definitions should live as a dict in `mojo/apps/account/services/public_message.py` so the schema is the single source of truth for both the template renderer and the submit validator.

## Endpoints

| Method | Path | Description | Permission |
|---|---|---|---|
| GET | `/contact` (configurable) | Bouncer-gated HTML form page, renders per `?kind=...` | Public (bouncer gated) |
| POST | `account/bouncer/message` | Submit public message; requires valid bouncer token | Public + `@md.rate_limit` / `@md.strict_rate_limit` |
| GET/POST | `account/public_message` | RestMeta list + detail + update status | `view_support` / `manage_support` |
| GET/POST | `account/public_message/<int:pk>` | RestMeta detail | `view_support` / `manage_support` |

Rate limit for submit: `@md.strict_rate_limit("public_message_submit", ip_limit=5, ip_window=300)` — stricter than the generic bouncer endpoints because this writes persistent records and sends email.

## Settings

| Setting | Default | Purpose |
|---|---|---|
| `BOUNCER_CONTACT_PATH` | `contact` | URL path for the gated contact/support page |
| `BOUNCER_PUBLIC_MESSAGE_MAX_LENGTH` | `4000` | Cap on the `message` field at submit time |
| `PUBLIC_MESSAGE_NOTIFY_SUBJECT` | `"New {kind} message"` | Email subject template |
| `PUBLIC_MESSAGE_NOTIFY_TEMPLATE` | `account/public_message_notify.html` | Email template for admin notification |

User flag (per-user, in `User.metadata`):
- `metadata.protected.notify_public_messages` (bool) — when truthy, the user receives email notifications for every new public message. Scoped to the resolved group if the message has one; unscoped users receive all messages across groups.

## Tests Required

- `test_public_message_submit_contact_us` — happy path with valid bouncer token + fields, record saved, email fan-out fired, metric incremented.
- `test_public_message_submit_support` — happy path for support kind with category/severity in metadata.
- `test_public_message_rejects_invalid_kind` — unknown `kind` → 400.
- `test_public_message_rejects_missing_token` — no token → 403, no record saved.
- `test_public_message_rejects_invalid_token` — bad signature, expired, or IP mismatch → 403.
- `test_public_message_rejects_reused_token` — single-use enforcement via nonce.
- `test_public_message_rejects_missing_required_fields` — missing `message` or `email` → 400.
- `test_public_message_rejects_overlong_message` — payload > configured cap → 400.
- `test_public_message_rejects_invalid_email` — malformed email → 400.
- `test_public_message_content_guard_blocks_spam` — `content_guard.check_text` says block → 400.
- `test_public_message_group_scoping` — hostname-resolved group is attached; admins from other groups are not notified.
- `test_public_message_notification_targets` — only users with `metadata.protected.notify_public_messages` receive email; group scoping filters the list.
- `test_public_message_notification_failure_does_not_break_submit` — email send raises → message still saved, no 500.
- `test_public_message_rate_limit` — sixth submit from same IP in window → 429.
- `test_public_message_admin_list_requires_permission` — unauthenticated / lacks-perm returns 403; `view_support` can read; `manage_support` can update status.
- `test_public_message_admin_list_filters_by_group` — group-scoped admin only sees messages for their group (RestMeta owner/group scoping behavior).
- `test_contact_page_renders_kind_form` — GET `/contact?kind=support` renders the support form variant (company field absent, category/severity present); invalid kind redirects or defaults to `contact_us`.
- `test_contact_page_bouncer_gate_serves_challenge` — page obeys the same challenge/decoy flow as `/auth`.

## Out of Scope

- Reply or resolve workflow beyond status=open/closed. No message threading, no admin-reply-to-submitter.
- Attachments or file uploads on the form.
- Additional kinds beyond `contact_us` and `support`. Schema is extensible via the services dict, but no other kinds ship in v1.
- Slack / Discord / webhook notifications — v1 is email only.
- CAPTCHA beyond the existing bouncer challenge — the bouncer gate is the anti-bot surface.
- Changing the semantics of `metadata.protected` or building UI to toggle `notify_public_messages`. That flag is assumed to be set by existing admin tooling.
- Merging with or replacing the `incident.Ticket` pipeline. Public messages stay in the account app.

## Plan

**Status**: planned
**Planned**: 2026-04-22

### Objective
Add `account.PublicMessage` model, a bouncer-gated `/contact` HTML page with per-kind forms, a token-protected submit endpoint, and admin RestMeta list/detail — reusing the existing `@md.requires_bouncer_token` decorator, `user.send_template_email` helper, and the bouncer page scaffolding.

### Key Reuse
- `@md.requires_bouncer_token('public_message')` at [mojo/decorators/bouncer.py:25](mojo/decorators/bouncer.py:25) — validates signature/IP/duid/nonce and consumes the token. Do NOT hand-roll `TokenManager.validate_and_consume`.
- `user.send_template_email(template_name, context, group, kind)` at [mojo/apps/account/models/user.py:1025](mojo/apps/account/models/user.py:1025) — canonical templated email path.
- `User.objects.filter(metadata__contains={"protected": {"notify_public_messages": True}})` — existing pattern at [mojo/apps/account/services/inactive.py:51](mojo/apps/account/services/inactive.py:51).
- `_resolve_group`, `_auth_context`, `_serve_challenge` from [mojo/apps/account/rest/bouncer/views.py](mojo/apps/account/rest/bouncer/views.py) — reused verbatim for the contact page gate.
- RestMeta auto-filters list by `GROUP_FIELD` when a user only has group-scoped perms ([mojo/models/rest.py:373-411](mojo/models/rest.py:373)) — no manual filter code needed.

### Steps

1. **`mojo/apps/account/models/public_message.py`** (new) — `PublicMessage(models.Model, MojoModel)` with fields: `created`, `modified`, `group` (FK, nullable, SET_NULL), `kind`, `name`, `email`, `subject`, `message`, `metadata` (JSONField), `status`, `ip_address`, `user_agent`. `RestMeta` with `VIEW_PERMS=["view_support", "security", "support"]`, `SAVE_PERMS=["manage_support", "security", "support"]`, `DELETE_PERMS=["manage_support"]`, `CAN_DELETE=True`, `SEARCH_FIELDS`, `GROUP_FIELD="group"`, `list` + `default` graphs.

2. **`mojo/apps/account/models/__init__.py`** — `from .public_message import PublicMessage`.

3. **`mojo/apps/account/services/public_message.py`** (new) — single source of truth:
   - `KIND_SCHEMAS` dict — per-kind field list (required, max_len, enum choices).
   - `validate_submission(kind, data)` — returns `(common_dict, metadata_dict)` or raises `ValueError('field:reason')`. Runs `content_guard.check_text()` on name/subject/message; blocks only on `decision == 'block'`; fail-open on exceptions.
   - `notify_admins(message)` — queries flagged users, intersects with `group.members` if scoped, loops `user.send_template_email(...)` wrapped in try/except.
   - `render_context_for_kind(kind)` — builds template context (fields, labels, placeholders, enum choices).

4. **`mojo/apps/account/rest/bouncer/public_message.py`** (new):
   - `on_submit_public_message` — `@md.POST('account/bouncer/message')` + `@md.public_endpoint(...)` + `@md.strict_rate_limit('public_message_submit', ip_limit=5, ip_window=300)` + `@md.requires_bouncer_token('public_message')`. Resolves group via `_resolve_group`, validates via service, saves record, fires `incident.report_event('security:bouncer:public_message', level=3, ...)`, calls `metrics.record(f"bouncer:public_messages:{kind}", category="bouncer")`, calls `notify_admins(message)` wrapped in try/except.
   - `on_public_message` — `@md.URL('account/public_message')` + `@md.URL('account/public_message/<int:pk>')` + `@md.uses_model_security(PublicMessage)`. Delegates to `PublicMessage.on_rest_request(request, pk)`.

5. **`mojo/apps/account/rest/bouncer/views.py`** — add `on_contact_page` mirroring `on_register_page`: path from `settings.get_static('BOUNCER_CONTACT_PATH', 'contact')` as absolute `/contact`; same signature-cache → pass-cookie → pre-screen → decoy/challenge/serve flow; `_serve_contact(request, group, kind)` merges `render_context_for_kind(kind)` with `_auth_context(...)` and renders `account/contact.html`; `_serve_challenge(..., page_type='public_message')` so issued tokens are scoped correctly; invalid kind falls back to `contact_us`.

6. **`mojo/apps/account/rest/bouncer/__init__.py`** — `from .public_message import *`.

7. **`mojo/apps/account/templates/account/contact.html`** (new) — extends `auth_base.html`. Fields rendered from context `fields` list so both kinds share one template. Inline JS lifts `bouncer_token` from `window.__MOJO_BOUNCER__` and POSTs to `account/bouncer/message`.

8. **`mojo/apps/account/templates/account/public_message_notify.html`** (new) — admin notification email template (kind, submitter, body, group label, admin link).

9. **Run `bin/create_testproject`** after step 1 to regenerate migrations.

10. **`docs/django_developer/account/bouncer.md`** — new "Public Messages" section (endpoint, settings, notify flag, extending with new kinds, `BOUNCER_REQUIRE_TOKEN=True` guidance).

11. **`docs/web_developer/account/public_messages.md`** (new) — REST contract for `GET /contact?kind=...` and `POST account/bouncer/message`, rate limits, error codes.

12. **`CHANGELOG.md`** — one-line entry.

### Design Decisions

- **Reuse `@md.requires_bouncer_token`** — single enforcement point, respects `BOUNCER_REQUIRE_TOKEN` rollout flag, and scopes tokens by `page_type='public_message'`.
- **Kind schemas live in `services/public_message.py`** as a dict — one place to add a new kind; consumed by both validator and template context.
- **Synchronous email fan-out wrapped in try/except** — v1 volume is low. If N grows, move to `jobs.publish(...)` (documented, not built).
- **Group resolution via existing `_resolve_group`** — no new resolution code; hostname → `?group=<uuid>` fallback chain is free.
- **`GROUP_FIELD="group"`** — framework auto-filters admin list for group-scoped admins.
- **Status is just `open`/`closed`** — no reply threading, notes, or resolution workflow in v1.
- **content_guard failure → fail-open** — moderation hiccups must not block legitimate contact submissions.

### Edge Cases

- **`BOUNCER_REQUIRE_TOKEN=False`** (default) — decorator logs invalid tokens but allows through. Doc calls out that production deployments should flip this to `True`.
- **Group-scoped message with no group-flagged admins** → silently no email; message still saved and visible to system-level admins via RestMeta.
- **No group resolved + system-wide flagged user** → receives cross-tenant submissions. Documented behavior.
- **Oversized message** → validator rejects → 400 `field:message:too_long`.
- **Unknown kind** → 400 `field:kind:invalid`.
- **Duplicate submit (token reused)** → nonce already consumed → 403 `nonce_consumed`. Matches `/auth` behavior.
- **content_guard exception** → logged at warning level, submission proceeds.
- **Email send failure for one recipient** → logged per-recipient, loop continues.

### Testing → `tests/test_account/`

- `test_public_message_submit.py`:
  - `test_submit_contact_us_happy_path` — valid token + fields → record saved, metric recorded, email fan-out fired.
  - `test_submit_support_happy_path` — category/severity land in metadata.
  - `test_rejects_invalid_kind` → 400.
  - `test_rejects_missing_token` (with `BOUNCER_REQUIRE_TOKEN=True`) → 403, no record.
  - `test_rejects_invalid_token` (bad sig / expired / IP mismatch) → 403.
  - `test_rejects_reused_token` — single-use nonce.
  - `test_rejects_missing_required_fields` → 400.
  - `test_rejects_overlong_message` → 400.
  - `test_rejects_invalid_email` → 400.
  - `test_content_guard_blocks_spam` → 400.
  - `test_group_scoping_of_recipients` — hostname-resolved group attaches; flagged admins from other groups are not emailed.
  - `test_notification_failure_does_not_break_submit` — email raises → record still saved.
  - `test_rate_limit` — sixth submit in 5-min window → 429.
- `test_public_message_admin.py`:
  - `test_admin_list_requires_permission` — unauth → 403; `view_support` can read; `manage_support` can update status.
  - `test_admin_list_filters_by_group` — group-scoped admin only sees their group's messages.
  - `test_delete_requires_manage_support`.
- `test_contact_page.py`:
  - `test_contact_page_renders_contact_us_form` — default kind.
  - `test_contact_page_renders_support_form` — `?kind=support`.
  - `test_contact_page_invalid_kind_falls_back` — `?kind=garbage` → renders `contact_us`.
  - `test_contact_page_bouncer_gate_serves_challenge` — first-visit flow matches `/auth`.

All tests use `testit` with `@th.django_unit_test()`, `opts.client` for HTTP, `th.server_settings(BOUNCER_REQUIRE_TOKEN=True)` for hard-fail cases, and `TokenManager.issue(...)` for token generation.

### Docs

- `docs/django_developer/account/bouncer.md` — add "Public Messages" section.
- `docs/web_developer/account/public_messages.md` — new file covering the REST contract.
- `CHANGELOG.md` — one-line entry.

## Resolution

**Status**: resolved
**Date**: 2026-04-22

### What Was Built
New unauthenticated contact/support intake built into the existing bouncer gate. Visitors hit `/contact[?kind=contact_us|support]`, complete the bouncer challenge, and POST to `account/bouncer/message` with a single-use bouncer token. Submissions land in `account.PublicMessage`, fire an incident event + metric, and email every User flagged with `metadata.protected.notify_public_messages=True` (group-scoped if the bouncer resolved a group). Admin RestMeta surface at `account/public_message` gated by `view_support` / `manage_support` / `security` / `support`.

### Files Changed
- `mojo/apps/account/models/public_message.py` — new PublicMessage model
- `mojo/apps/account/models/__init__.py` — export
- `mojo/apps/account/services/public_message.py` — KIND_SCHEMAS, validate_submission, notify_admins, render_context_for_kind
- `mojo/apps/account/rest/bouncer/public_message.py` — submit + admin RestMeta endpoints
- `mojo/apps/account/rest/bouncer/__init__.py` — re-export
- `mojo/apps/account/rest/bouncer/views.py` — `on_contact_page`, `_serve_contact`, challenge redirect path
- `mojo/apps/account/templates/account/contact.html` — new contact/support form page
- `mojo/apps/account/migrations/0040_publicmessage.py` — schema
- `mojo/apps/aws/seeds/email_templates/public_message_notify.json` — admin notification email

### Tests
- `tests/test_public_messages/1_submit.py` — submit happy paths, kind/field validation, token enforcement, content_guard block, rate limit, notification failure isolation
- `tests/test_public_messages/2_notify.py` — fan-out target selection (system-wide vs group-scoped), per-recipient failure isolation
- `tests/test_public_messages/3_admin.py` — RestMeta perms, group-scoped filtering, delete requires manage_support
- `tests/test_public_messages/4_contact_page.py` — page renders per kind, invalid kind falls back, service schema contract
- Run: `bin/run_tests --agent -t test_public_messages` — 25/25 passing
- Full suite: 1749/1805 pass (56 pre-existing skips), no regressions

### Docs Updated
- `docs/django_developer/account/bouncer.md` — new "Public Messages" section
- `docs/django_developer/account/README.md` — index entry
- `docs/django_developer/account/auth_pages.md` — URL routes + settings table
- `docs/django_developer/helpers/settings_reference.md` — BOUNCER and PUBLIC_MESSAGE namespaces
- `docs/web_developer/account/public_messages.md` — new REST contract doc
- `docs/web_developer/account/README.md` — index entry
- `CHANGELOG.md`

### Security Review
Two findings from the security-review agent:
- **Fixed**: incident.report_event no longer interpolates submitter-supplied email into the event `details` string. Email now goes through kwargs and is sanitized by `reporter._create_event_dict`.
- **Documented**: `BOUNCER_REQUIRE_TOKEN` default-False (log-only) means production deployments that never flip the flag rely solely on the 5-per-5-min rate limit for bot defense. Already called out in `bouncer.md`.

Informational findings (`metadata__contains` is Postgres-specific; `notify_public_messages` lives in user-writable JSON but only grants email subscription — no elevated access) are documented but not changed.

### Follow-up
- Web-mojo admin UI: `web-mojo/planning/requests/public-messages-admin.md` — read/triage admin interface in the Messaging nav.
- Consider a startup warning when `BOUNCER_REQUIRE_TOKEN=False` on deployments that expose the submit endpoint (security review recommendation).
