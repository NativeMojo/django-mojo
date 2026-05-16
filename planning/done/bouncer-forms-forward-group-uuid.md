# Bouncer Auth Forms Forward `group_uuid` to Register / Login

**Type**: request
**Status**: resolved
**Date**: 2026-05-16
**Priority**: high

## Description

The bouncer-served `/auth` (login) and `/register` pages already pick up
the operator group from the URL (`?group=<uuid>`) and expose it on
`window._matConfig.groupUuid` via `auth_base.html`. But the inline form-
submit handlers in `login.html` and `register.html` don't read that field
when building the payload sent to `/api/auth/login` and
`/api/auth/register`. As a result, the rendered bouncer forms cannot
satisfy `REQUIRE_GROUP_ON_REGISTRATION = True` and the `USER_LOGIN_HANDLER`
loses operator context for every password login through the bouncer page.

Add the missing payload line so the rendered forms participate in the
multi-tenant contract that `MojoAuth.register()` (and the register endpoint
itself) already supports.

## Context

After the recent `register-extensibility` work landed, consumer apps can
mark their deployments as strictly multi-tenant via
`REQUIRE_GROUP_ON_REGISTRATION = True`. The `POST /api/auth/register`
endpoint accepts an optional `group_uuid` body param, looks up the Group
by uuid, and auto-creates a `GroupMember`. `MojoAuth.register()` was also
updated to forward an arbitrary payload via
`Object.assign({}, payload || {})`, so passing `group_uuid` from the
form handler is a one-line change.

The bouncer's `auth_base.html` already plumbs the group through:

```js
// auth_base.html (excerpt)
var GROUP_UUID = "{{ group_uuid|default:''|escapejs }}";
window._matConfig = {
    apiBase: API_BASE,
    redirectTo: redirectTo,
    authUrl: AUTH_URL,
    registerUrl: REGISTER_URL,
    enableGoogle: ENABLE_GOOGLE,
    enableApple: ENABLE_APPLE,
    enablePasskeys: ENABLE_PASSKEYS,
    groupUuid: GROUP_UUID,
    backUrl: paramBack,
    params: params,
};
```

But the form handlers don't read `_matConfig.groupUuid`:

```js
// register.html (excerpt — current)
var payload = { email: email, password: password };
if (firstName) payload.first_name = firstName;
if (lastName) payload.last_name = lastName;
MojoAuth.register(payload);  // ← group_uuid not included
```

```js
// login.html (excerpt — current)
MojoAuth.login(u, p)  // ← payload-only call; no extras possible
```

The login path is more nuanced — `MojoAuth.login()` takes positional
`(username, password)` args, not a payload object. Either the JS
helper grows a third arg or the bouncer page POSTs with `group_uuid`
through a different mechanism. See "Open Questions" below.

## Acceptance Criteria

### `register.html` form handler

Append `group_uuid` from `_matConfig` to the payload before calling
`MojoAuth.register`:

```js
var payload = { email: email, password: password };
if (firstName) payload.first_name = firstName;
if (lastName) payload.last_name = lastName;
if (cfg.groupUuid) payload.group_uuid = cfg.groupUuid;
MojoAuth.register(payload);
```

(`cfg` is `window._matConfig` and is already referenced in the same
file.) Without a value in `_matConfig.groupUuid`, behavior is unchanged
(the key is just not added).

### `login.html` form handler

Forward `group_uuid` to `/api/login` so:

- `request.group` middleware can resolve the operator without falling
  back to hostname.
- `USER_LOGIN_HANDLER` receives a group when the consumer wants it.

The cleanest path is to extend `MojoAuth.login()`:

```js
// mojo-auth.js
login: function (username, password, options) {
    var payload = { username: username, password: password };
    if (options && options.group_uuid) payload.group_uuid = options.group_uuid;
    return post(ep('login'), _withDevice(payload))
        .then(function (resp) { ... });
}
```

Then `login.html` calls it as:

```js
MojoAuth.login(u, p, { group_uuid: cfg.groupUuid });
```

### Tests

- `register.html` rendered with a `group_uuid` in context → form submit
  POSTs with `group_uuid` in the body. (Use the django test client +
  the helper that posts to the existing register endpoint with the
  bouncer-token decoy disabled, the same way the
  `register-extensibility` tests do.)
- `register.html` rendered without `group_uuid` → form submit posts
  without it (existing behavior preserved).
- `MojoAuth.login(u, p, { group_uuid })` → POST body contains
  `group_uuid`.
