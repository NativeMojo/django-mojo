# Bouncer URLs Use `group_uuid` Param (Match the Middleware Contract)

**Type**: request
**Status**: resolved
**Date**: 2026-05-16
**Priority**: high

## Description

The framework's URL-param contract is unambiguous:

- `?group=<int>` — integer pk; the dispatch middleware in
  `mojo/decorators/http.py` does `int(request.DATA.group)` and 400s
  with `"Invalid group ID"` on a non-numeric value.
- `?group_uuid=<string>` — operator UUID; the same middleware accepts
  this in its `elif` fallback and resolves `request.group` from it.

The bouncer-served auth pages and the OAuth begin/callback flow
violate this contract — they emit `?group=<uuid>` (a string) and read
`request.GET.get('group')` expecting a UUID. The result: any client
that follows a bouncer-generated link, or any consuming app that
passes the operator uuid to `/auth` or `/register`, gets 400
"Invalid group ID" from the dispatch middleware before the bouncer
view ever runs.

Switch the bouncer + OAuth code paths to consistently use
`group_uuid` for the uuid-bearing param. The integer-pk
`?group=` convention stays exactly as it is.

## Context

A consumer app (multi-tenant SPA on a separate origin in dev)
navigates to the bouncer's `/register?group_uuid=<uuid>&redirect=<spa>`
to start the cross-origin auth handoff. Today the SPA must send
`?group=<uuid>` to get branding because the bouncer's `_resolve_group`
only reads `?group=`. That triggers `int("<uuid>")` in
`dispatch_view_request` and 400s.

Beyond the consumer-app case, this also means the bouncer's *own*
self-generated cross-links are broken — `_auth_context` emits
`?group=<uuid>` into `auth_url` and `register_url`, so the auth
page's "Don't have an account? Create one" link 400s the moment a
group is present in the request.

The contract the middleware enforces is the correct one:

- integer ids in `?group=`
- string uuids in `?group_uuid=`

This request realigns the bouncer (and OAuth bounce path) to that
contract. No template or `mojo-auth.js` changes are needed — those
render branding from the resolved Group object, not from URL params.

## Acceptance Criteria

### `mojo/apps/account/rest/bouncer/views.py`

- `_auth_context(request, group)` — change every place that builds a
  query string from a group uuid:
  ```python
  group_qs = f'?group={group_uuid}' if group_uuid else ''
  ```
  to:
  ```python
  group_qs = f'?group_uuid={group_uuid}' if group_uuid else ''
  ```
  Touches `auth_url`, `register_url`, and any other link emitted with
  group context.

- `_resolve_group(request)` — change the query-param fallback (used
  when hostname lookup misses, typically in dev) from:
  ```python
  group_uuid = request.GET.get('group', '')
  ```
  to:
  ```python
  group_uuid = request.GET.get('group_uuid', '')
  ```

- `_serve_challenge(request, ..., group)` — when forwarding the
  challenge redirect, the preserved query params (currently puts
  `group_uuid` into `fwd_params['group']`) should put it in
  `fwd_params['group_uuid']`.

### `mojo/apps/account/rest/oauth.py`

- `on_oauth_begin` — `state_extra['group_uuid'] = ...` stays as is.
  When the OAuth callback bounces the browser to the frontend
  (`on_oauth_callback`), it appends params. Currently:
  ```python
  redirect_params = {"code": code, "state": state}
  group_uuid = state_data.get("group_uuid", "")
  if group_uuid:
      redirect_params["group"] = group_uuid
  ```
  should become:
  ```python
  redirect_params = {"code": code, "state": state}
  group_uuid = state_data.get("group_uuid", "")
  if group_uuid:
      redirect_params["group_uuid"] = group_uuid
  ```

- The lookup of `group_uuid` from the request earlier in
  `on_oauth_begin` (`request.DATA.get("group_uuid", "")
  or request.GET.get("group", "")`) should drop the fallback to
  `request.GET.get("group", "")` so the param convention is enforced
  on this entry point too. Callers passing the operator uuid via
  `?group=<uuid>` would have been 400ing in the middleware anyway;
  the silent fallback masked that.

