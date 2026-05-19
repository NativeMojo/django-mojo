# Auth switcher preserves redirect/back params

**Type**: request
**Status**: resolved
**Date**: 2026-05-18
**Priority**: medium

## Description
When a user lands on the auth page with `?redirect=`, `?next=`, `?returnTo=`,
or `?back=` query params and clicks the cross-link to switch between login
and register, those params must be preserved on the destination page so the
post-auth redirect (and the hero "Back to website" link) continue to honor
the original caller's intent.

Today both switcher links are static, server-rendered hrefs that only carry
the optional `?group_uuid=` param — so a user arriving at
`/auth?redirect=/dashboard&back=/site` and clicking "Create one" lands on
`/register?group_uuid=...` with no `redirect` or `back`, and after a
successful registration they're sent to `AUTH_SUCCESS_REDIRECT` (default `/`)
instead of `/dashboard`.

## Context
- `_auth_context()` in [bouncer/views.py:219](mojo/apps/account/rest/bouncer/views.py:219)
  builds `auth_url` and `register_url` from `BOUNCER_LOGIN_PATH` /
  `BOUNCER_REGISTER_PATH` and appends only `?group_uuid=` when a group is
  present. Incoming request query params are not inspected.
- The login switcher is rendered at [login.html:161](mojo/apps/account/templates/account/login.html:161):
  `<a href="{{ register_url|default:'/register' }}">Create one</a>`.
- The register switcher is rendered at [register.html:6](mojo/apps/account/templates/account/register.html:6):
  `<a href="{{ auth_url|default:'/auth' }}">Log in</a>`.
- The base template already reads `redirect`, `next`, `returnTo`, and `back`
  from `location.search` on the receiving page
  ([auth_base.html:63-66](mojo/apps/account/templates/account/auth_base.html:63)
  and [auth_base.html:146](mojo/apps/account/templates/account/auth_base.html:146))
  — they work correctly *if* the params survive the switch. The bug is purely
  that the switcher href drops them.
- `docs/django_developer/account/auth_pages.md` documents `?redirect=` and
  `?back=` as supported per-request overrides — the framework currently fails
  to honor that contract when the user crosses between login and register.

## Acceptance Criteria
- Visiting `/auth?redirect=/dashboard` and clicking "Create one" lands the
  user on `/register?redirect=/dashboard` (and on `/register?group_uuid=X&redirect=/dashboard`
  when a group is in play). After completing registration the user is
  redirected to `/dashboard`, not to `AUTH_SUCCESS_REDIRECT`.
- Same behavior in reverse: `/register?redirect=/dashboard` → "Log in" link
  carries `?redirect=/dashboard` onto `/auth`.
- All four documented forwarding params are preserved together when present:
  `redirect`, `next`, `returnTo`, `back`. (These are the keys
  [auth_base.html:64-66](mojo/apps/account/templates/account/auth_base.html:64)
  already reads.)
- The existing `?group_uuid=` behavior is unchanged — when set, it still
  appears on the destination URL alongside the forwarded params, with a
  single `?` and correct `&` joins (no `??` or `?&` artifacts).
- Unknown / unrelated query params on the source URL are NOT carried over —
  only the documented forwarding set, to avoid leaking arbitrary state (e.g.
  OAuth `code`/`state`, magic-link `token`, reset `token`) onto the switched
  page where it could be mis-handled by the receiving JS.
- Works for both the single-pane and stepped (`register_step2_active`)
  register layouts — the switcher href is the same in both branches.

## Investigation
**What exists**:
- `_auth_context()` builds `auth_url` / `register_url` server-side using
  `group_qs = f'?group_uuid={group_uuid}' if group_uuid else ''`.
- The receiving page already correctly reads forwarding params from
  `location.search` (auth_base.html lines 63-66, 146) — no change needed
  there.
- `paramBack` already overrides the hero "Back to website" link in JS at
  [auth_base.html:146-149](mojo/apps/account/templates/account/auth_base.html:146).

**What changes**:
- [mojo/apps/account/rest/bouncer/views.py](mojo/apps/account/rest/bouncer/views.py):
  Extend `_auth_context()` (or its callers `_serve_login` / `_serve_registration`)
  to read the four forwarding keys from `request.GET` and append them to the
  computed `auth_url` and `register_url`. Build the query string with
  `urllib.parse.urlencode` so the `group_uuid` + forwarded keys merge
  cleanly under a single `?`.
- No template changes are strictly required — the templates already render
  `{{ auth_url }}` / `{{ register_url }}` as-is. Confirm both switcher
  hrefs (register.html:6 and login.html:161) pick up the enriched value.

Alternative considered (rejected): doing the param-forwarding in JS by
rewriting the switcher `<a>` hrefs after page load. Server-side is simpler,
works without JS, and keeps the contract visible in one place
(`_auth_context`).