- `MojoAuth.login(u, p)` (no options) → POST body unchanged from
  pre-change behavior.

## Investigation

**What exists**:
- `mojo/apps/account/templates/account/auth_base.html:72-85` — defines
  `_matConfig.groupUuid` from the page's `group_uuid` context var.
- `mojo/apps/account/templates/account/register.html:72-95` — form
  submit handler that builds `payload` from inputs.
- `mojo/apps/account/templates/account/login.html:202-213` — login
  submit handler.
- `mojo/apps/account/static/account/mojo-auth.js:196-204` — current
  `login()` JS helper (no extras).
- `mojo/apps/account/static/account/mojo-auth.js:218-229` — current
  `register()` JS helper (already forwards arbitrary payload).
- `mojo/apps/account/rest/user.py:228+` — `on_register`, already
  accepts `group_uuid`.
- `mojo/apps/account/rest/user.py:132+` — `on_user_login`, accepts
  `group_uuid` via existing `request.DATA` plumbing (resolves group
  middleware-side).

**What changes**:
- `mojo/apps/account/templates/account/register.html` — one line.
- `mojo/apps/account/templates/account/login.html` — one line at the
  submit handler.
- `mojo/apps/account/static/account/mojo-auth.js` — extend `login()`
  to accept an `options` object with `group_uuid`.

**Constraints**:
- Backward compatibility: callers passing only `(username, password)`
  to `MojoAuth.login()` must continue to work exactly as today.
- No new template context — `groupUuid` is already in `_matConfig`.
- Honeypot decoy `/login` / `/signup` paths
  (`bouncer/views.py:on_decoy_*`) are not affected; they intentionally
  dead-end.

**Related files**:
- `mojo/apps/account/templates/account/auth_base.html`
- `mojo/apps/account/templates/account/login.html`
- `mojo/apps/account/templates/account/register.html`
- `mojo/apps/account/static/account/mojo-auth.js`
- `mojo/apps/account/rest/user.py`

## Endpoints

| Method | Path | Change | Permission |
|---|---|---|---|
| POST | `/api/auth/register` | No change — already accepts `group_uuid`. The hosted form now actually sends it. | Public, bouncer-token + rate-limited (unchanged) |
| POST | `/api/auth/login` | No change — `request.group` middleware already reads `group_uuid` if present. The hosted form now actually sends it. | Public, bouncer-token + rate-limited (unchanged) |

## Settings

No new settings. Existing flags continue to govern behavior:

| Setting | Behavior with this change |
|---|---|
| `REQUIRE_GROUP_ON_REGISTRATION` | Now satisfiable from the hosted register form (was not before). |
| `ALLOWED_REDIRECT_URLS` | Unaffected. |
| `BOUNCER_REQUIRE_TOKEN` | Unaffected. |

## Tests Required

- Render `register.html` with `group_uuid='test-brand'` in context.
  Submit form via the existing testing harness → POST body contains
  `group_uuid: 'test-brand'`.
- Same render without `group_uuid` → POST body omits the key (does not
  include `''` or `null`).
- Submit form with `REQUIRE_GROUP_ON_REGISTRATION = True` and a valid
  `group_uuid` → 200 with new user + `GroupMember`.
- Submit form with `REQUIRE_GROUP_ON_REGISTRATION = True` and no
  `group_uuid` → 400 (existing behavior, just exercised via the form
  path).
- `MojoAuth.login(u, p, { group_uuid: 'x' })` → POST body includes
  `group_uuid`.
- `MojoAuth.login(u, p)` → POST body has no `group_uuid`.

## Out of Scope

- Allow `group_uuid` to flow through the OAuth login path
  (Google/Apple/GitHub). The OAuth flow already resolves group context
  via state — separate concern.
- Magic link / email verify flows. Those don't take credentials in
  the form, so the group-context route is different (token claims).
- Adding a `group_uuid` input field visible to the user. The value
  rides invisibly from the URL → `_matConfig` → form submit; users
  don't pick the operator.

## Open Questions

1. **`MojoAuth.login()` signature change**: adding a third arg
   (`options`) is the most backward-compatible. Alternative: convert
   to a single object arg (`MojoAuth.login({ username, password,
   group_uuid })`) and deprecate the positional form. Prefer the
   additive third-arg approach for v1 — minimum disruption to
   downstream callers.