### Behavior after the change

A multi-tenant consumer app builds a URL like:
```
http://bouncer/auth?group_uuid=test-brand&redirect=https://spa/callback
```
- Middleware (`dispatch_view_request`): sees no `?group=`, hits the
  `elif group_uuid` branch, resolves `request.group` from
  `"test-brand"`. ✓
- Bouncer view (`on_login_page`): `_resolve_group` reads
  `?group_uuid=` (post-change), finds same Group, sets template
  context for branding. ✓
- Page renders with branding; submit posts succeed.

`?group=<int>` continues to work exactly as before for pk-keyed
endpoints.

## Investigation

**What exists**:
- `mojo/decorators/http.py:74-91` — `dispatch_view_request`: int-only
  on `?group=`, uuid-only on `?group_uuid=`. The contract this
  request aligns to.
- `mojo/apps/account/rest/bouncer/views.py:_auth_context` — emits
  `?group=<uuid>` (incorrect).
- `mojo/apps/account/rest/bouncer/views.py:_resolve_group` — reads
  `?group=` as a uuid (incorrect).
- `mojo/apps/account/rest/bouncer/views.py:_serve_challenge` —
  preserves `group_uuid` under the wrong key in forwarded params.
- `mojo/apps/account/rest/oauth.py:on_oauth_callback` — appends
  `group_uuid` to redirect as `?group=` (incorrect).
- `mojo/apps/account/rest/oauth.py:on_oauth_begin` — falls back to
  `?group=` if `?group_uuid=` not supplied (masks the bug).
- `mojo/apps/account/templates/account/auth_base.html` — JS reads
  `{{ group_uuid }}` from template context (no change needed; comes
  from the resolved Group, not the URL).
- `mojo/apps/account/static/account/mojo-auth.js` — no URL building
  that touches `?group=` (no change needed).

**What changes**:
- `mojo/apps/account/rest/bouncer/views.py` — ~3 places.
- `mojo/apps/account/rest/oauth.py` — ~2 places.

**Constraints**:
- This is a contract fix, not a feature. After the change, callers
  that previously sent `?group=<uuid>` to bouncer/OAuth URLs will get
  the 400 the middleware always wanted to emit. Any consumer that was
  relying on the bouncer's tolerance needs to switch to `?group_uuid=`.
  Document this in the changelog as a breaking change for those
  callers; it's the correct contract.
- Hostname-based group resolution (`Group.resolve_by_auth_domain`)
  continues to be the primary route in production. The URL-param path
  is the dev fallback; aligning it with the rest of the framework is
  the goal here.

**Related files**:
- `mojo/apps/account/rest/bouncer/views.py`
- `mojo/apps/account/rest/oauth.py`
- `mojo/decorators/http.py`
- `mojo/apps/account/templates/account/auth_base.html`
- `mojo/apps/account/static/account/mojo-auth.js`

## Endpoints

| Method | Path | Change | Permission |
|---|---|---|---|
| GET | `/auth` (bouncer login page) | Reads `?group_uuid=` (was `?group=`) for dev-mode branding fallback. Cross-links it emits use `?group_uuid=`. | Public (existing) |
| GET | `/register` (bouncer register page) | Same. | Public (existing) |
| GET | `/contact` (bouncer contact page) | Same — same `_serve_challenge` path. | Public (existing) |
| GET | `/api/auth/oauth/<provider>/begin` | Stops silently accepting `?group=<uuid>` fallback. Callers must use `?group_uuid=`. | Public (existing) |
| GET | `/api/auth/oauth/<provider>/callback` | Appends `?group_uuid=` (was `?group=`) when bouncing to the frontend. | Public (existing) |

## Settings

No new settings.

## Tests Required

- Request `/register?group_uuid=<valid-uuid>` against the wired
  middleware + bouncer → 200 (was: 400 before fix).