**Constraints**:
- Only the documented forwarding keys (`redirect`, `next`, `returnTo`,
  `back`) should be carried — never arbitrary GET params. OAuth callback
  params (`code`, `state`), magic-link `token`, and reset tokens must NOT
  bleed across the switch.
- Values must be URL-encoded on output to keep redirect targets safe in the
  href context.
- Existing open-redirect protections in the redirect handler are unchanged —
  this work only forwards the value, it doesn't alter how the destination
  page validates it.
- Backwards compatible: when no forwarding params are present, the
  generated URLs are identical to today's output.

**Related files**:
- [mojo/apps/account/rest/bouncer/views.py](mojo/apps/account/rest/bouncer/views.py) — `_auth_context()` at line 219, `_serve_login()` at line 285
- [mojo/apps/account/templates/account/login.html](mojo/apps/account/templates/account/login.html) — switcher href at line 161
- [mojo/apps/account/templates/account/register.html](mojo/apps/account/templates/account/register.html) — switcher href at line 6
- [mojo/apps/account/templates/account/auth_base.html](mojo/apps/account/templates/account/auth_base.html) — consumer of `redirect`/`back` params at lines 63-66, 146
- [docs/django_developer/account/auth_pages.md](docs/django_developer/account/auth_pages.md) — documents `?redirect=` and `?back=` per-request overrides

## Tests Required
- Request to `/auth?redirect=/dashboard` renders a register switcher href of
  `/register?redirect=%2Fdashboard` (or with `group_uuid` merged when group
  is present).
- Request to `/register?next=/x&back=/y` renders a login switcher href that
  includes both `next` and `back`.
- Request to `/auth?token=ml:abc&redirect=/d` renders a register switcher
  href that contains `redirect` but NOT `token` (whitelist enforcement).
- Request to `/auth?group_uuid=GU&redirect=/d` renders a register switcher
  href containing both `group_uuid` and `redirect`, joined with a single `?`
  and one `&`.
- Plain `/auth` with no query params renders the same URLs as today
  (regression).

## Out of Scope
- Changes to how the destination page consumes `redirect`/`back` — those
  already work.
- Changes to open-redirect validation in `_mat.redirect`.
- Preserving non-whitelisted query params (intentionally excluded).
- Reworking the OAuth-callback or magic-link flows.
- Any visual / template changes to the switcher copy or layout.

## Plan

**Status**: planned
**Planned**: 2026-05-18

### Objective
Forward a whitelisted set of URL params (`redirect`, `next`, `returnTo`, `back`)
into the server-rendered `auth_url` / `register_url` switcher links so they
survive a login ↔ register hop.

### Steps
1. [mojo/apps/account/rest/bouncer/views.py](mojo/apps/account/rest/bouncer/views.py) — In `_auth_context()` at line 219, replace the bare `group_qs = f'?group_uuid={group_uuid}' if group_uuid else ''` (line 234) with a `urlencode()`-built query string composed of `group_uuid` plus any of `{redirect, next, returnTo, back}` present on `request.DATA`. Mirror the existing precedent in `_serve_challenge` ([lines 312-325](mojo/apps/account/rest/bouncer/views.py:312)) — same whitelist shape, same `urlencode()` call. The resulting `group_qs` is reused as-is for both `auth_url` and `register_url` (lines 268-269). No template changes required.
2. [tests/test_auth/bouncer_forms.py](tests/test_auth/bouncer_forms.py) — Extend the existing `_render()` helper (line 41) to accept an optional querystring so `RequestFactory` can build `/auth?redirect=...` etc. Add a new test section (`# Switcher param forwarding`) with the five scenarios listed under Testing. Use the same `assert_true` / `assert_eq` style as the surrounding tests.
3. [docs/django_developer/account/auth_pages.md](docs/django_developer/account/auth_pages.md) — Append one line near the `AUTH_SUCCESS_REDIRECT` / `AUTH_BACK_TO_WEBSITE_URL` rows (or in the per-request override paragraph) noting that `?redirect=`, `?next=`, `?returnTo=`, and `?back=` are preserved across the login↔register switcher.

### Design Decisions
- **Server-side over JS**: Matches the existing `_serve_challenge` precedent in the same file; works without JS; one source of truth for the whitelist.
- **Preserve original key names** (`next` stays `next`, not normalized to `redirect`): least-surprise for third-party deep links that name a specific param.
- **`request.DATA` not `request.GET`**: Per the core CLAUDE.md rule, and matches what `_serve_challenge` already does.
- **Whitelist (not pass-through)**: Explicitly exclude OAuth `code`/`state`, magic-link `token`, reset tokens — those would mis-trigger logic on the switched page.
- **No new helper in `mojo/helpers/`**: ~6 lines used in two adjacent functions in one file. Extracting a helper would be premature.