2. **Login should fail closed if `group_uuid` is supplied but invalid?**
   Today the `request.group` middleware silently falls through to other
   resolvers if the supplied id doesn't resolve. Should an explicit-but-
   unknown `group_uuid` in the body be a 400 instead? Default proposal:
   keep current behavior (best-effort resolution) — bouncer page already
   validates the URL `?group=` server-side when rendering the form, so
   an invalid value reaching the POST means the page was hand-crafted.

## Plan

**Status**: planned
**Planned**: 2026-05-16

### Objective
Make the bouncer-served register/login forms forward `group_uuid` from
`_matConfig` so multi-tenant deployments (`REQUIRE_GROUP_ON_REGISTRATION`)
and `USER_LOGIN_HANDLER` see operator context for hosted-form auth.

### Steps
1. `mojo/apps/account/static/account/mojo-auth.js:196-204` — Extend
   `login()` to accept an optional third `options` arg. Build the payload
   as `{ username, password }`; if `options && options.group_uuid` is
   truthy, add `group_uuid` to the payload. Update the JSDoc to document
   the new param. Two-arg callers behave identically to today.
2. `mojo/apps/account/templates/account/login.html:207` — Change the
   password submit call from `MojoAuth.login(u, p)` to
   `MojoAuth.login(u, p, { group_uuid: cfg.groupUuid })`.
   `cfg = window._matConfig` is already declared at line 159; no other
   changes needed in this file.
3. `mojo/apps/account/templates/account/register.html:70-89` —
   Declare `var cfg = window._matConfig;` in the page_script IIFE
   (parallel to login.html). In the submit handler, after building
   `payload` from form inputs and before `MojoAuth.register(payload)`,
   add `if (cfg.groupUuid) payload.group_uuid = cfg.groupUuid;`.

### Design Decisions
- Additive third `options` arg on `login()`: minimum disruption to
  existing positional callers. Single-object form deferred.
- Best-effort group resolution on login: per open-question #2 — keep
  current middleware behavior. Bouncer page validates `?group=`
  server-side before render, so a bad value in POST means hand-crafted
  page.
- Truthy check before assignment: both forms set `group_uuid` only when
  `cfg.groupUuid` is non-empty. Single-tenant deployments emit identical
  payloads to today — matches existing `register-extensibility` contract.
- No template-context changes: `group_uuid` is already plumbed through
  `auth_base.html:72-82` into `_matConfig.groupUuid`. The defect is that
  downstream form handlers never read it.

### User Cases
- Multi-tenant register page: `/register?group=<uuid>` →
  `_matConfig.groupUuid` populated → submit POSTs `group_uuid` →
  `on_register` creates `User` + `GroupMember`. Satisfies
  `REQUIRE_GROUP_ON_REGISTRATION=True`.
- Multi-tenant login page: `/auth?group=<uuid>` → password submit POSTs
  `group_uuid` → `request.group` middleware resolves operator →
  `USER_LOGIN_HANDLER` receives the group.
- Single-tenant deployment: `_matConfig.groupUuid === ""` → submit
  payloads omit the key → identical to today.
- Hostname-resolved group: bouncer detects group via
  `Group.auth_domain` (not query param) → `_auth_context` still sets
  `group_uuid` on the template → form submit POSTs it. Backend resolves
  the same group; idempotent.
- OAuth / magic link / passkey paths: untouched. OAuth resolves group
  via `state`; magic link via token claims. Out of scope per request.

### Edge Cases
- `cfg.groupUuid === ""`: truthy check skips the assignment; payload
  identical to pre-change behavior.
- `MojoAuth.login(u, p)` legacy callers (downstream apps): no change —
  `options` is `undefined`, payload omits `group_uuid`.
- `MojoAuth.login(u, p, undefined)` / `MojoAuth.login(u, p, {})`:
  `options && options.group_uuid` short-circuits; payload omits the key.
- Honeypot decoy `/login` / `/signup` POSTs: use a different template
  (`bouncer_decoy.html`) and dead-end at `on_decoy_post`. Not affected.
- `escapejs` on `GROUP_UUID`: already applied in `auth_base.html:72`.
  No XSS concern in the inline JS string literal.

### Testing
Tests go in a new `tests/test_auth/bouncer_forms.py` and use the
`RequestFactory` + `django.shortcuts.render` pattern from
`tests/test_whitelabel/whitelabel.py:179-263`. They assert against
rendered HTML — JS execution is out of scope (no JS test runner in repo).

- `test_register_form_emits_group_uuid_assignment` — render
  `register.html` via `_serve_login(page_mode='register', group=<group>)`
  → assert response body contains
  `payload.group_uuid = cfg.groupUuid` → `tests/test_auth/bouncer_forms.py`