- Request `/register?group=<valid-uuid>` (the broken pre-fix shape)
  → 400 from the middleware (asserts that the contract is enforced).
- Request `/register?group=<int-pk>` → 200 (asserts integer-pk path
  is unchanged).
- Rendered auth page contains an internal link to `/register?group_uuid=...`
  (no `?group=<uuid>` form).
- OAuth `on_oauth_callback` bounce URL includes `?group_uuid=` and not
  `?group=` when the OAuth state carried a group.

## Out of Scope

- Making the middleware accept either int or uuid in `?group=`.
  Considered and rejected — the dual convention is the foot-gun this
  request is removing, not extending.
- Bouncer template / JS changes. None needed; they read the resolved
  Group object, not URL params.

## Open Questions

1. **Backward compat for callers that send `?group=<uuid>` today?**
   Default proposal: don't bother — those calls 400 today (because of
   the middleware). They're already broken; this change just stops
   masking the bug in bouncer/oauth view code.

## Resolution

**Status**: resolved
**Date**: 2026-05-16

### What Was Built
All five code edits described in Acceptance Criteria, plus doc + test
updates. The bouncer and OAuth flows now consistently use
`?group_uuid=<uuid>` for UUID-based group selection, matching the
existing framework dispatcher contract. Hostname-based resolution via
`Group.auth_domain` is unchanged.

### Files Changed
- `mojo/apps/account/rest/bouncer/views.py` —
  `_resolve_group` reads `?group_uuid=`; `_auth_context` emits
  `?group_uuid=` in cross-links; `_serve_challenge` forwards
  `group_uuid` through the challenge redirect.
- `mojo/apps/account/rest/oauth.py` —
  `on_oauth_begin` drops the silent `request.GET.get("group")`
  fallback; `on_oauth_callback` appends `?group_uuid=` to the frontend
  bounce URL.
- `tests/test_whitelabel/whitelabel.py` — query-param tests switched
  to `?group_uuid=`; new test asserts `?group=<uuid>` is intentionally
  *not* honored (dispatcher contract); cross-link test now asserts
  `auth_url` / `register_url` emit `?group_uuid=` and not `?group=`.
- `docs/django_developer/account/auth_pages.md`,
  `docs/django_developer/account/bouncer.md`,
  `docs/web_developer/account/auth_pages.md`,
  `docs/web_developer/account/bouncer.md` — public URL convention
  updated to `?group_uuid=` with explicit note about the dispatcher
  contract.
- `CHANGELOG.md` — breaking-change entry for any frontend that built
  `?group=<uuid>` URLs.

### Tests
- `tests/test_whitelabel/whitelabel.py` —
  `test_resolve_group_by_query_param` (now uses `?group_uuid=`),
  `test_resolve_group_ignores_group_int_alias` (new — asserts the
  contract), `test_resolve_group_invalid_uuid`,
  `test_auth_context_group_urls_preserve_param` (now asserts both
  positive and negative — `?group_uuid=` present, `?group=<uuid>`
  absent).
- `tests/test_auth/bouncer_forms.py` — unchanged; still passes (the
  payload-side wiring is independent of URL conventions).
- Run: `bin/run_tests --agent -t test_whitelabel -t test_oauth -t test_auth.bouncer_forms`

### Docs Updated
See "Files Changed" above. Documents both *what* changed and *why*
(the framework dispatcher's strict integer-only handling of `?group=`).

### Security Review
No new surface. UUID resolution was already gated to
`is_active=True` via `Group.objects.filter(uuid=..., is_active=True)`
in `_resolve_group`. Tightening the OAuth begin endpoint by removing
the silent `?group=` fallback is a hardening, not a regression — the
fallback was always silently masking a contract violation.

### Follow-up
- Consumer apps that built URLs with `?group=<uuid>` for bouncer pages
  or OAuth begin must switch to `?group_uuid=<uuid>`. The CHANGELOG
  entry documents this. Those callers were already 400ing in
  production (the dispatcher rejected them), so there's no live
  traffic to migrate.