### Edge Cases
- Empty-string values: filtered by truthiness check (mirrors `_serve_challenge`) — no `?redirect=&back=` artifacts.
- Special chars in redirect targets: `urlencode()` handles escaping.
- `&` in href: HTML-attribute context (not JS-string), Django auto-escapes to `&amp;` which browsers normalize correctly — the bug class fixed by [bouncer_forms.py:376](tests/test_auth/bouncer_forms.py:376) does not apply here.
- Switcher href is identical in both `register_step2_active` (stepped) and single-pane register layouts — one template variable, both branches render `{{ auth_url }}` as-is.
- No-param request: `group_qs == ''` → `auth_url == '/auth'`, identical to today.

### Testing
All in [tests/test_auth/bouncer_forms.py](tests/test_auth/bouncer_forms.py):
- `/auth?redirect=/dashboard` → register switcher href contains URL-encoded `/dashboard` → `tests/test_auth/bouncer_forms.py`
- `/register?next=/x&returnTo=/y&back=/z` → login switcher carries all three keys → `tests/test_auth/bouncer_forms.py`
- `/auth?token=ml:abc&redirect=/d&code=X&state=Y` → register switcher contains `redirect`, NOT `token`/`code`/`state` (whitelist enforcement) → `tests/test_auth/bouncer_forms.py`
- `/auth?group_uuid=GU&redirect=/d` → register switcher has single `?`, both `group_uuid` and `redirect`, joined with one `&` → `tests/test_auth/bouncer_forms.py`
- Plain `/auth` → register switcher href is exactly `/register` (regression guard) → `tests/test_auth/bouncer_forms.py`

### Docs
- [docs/django_developer/account/auth_pages.md](docs/django_developer/account/auth_pages.md) — short note that the switcher preserves the four forwarding params.
- `docs/web_developer/` — no change needed; the API contract for consumers is unchanged.

## Resolution

**Status**: resolved
**Date**: 2026-05-18

### What Was Built
`_auth_context()` in the bouncer views now forwards a whitelisted set of URL
params (`redirect`, `next`, `returnTo`, `back`) from `request.DATA` into the
rendered `auth_url` / `register_url` switcher links, alongside the existing
`group_uuid`. The querystring is assembled with `urllib.parse.urlencode`,
mirroring the precedent already in `_serve_challenge`. Templates were not
touched — both switcher hrefs (login.html:161, register.html:6) pick up the
enriched URL through the same `{{ register_url }}` / `{{ auth_url }}`
variables they already render. OAuth (`code`/`state`), magic-link `token`,
and reset tokens are intentionally NOT forwarded.

### Files Changed
- `mojo/apps/account/rest/bouncer/views.py` — `_auth_context()` lines 228-245 now build `group_qs` via `urlencode` over `group_uuid` + the four whitelisted forwarding keys.
- `tests/test_auth/bouncer_forms.py` — added `_render_switcher()` helper plus five new tests.
- `docs/django_developer/account/auth_pages.md` — added a "Per-request override params" paragraph after the Routing settings table.
- `CHANGELOG.md` — added entry under `v1.1.0 - (current)`.

### Tests
- `tests/test_auth/bouncer_forms.py` — five new tests:
  - `test_switcher_preserves_redirect_param`
  - `test_switcher_preserves_all_forwarding_keys`
  - `test_switcher_whitelists_against_non_forwarded_params`
  - `test_switcher_merges_group_uuid_with_forwarding`
  - `test_switcher_no_params_unchanged_regression`
- Run: `bin/run_tests --agent -t test_auth.bouncer_forms` — 24/24 pass (19 existing + 5 new).
- Full suite: 2089 passed, 2 skipped failures unrelated to this change (pre-existing flakes in `test_mfa.zz_throttle` and `test_realtime.basic`).

### Docs Updated
- `docs/django_developer/account/auth_pages.md` — paragraph after the Routing settings table documenting that the four forwarding params are preserved across the login↔register switcher.
- `docs/web_developer/` — no change (API contract for consumers is unchanged).
- `CHANGELOG.md` — fix entry under `v1.1.0 - (current)`.

### Security Review
No concerns. `urlencode` percent-encodes all values; Django auto-escapes the rendered href; the whitelist correctly excludes OAuth/magic-link/reset tokens. The forwarded value is just carried one hop further on the same origin — destination-page redirect validation is unchanged. One LOW observation: `_serve_challenge` collapses `next`/`returnTo` into `redirect` while `_auth_context` preserves original key names (intentional per design decision, but a consistency note for future readers).

### Follow-up
- None required.