- `test_register_form_groupuuid_populated_from_context` — render with
  group → assert HTML contains `var GROUP_UUID = "<test uuid>"` →
  `tests/test_auth/bouncer_forms.py`
- `test_register_form_groupuuid_empty_without_group` — render with no
  group → assert HTML contains `var GROUP_UUID = ""` →
  `tests/test_auth/bouncer_forms.py`
- `test_login_form_passes_group_uuid_option` — render `login.html` with
  group → assert HTML contains
  `MojoAuth.login(u, p, { group_uuid: cfg.groupUuid })` →
  `tests/test_auth/bouncer_forms.py`
- `test_mojo_auth_login_signature_accepts_options` — read `mojo-auth.js`
  from disk → assert `login: function (username, password, options)` and
  `options.group_uuid` both appear (smoke check guarding against revert)
  → `tests/test_auth/bouncer_forms.py`

End-to-end backend coverage that `/api/auth/register` honors
`group_uuid` and enforces `REQUIRE_GROUP_ON_REGISTRATION` already
exists in `tests/test_register/register.py:138-260` — no new backend
tests required.

### Docs
- `docs/django_developer/` — auth-pages section: note that the hosted
  register/login forms now automatically forward `group_uuid` when the
  bouncer page resolves a group (either via `?group=` or hostname
  `Group.auth_domain`). Multi-tenant deployments no longer need a
  custom front-end to satisfy `REQUIRE_GROUP_ON_REGISTRATION`.
- `docs/web_developer/` — `MojoAuth` reference: document the new
  optional third arg on `login(username, password, options)` with
  shape `{ group_uuid?: string }` so downstream apps can pass it
  explicitly when not using the hosted form.
- `CHANGELOG.md` — entry: "Bouncer-hosted register and login forms now
  forward `group_uuid` from `_matConfig`. `MojoAuth.login()` accepts an
  optional third `options` arg with `{ group_uuid }`."

## Resolution

**Status**: resolved
**Date**: 2026-05-16

### What Was Built
The bouncer-hosted register and login forms now forward
`_matConfig.groupUuid` to the backend on submit. `MojoAuth.login()`
gained an optional third `options` arg with `{ group_uuid }`. The change
is purely additive: single-tenant deployments and existing two-arg
callers behave identically.

### Files Changed
- `mojo/apps/account/static/account/mojo-auth.js` — `login()` accepts
  optional `options` arg and forwards `options.group_uuid` to
  `POST /api/login`.
- `mojo/apps/account/templates/account/login.html` — submit handler
  now calls `MojoAuth.login(u, p, { group_uuid: cfg.groupUuid })`.
- `mojo/apps/account/templates/account/register.html` — submit handler
  declares `cfg = window._matConfig` and adds
  `if (cfg.groupUuid) payload.group_uuid = cfg.groupUuid;` before
  `MojoAuth.register(payload)`.

### Tests
- `tests/test_auth/bouncer_forms.py` — 7 tests asserting the rendered
  template HTML carries the new payload wiring, the `GROUP_UUID`
  template binding stays correct with and without a group, and
  `mojo-auth.js` retains the `options` signature and `group_uuid`
  forwarding.
- Run: `bin/run_tests --agent -t test_auth.bouncer_forms`

### Docs Updated
- `docs/django_developer/account/auth_pages.md` — new
  "Multi-Tenant: Forwarding `group_uuid` From the Hosted Forms" section
  describing the resolve → emit → forward chain.
- `docs/web_developer/account/auth_pages.md` — under "Per-Group
  Branding", notes that the rendered forms automatically include
  `group_uuid` on submit so `REQUIRE_GROUP_ON_REGISTRATION` is
  satisfiable from the hosted page.
- `docs/web_developer/account/authentication.md` — `POST /api/login`
  field table now documents the optional `group_uuid` body param and
  the SDK third-arg shape.
- `CHANGELOG.md` — entry under `v1.1.0 (current)`.

### Security Review
No new sensitive surface. `group_uuid` is already accepted by
`/api/auth/register` and resolved by `request.group` middleware; this
change only adds a client-side payload line. The template literal is
already passed through `escapejs`. `request.group` keeps its
best-effort resolution (per the request's open-question default), and
the register endpoint already strictly validates `group_uuid` against
the active-group set.

### Follow-up
- None planned. OAuth / magic-link / passkey paths intentionally
  remain out of scope per the request's "Out of Scope" section.
