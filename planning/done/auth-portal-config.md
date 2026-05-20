# Group-Owned Auth Portal Config (Theming, Registration, Passkey & Login Methods)

**Type**: request
**Status**: resolved
**Date**: 2026-05-20
**Priority**: medium

## Description

Give each group a self-owned **portal config** that controls how its login and
registration experience looks and behaves, plus a reusable framework-served
passkey enrollment page so no UI has to rebuild the WebAuthn flow.

The feature has four user-facing parts:

1. **Passkey enrollment during registration** — after a user registers, offer
   (or require) adding a passkey, driven by group config.
2. **Per-group registration & login theming** — when `?group_uuid=` is passed
   (or the host matches `Group.auth_domain`), the hosted login/register pages
   render with that group's branding and form shape.
3. **Passkey login** — already implemented; verify it works end-to-end and gate
   its visibility on the new per-group login-method config.
4. **Per-group login-method restriction** — a group can narrow which login
   methods are offered (e.g. "phone code or passkey only"). This is a **UX
   convenience, not a security boundary** (see Constraints).

The unifying design is a single structured `portal` config object — `theme`,
`registration`, `login` — resolved per group, with a deployment-wide default.

## Context

Asked for by the project owner. The auth system already has all the hard
primitives (passkeys, SMS OTP, OAuth, magic links, schema-driven registration,
per-group setting resolution). What's missing is a **coherent, group-owned
config surface** and a **reusable passkey enrollment UI**.

Today, auth-page behavior is spread across ~20 flat `AUTH_*` Django settings
plus `AUTH_REGISTER_FIELDS`. These are DB-backed and *can* be scoped per-group
via `Setting.set(key, value, group=group)`, but there is no single object a
group admin manages as "their portal." **No deployment is using the `AUTH_*`
settings yet**, so we are free to redesign the storage model rather than
preserve the flat keys.

Key decisions already made with the project owner:

- Login-method restriction is a **UX nice-to-have**, not hard enforcement.
- Registration is **also** group-configurable (fields + which signup methods).
- Config is stored on **`Group.metadata`** (the group owns it), with a single
  global JSON setting as the deployment default. The flat `AUTH_*` keys are
  retired and folded into one structured object.
- Passkey enrollment is inherently a browser round-trip flow; a **dedicated
  framework template** hosts it so every UI can reuse it.

## Acceptance Criteria

- A structured `portal` config object exists with `theme`, `registration`, and
  `login` sections, plus a `resolve_portal_config(group)` resolver that
  deep-merges: code defaults ← global `AUTH_PORTAL` setting ← `group.metadata["portal"]`.
- The hosted `/auth` and `/register` pages render theme, register fields, and
  offered methods from the resolved portal config for the request's group
  (host → `Group.auth_domain`, or `?group_uuid=`).
- A group admin can set `metadata.portal` through the existing Group REST
  endpoint; invalid config (bad register fields, unknown method names, bad
  enum values) is rejected at write time with a clear error.
- A reusable passkey enrollment page is served by the framework, themed by the
  portal config, that runs begin → `navigator.credentials.create()` → complete
  and then redirects to a target URL.
- The hosted `/register` page, when `registration.passkey_prompt` is `optional`
  or `required`, sends the user to the passkey enrollment page after signup;
  `required` removes the skip option.
- `mojo-auth.js` exposes a `registerPasskey()` helper that performs the
  authenticated begin/complete round-trip.
- Passkey login on the hosted login page works end-to-end and is shown only
  when `passkey` is in `login.methods`.
- When `group_uuid` is present on a login request, login endpoints reject a
  method that is not in that group's `login.methods` with a clear,
  non-enumerating error. When `group_uuid` is absent, no restriction applies.
- The hosted login/register pages omit form fields/buttons for methods not in
  the group's config (e.g. no password box for a passkey/SMS-only group).
- A public `GET /api/auth/portal` returns the resolved, safe-subset portal
  config so custom front-ends can render their own login UI from it.
- Default portal config (no group customization) reproduces today's behavior:
  email + password registration, all login methods enabled.
- Docs updated in both tracks; `CHANGELOG.md` updated.

## Investigation

**What exists**

- **Passkeys — fully implemented.**
  `mojo/apps/account/models/pkey.py` (`Passkey` model),
  `mojo/apps/account/utils/passkeys.py` (`PasskeyService`, `fido2` lib),
  `mojo/apps/account/rest/passkeys.py`:
  - Authenticated registration: `POST /api/account/passkeys/register/begin`
    + `/complete` (`passkeys.py:59-144`).
  - Public login: `POST /api/auth/passkeys/login/begin` + `/complete`
    (`passkeys.py:151-242`).
  - Management: `GET/POST/DELETE /api/account/passkeys[/<pk>]`.
  These endpoints need **no changes**.
- **Passkey login on the hosted page — already wired.**
  `templates/account/login.html` has `#btn-passkey` (shown when
  `enable_passkeys`); `static/account/mojo-auth.js` has
  `loginWithPasskeyDiscoverable()`, `isPasskeySupported()`.
- **Per-group page resolution — already wired.**
  `rest/bouncer/views.py` `_resolve_group()` (host → `Group.auth_domain`, or
  `?group_uuid=`); `_auth_context()` resolves every `AUTH_*` setting with
  `group=group`. `register_schema.resolve_fields(group=)` already group-scopes
  the register form.
- **Settings group-scoping.** `settings.get(key, default, group=group)` and
  `Setting.set(key, value, group=group)` support per-group values with parent
  chain + global fallback.
- **Registration.** `on_register` at `rest/user.py:238-427`, schema-driven via
  `mojo/apps/account/services/register_schema.py`. Gated by
  `ALLOW_USER_REGISTRATION`; `group_uuid` already accepted and resolved.
- **Hosted pages.** Bouncer views `rest/bouncer/views.py`; static asset serving
  `rest/bouncer/static.py`; templates in `templates/account/`
  (`auth_base.html`, `login.html`, `register.html`, `auth_hero.html`, etc.).
- **Group model.** `models/group.py` — `metadata` JSONField (in the `default`
  REST graph), `RestMeta` SAVE_PERMS `manage_groups`/`manage_group`/`groups`,
  `PROTECTED_JSON_PERMS`, `get_metadata_value()` with parent fallback.
- **MFA method discovery.** `rest/user.py:430` `get_mfa_methods()` already
  enumerates `totp`/`sms`/`passkey` — a useful pattern for method lists.

**What changes**

- **New** `mojo/apps/account/services/portal_config.py` — defines the portal
  config schema, code defaults, `resolve_portal_config(group)` (deep merge of
  defaults ← `AUTH_PORTAL` setting ← `group.metadata["portal"]`), a
  `validate_portal_config(cfg)` validator, and a `public_portal_config(cfg)`
  helper that strips non-public fields for the public read endpoint.
- **`services/register_schema.py`** — read fields / identity / min-age from
  `resolve_portal_config(group).registration` instead of the standalone
  `AUTH_REGISTER_FIELDS` / `AUTH_REGISTER_IDENTITY_FIELD` / `AUTH_MIN_AGE_YEARS`
  settings. Keep all existing validators (`validate_payload`, `field_rows`,
  `partition_for_stepped_flow`).
- **`rest/bouncer/views.py`** — `_auth_context()` reads theme + register +
  login sections from `resolve_portal_config(group)`; the template context
  exposes the offered login/registration methods so disabled ones are omitted
  server-side. New route + handler for the passkey enrollment page.
- **New template** `templates/account/passkey_enroll.html` — themed via the
  portal config; loads `mojo-auth.js`, reads the stored JWT, runs the begin →
  `navigator.credentials.create()` → complete round-trip, then redirects to
  `?redirect=`. `required` mode hides the skip button.
- **`static/account/mojo-auth.js`** — add `registerPasskey()` helper +
  endpoint refs for `passkeysRegisterBegin`/`passkeysRegisterComplete`.
- **`templates/account/register.html`** — after `MojoAuth.register()` resolves,
  if `registration.passkey_prompt != off`, send the user to the passkey
  enrollment page (carrying `group_uuid` + `redirect`).
- **`templates/account/login.html`** — render only the methods in
  `login.methods` (omit the password field for password-disabled groups).
- **Login endpoints** — soft method-gate when `group_uuid` is present:
  `on_user_login` (password), `on_sms_login` (sms), `on_passkeys_login_begin`
  (passkey), magic-link send, OAuth begin. Reject disabled methods with a
  generic error. No restriction when `group_uuid` is absent.
- **`on_register` / OAuth register** — honor `registration.enabled` and
  `registration.methods` per group (in addition to the global
  `ALLOW_USER_REGISTRATION` kill-switch).
- **`models/group.py`** — `on_rest_pre_save` hook validates `metadata["portal"]`
  via `validate_portal_config()` so a bad config is rejected at write time.
- **New endpoint** `GET /api/auth/portal` — public, returns the resolved
  safe-subset portal config for a `group_uuid` (or the deployment default).
- **Settings** — add `AUTH_PORTAL` (JSON, global default). Retire the flat
  `AUTH_*` auth-page keys and `AUTH_REGISTER_*` keys (no deployment uses them).
- **Docs** — `docs/django_developer/account/auth_pages.md` +
  `docs/web_developer/account/auth_pages.md` rewritten around the portal
  config; `passkeys.md` (both tracks) gains the enrollment-page flow; consider
  a new `portal_config.md`. Update both `README.md` indexes and `CHANGELOG.md`.

**Constraints**

- **WebAuthn requires browser round-trips** — passkey enrollment cannot be a
  single API call; the begin → `navigator.credentials.create()` → complete
  sequence must run in the browser. The dedicated enrollment page exists to
  centralize this.
- **JWT is not a cookie** — the enrollment page reads the access token from
  `localStorage` (where `mojo-auth.js` stores it). This is seamless for
  same-origin clients using that library. A cross-origin custom app that
  manages tokens differently must call the begin/complete API itself — that is
  an accepted boundary, not a bug.
- **Login-method restriction is UX-only, not a security boundary.** It is
  enforced only when `group_uuid` is supplied; an API caller that omits
  `group_uuid` is not restricted. Do not present this as a hard control, and do
  not weaken any existing permission/verification gate to implement it.
- **`?group=` is reserved** by the framework dispatcher for integer IDs; the
  public group slot must remain `?group_uuid=`.
- **`custom_css` is an XSS vector.** Theme `custom_css` is injected as an inline
  `<style>` tag. With config now editable by group admins (not just platform
  ops), an admin could break out of the tag (`</style><script>…`). Mitigate:
  keep `custom_css` / `custom_css_url` as a platform-ops-only field (rejected
  from `metadata.portal` writes, or guarded by `PROTECTED_JSON_PERMS`), or
  strictly sanitize it. Resolve during design.
- **Backwards behavior** — although the flat `AUTH_*` settings are retired, the
  *default* resolved portal config must reproduce today's behavior: email +
  password registration, all login methods enabled, existing theme defaults.
- **Migrations** — `metadata` is an existing JSONField; no schema migration is
  expected. Re-run `bin/create_testproject` only if a model field changes.

**Related files**

- `mojo/apps/account/rest/bouncer/views.py`
- `mojo/apps/account/rest/bouncer/static.py`
- `mojo/apps/account/rest/passkeys.py`
- `mojo/apps/account/rest/user.py`
- `mojo/apps/account/rest/sms.py`
- `mojo/apps/account/rest/oauth.py`
- `mojo/apps/account/services/register_schema.py`
- `mojo/apps/account/services/portal_config.py` *(new)*
- `mojo/apps/account/models/group.py`
- `mojo/apps/account/utils/passkeys.py`
- `mojo/apps/account/templates/account/login.html`
- `mojo/apps/account/templates/account/register.html`
- `mojo/apps/account/templates/account/passkey_enroll.html` *(new)*
- `mojo/apps/account/templates/account/auth_base.html`
- `mojo/apps/account/static/account/mojo-auth.js`
- `docs/django_developer/account/auth_pages.md`, `docs/web_developer/account/auth_pages.md`
- `docs/django_developer/account/passkeys.md`, `docs/web_developer/account/passkeys.md`

## Endpoints

| Method | Path | Description | Permission |
|---|---|---|---|
| GET | `/passkey` | Framework-served passkey enrollment page (route configurable via `BOUNCER_PASSKEY_PATH`). Themed by portal config; requires a client-side JWT. | public route; client must hold a valid access token |
| GET | `/api/auth/portal` | Resolved safe-subset portal config for `?group_uuid=` (or deployment default). Lets custom front-ends render their own login UI. | public |
| POST | `/api/account/passkeys/register/begin` | *(exists, unchanged)* Begin authenticated passkey registration. | auth required |
| POST | `/api/account/passkeys/register/complete` | *(exists, unchanged)* Complete passkey registration. | auth required |
| POST | `/api/auth/passkeys/login/begin` | *(exists)* Begin passkey login. Soft-gated when `group_uuid` present. | public |
| POST | `/api/auth/passkeys/login/complete` | *(exists)* Complete passkey login. | public |
| — | `/api/account/group/<pk>` | *(exists)* Portal config edited via `metadata.portal` on the standard Group endpoint; validated on save. | `manage_groups` / `manage_group` / `groups` |

A dedicated `GET/POST /api/account/group/<pk>/portal` read-write endpoint may be
considered during design if editing the whole `metadata` blob proves awkward;
the default approach is the existing Group endpoint + a `pre_save` validator.

## Settings

| Setting | Default | Purpose |
|---|---|---|
| `AUTH_PORTAL` | `{}` (code defaults apply) | Deployment-wide default portal config (theme + registration + login). Replaces the retired flat `AUTH_*` keys. Group overrides live in `Group.metadata["portal"]`. |
| `ALLOW_USER_REGISTRATION` | `False` | *(exists)* Global registration kill-switch. Per-group `registration.enabled` is layered on top. |
| `BOUNCER_PASSKEY_PATH` | `passkey` | URL path for the hosted passkey enrollment page. |
| `BOUNCER_LOGIN_PATH` / `BOUNCER_REGISTER_PATH` | `auth` / `register` | *(exist, unchanged)* |
| *(retired)* `AUTH_APP_TITLE`, `AUTH_LOGO_URL`, `AUTH_FAVICON_URL`, `AUTH_HERO_*`, `AUTH_BACK_TO_WEBSITE_URL`, `AUTH_TERMS_URL`, `AUTH_CUSTOM_CSS`, `AUTH_CUSTOM_CSS_URL`, `AUTH_LAYOUT`, `AUTH_API_BASE`, `AUTH_SUCCESS_REDIRECT`, `AUTH_ENABLE_GOOGLE/APPLE/PASSKEYS`, `AUTH_REGISTER_FIELDS`, `AUTH_REGISTER_IDENTITY_FIELD`, `AUTH_MIN_AGE_YEARS` | — | Folded into the `AUTH_PORTAL` structured object / `portal` config. Safe to retire — no deployment uses them. |

### Portal config shape (draft — finalized in design)

```json
{
  "theme": {
    "app_title": "DJANGO MOJO",
    "logo_url": "", "favicon_url": "",
    "hero_image_url": "", "hero_headline": "Welcome back", "hero_subheadline": "",
    "back_to_website_url": "", "terms_url": "",
    "layout": "card",
    "custom_css": "", "custom_css_url": ""
  },
  "registration": {
    "enabled": true,
    "fields": [
      {"name": "first_name", "required": false},
      {"name": "last_name",  "required": false},
      {"name": "email",      "required": true, "verify": "email"},
      {"name": "password",   "required": true}
    ],
    "identity_field": null,
    "min_age": null,
    "methods": ["password", "oauth"],
    "passkey_prompt": "optional"
  },
  "login": {
    "methods": ["password", "sms", "passkey", "magic", "oauth"]
  }
}
```

- `login.methods` / `registration.methods` ∈ `password`, `sms`, `passkey`,
  `magic`, `oauth` — group config can toggle every method on/off.
- `registration.passkey_prompt` ∈ `off`, `optional`, `required`.
- `theme.layout` ∈ `card`, `fullscreen`.

## Tests Required

- `resolve_portal_config()` deep-merges code defaults ← `AUTH_PORTAL` setting ←
  `group.metadata["portal"]`; group override beats global beats defaults.
- Default config (no customization) yields today's behavior: email + password
  registration, all five login methods enabled.
- `validate_portal_config()` rejects: unknown method names, bad
  `passkey_prompt` / `layout` enum values, malformed `registration.fields`,
  a field schema with neither `email` nor `phone`.
- Saving an invalid `metadata.portal` through the Group REST endpoint returns a
  validation error and does not persist.
- Hosted `/auth` for a group with `login.methods` excluding `password` renders
  no password field but shows the SMS/passkey options.
- Hosted `/register` for a group with a custom `registration.fields` schema
  renders that form (reuses existing register_schema tests).
- Passkey login: full begin → complete cycle issues a JWT (reuses existing
  passkey tests); login is rejected when `group_uuid` resolves a group whose
  `login.methods` excludes `passkey`.
- Password login with a `group_uuid` whose group disables `password` is
  rejected with a generic error; the same login with no `group_uuid` succeeds
  (confirms UX-only, non-enforced semantics).
- SMS login is rejected for an SMS-disabled group when `group_uuid` is present.
- `registration.passkey_prompt = required` produces an enrollment page with no
  skip path; `optional` keeps the skip path.
- `GET /api/auth/portal?group_uuid=` returns the group's resolved config and
  omits non-public fields (e.g. raw `custom_css` handling per the XSS decision).
- Per-group theming applies: two groups with different `theme` configs render
  different branding for the same hosted page.
- Passkey enrollment page: a valid token completes enrollment and a `Passkey`
  row is created; a missing/invalid token surfaces a clear error.

## Out of Scope

- **Hard security enforcement** of login-method restriction (tying the
  restriction to the user's group membership so it cannot be bypassed by
  omitting `group_uuid`). Explicitly a UX feature this round.
- Building login/registration UIs for custom (non-framework-hosted)
  front-ends — they consume `GET /api/auth/portal` and the existing endpoints.
- Cross-origin JWT handoff for the passkey enrollment page beyond the
  `localStorage` / `mojo-auth.js` path.
- New OAuth providers or changes to OAuth provider behavior.
- A visual admin/portal editor UI for the portal config (config is edited via
  the Group REST endpoint / JSON this round).
- Changes to the passkey `register/begin`/`complete` or `login/*` endpoints.
- Migrating or changing the `Passkey`, `User`, or `Group` model schemas
  (config lives in the existing `Group.metadata` JSONField).

## Plan

**Status**: planned
**Planned**: 2026-05-20

### Objective
Introduce a group-owned `portal` config (theme / registration / login) resolved
per group, wire the hosted auth pages and login endpoints to it, and add a
reusable framework-served passkey enrollment page.

### Config model

`portal` config object — three sections:

```
theme:        app_title, logo_url, favicon_url, hero_image_url, hero_headline,
              hero_subheadline, back_to_website_url, terms_url, layout,
              api_base, success_redirect, custom_css, custom_css_url
registration: enabled, fields[], identity_field, min_age,
              methods[], passkey_prompt
login:        methods[]
```

- Method tokens: `password`, `sms`, `passkey`, `magic`, `google`, `apple`
  (granular — no coarse `oauth`). `registration.methods` ⊆
  `password, google, apple`. `passkey_prompt` ∈ `off|optional|required`.
  `layout` ∈ `card|fullscreen`.
- Resolution: `DEFAULT_PORTAL` (code) ← `AUTH_PORTAL` setting (global) ←
  `group.metadata["portal"]` walked root→group down the parent chain.
  Deep-merge — dicts merge, lists/scalars replace.

### Steps

1. `mojo/apps/account/services/portal_config.py` *(new)* — `DEFAULT_PORTAL`
   defaults; `resolve_portal_config(group=None, request=None)` (deep-merge +
   parent-chain walk, returns objict); `validate_portal_config(cfg)` (method
   tokens, enums, `registration.fields` via `register_schema` normalization,
   non-empty `login.methods`, `validate_custom_css`); `validate_custom_css(s)`
   (reject `<`; reject `@import` + external URL schemes `http:`/`https:`/`//`,
   allow `data:`; `custom_css_url` must be well-formed `https://`);
   `public_portal_config(cfg)` (safe subset for the public endpoint);
   `resolve_group_from_request(request)`; `assert_login_method(method, group)`
   (raises only when a group is resolved). Test-mode override header
   `X-Mojo-Test-Portal-Config` mirroring `register_schema`'s pattern.
2. `mojo/apps/account/services/register_schema.py` — `resolve_fields` /
   `resolve_identity_field` / `resolve_min_age` source from
   `resolve_portal_config(group).registration`; keep all validators
   (`validate_payload`, `field_rows`, `partition_for_stepped_flow`). Drop the
   `AUTH_REGISTER_FIELDS` / `AUTH_REGISTER_IDENTITY_FIELD` / `AUTH_MIN_AGE_YEARS`
   reads. Keep `X-Mojo-Test-Register-Fields` working by routing through the
   portal-config test override.
3. `mojo/apps/account/models/group.py` — add `on_rest_pre_save` that runs
   `validate_portal_config(metadata["portal"])` when present → `ValueException`
   on bad config.
4. `mojo/apps/account/rest/bouncer/views.py` — `_auth_context()` builds context
   from `resolve_portal_config(group)`; expose `login_methods`,
   `registration_methods`, `passkey_prompt`. New route `/passkey`
   (`BOUNCER_PASSKEY_PATH`, default `passkey`) + `on_passkey_enroll_page`
   handler (not bouncer-gated; resolves group by host/`?group_uuid=`; renders
   `passkey_enroll.html`).
5. `mojo/apps/account/templates/account/passkey_enroll.html` *(new)* — extends
   `auth_base.html`; reads JWT from `localStorage`; "Add a passkey" →
   `MojoAuth.registerPasskey()`; "Skip" → redirect to `?redirect=` (hidden when
   `passkey_prompt=required`); no-JWT → "session expired" + login link;
   no WebAuthn support on a `required` group → degrade to skippable + message.
6. `mojo/apps/account/templates/account/auth_base.html` — add `loginMethods`
   + `passkeyPrompt` to `window._matConfig`.
7. `mojo/apps/account/templates/account/login.html` — render password form,
   OAuth, passkey, magic per `login_methods`; add a new **SMS-code login view**
   (phone → send code → 6-digit verify) shown when `sms` ∈ `login_methods`.
8. `mojo/apps/account/templates/account/register.html` — after `register()`
   success (non-`requires_verification`), if `passkey_prompt != off` redirect
   to the `/passkey` page (carry `group_uuid` + `redirect`); gate OAuth buttons
   by `registration_methods`.
9. `mojo/apps/account/static/account/mojo-auth.js` — endpoint refs
   `passkeyRegisterBegin`/`passkeyRegisterComplete`, `smsLogin`, `smsVerify`,
   `portalConfig`; `authedPost()` helper (Bearer); `registerPasskey(name)`
   (authed begin → `navigator.credentials.create()` → authed complete);
   `startSmsLogin(phone)` / `verifySmsLogin(identifier, code)`;
   `getPortalConfig(groupUuid)`.
10. `mojo/apps/account/rest/user.py` — `on_user_login` soft-gate `password`;
    `on_magic_login_send` soft-gate `magic`; `on_register` reject when
    `resolve_portal_config(group).registration.enabled` is False (after the
    global `ALLOW_USER_REGISTRATION` check).
11. `mojo/apps/account/rest/sms.py` — `on_sms_login` soft-gate `sms`
    (before the enumeration-safe success path).
12. `mojo/apps/account/rest/oauth.py` — `on_oauth_begin` soft-gate the provider
    token; OAuth auto-registration honors `registration.methods`.
13. `mojo/apps/account/rest/passkeys.py` — `on_passkeys_login_begin` soft-gate
    `passkey`.
14. `mojo/apps/account/rest/portal.py` *(new)* — public `GET /api/auth/portal`
    returning `public_portal_config(resolve_portal_config(group))` for
    `?group_uuid=`. Ensure the module is imported by `rest/__init__.py`.
15. Settings — add `AUTH_PORTAL` (JSON, global default) + `BOUNCER_PASSKEY_PATH`
    (`passkey`); retire the flat `AUTH_*` / `AUTH_REGISTER_*` keys.

No model/schema change → no migration, no `bin/create_testproject`.

### Design Decisions
- **Granular method tokens** (`google`/`apple`, not `oauth`): matches "control
  every login on/off" and is what templates need to render each button.
- **Soft enforcement only**: login endpoints gate a method *only* when
  `group_uuid` resolves a group. Absent/invalid `group_uuid` → no gating. This
  is a UX guardrail, not a security boundary (per request scope).
- **SMS-code login is new frontend work**: the hosted login page has no
  phone-code view today; required for "phone-code-only" groups to function.
  API endpoints (`/auth/sms/login`, `/auth/sms/verify`) already exist.
- **`custom_css` stays group-admin-editable** (already behind
  `manage_group`/`manage_groups`); `validate_custom_css` rejects `<`
  (HTML-breakout XSS) and external URL loading (`@import`, `http(s):`, `//`).
- **Config lives in `Group.metadata["portal"]`**: the group owns it, edited via
  the existing Group REST endpoint; `AUTH_PORTAL` is the deployment default.
- **`register_schema` kept separate** from `portal_config`: it owns form
  validation, only its config source changes.

### Edge Cases
- No `metadata.portal` and no `AUTH_PORTAL` → code defaults reproduce today's
  behavior (email+password register, all login methods, `passkey_prompt=off`).
- `passkey_prompt=required` + browser without WebAuthn → degrade to skippable
  with a message (UX-only, never strands the user).
- Registration returns `requires_verification` (no JWT) → skip the passkey
  step; enrollment needs a token.
- Passkey enroll page opened with no stored JWT → "session expired" + login
  link, no crash.
- Empty `login.methods` → rejected by `validate_portal_config` (would lock
  everyone out).
- Deep-merge: lists (`methods`, `fields`) replace wholesale; only dicts merge.
- Invalid `group_uuid` on a login request → group unresolved → no gating
  (login proceeds, matching today's tolerant middleware behavior).
- `custom_css` with `<` or external URL → `ValueException` on Group save.

### Testing
- `portal_config` resolve precedence (defaults ← `AUTH_PORTAL` ← group ←
  parent chain); list-replace vs dict-merge → `tests/test_portal/portal_config.py`
- `validate_portal_config` rejects bad method tokens, bad enums, malformed
  `fields`, empty `login.methods`, `custom_css` with `<` / `@import` / http URL
  → `tests/test_portal/portal_config.py`
- Saving invalid `metadata.portal` via Group REST returns a validation error
  → `tests/test_portal/portal_config.py`
- Default config reproduces today's register + login behavior
  → `tests/test_register/passkey_prompt.py`
- Password login rejected when `group_uuid` group disables `password`;
  succeeds with no `group_uuid` → `tests/test_auth/login_methods.py`
- SMS login rejected for an SMS-disabled group; passkey-login-begin rejected
  for a passkey-disabled group → `tests/test_auth/login_methods.py`
- `registration.enabled=False` blocks `on_register`
  → `tests/test_register/passkey_prompt.py`
- `passkey_prompt` rendered states (`off`/`optional`/`required`) on the
  enroll page; register page redirects when prompt ≠ off → template/JS-source
  assertions in `tests/test_register/passkey_prompt.py`
- Hosted login page omits password field for a password-disabled group;
  renders SMS-code view for an SMS-enabled group
  → `tests/test_auth/login_methods.py`
- Per-group theming: two groups render different branding for the same page
  → extend `tests/test_whitelabel/whitelabel.py`
- `GET /api/auth/portal` returns resolved safe-subset config
  → `tests/test_portal/portal_config.py`
- `mojo-auth.js` exposes `registerPasskey` / `startSmsLogin` / `verifySmsLogin`
  → JS-source assertions in `tests/test_auth/login_methods.py`
- Passkey login still issues a JWT end-to-end → reuse `tests/test_mfa/passkeys.py`

### Docs
- `docs/django_developer/account/auth_pages.md` — rewrite around the portal
  config; remove the retired `AUTH_*` table.
- `docs/web_developer/account/auth_pages.md` — same for API consumers.
- `docs/django_developer/account/portal_config.md` *(new)* — config schema,
  resolution order, validation rules, `Group.metadata["portal"]`.
- `docs/web_developer/account/portal_config.md` *(new)* — `GET /api/auth/portal`,
  per-group theming via `?group_uuid=`.
- `docs/django_developer/account/passkeys.md` + `docs/web_developer/account/passkeys.md`
  — add the enrollment-page flow and `registerPasskey()` helper.
- Update both `docs/*/account/README.md` indexes; update `CHANGELOG.md`.

## Resolution

**Status**: resolved
**Date**: 2026-05-20

### What Was Built
A group-owned `portal` config (`theme` / `registration` / `login`) resolved
per group from code defaults ← `AUTH_PORTAL` setting ← `group.metadata["portal"]`
down the parent chain. It drives the hosted login/register page theming and
which auth methods each group offers, adds a reusable `/passkey` enrollment
page, an SMS-code login view, and a public `GET /api/auth/portal` endpoint.
The flat `AUTH_*` / `AUTH_REGISTER_*` settings are retired. Login/registration
endpoints soft-gate disabled methods when a `group_uuid` resolves a group
(UX guardrail, not a security boundary).

### Files Changed
- `mojo/apps/account/services/portal_config.py` *(new)* — schema, resolution,
  validation, `public_portal_config`, `resolve_group_from_request`,
  `assert_login_method`.
- `mojo/apps/account/services/register_schema.py` — sources fields/identity/
  min-age from portal config; added `validate_fields_config`.
- `mojo/apps/account/models/group.py` — `on_rest_pre_save` validates `metadata.portal`.
- `mojo/apps/account/rest/bouncer/views.py` — `_auth_context` from portal config;
  new `/passkey` route + `on_passkey_enroll_page`.
- `mojo/apps/account/rest/portal.py` *(new)* — public `GET /api/auth/portal`.
- `mojo/apps/account/rest/{user,sms,oauth,passkeys}.py` — soft method-gating;
  `on_register` honors `registration.enabled`.
- `mojo/apps/account/rest/__init__.py` — registers the portal module.
- `mojo/apps/account/templates/account/{auth_base,login,register}.html` —
  method-gated rendering, SMS-code login view, passkey redirect.
- `mojo/apps/account/templates/account/passkey_enroll.html` *(new)*.
- `mojo/apps/account/static/account/mojo-auth.js` — `registerPasskey`,
  `startSmsLogin`/`verifySmsLogin`, `getPortalConfig`.

### Tests
- `tests/test_portal/portal_config.py` *(new)* — resolution precedence,
  deep-merge, validation, Group save-time guard, `GET /api/auth/portal`.
- `tests/test_auth/login_methods.py` *(new)* — `assert_login_method`, endpoint
  soft-gating, login.html method rendering, mojo-auth.js helper surface.
- `tests/test_register/passkey_prompt.py` *(new)* — `registration.enabled`
  gate, `passkey_prompt` rendering on register + passkey_enroll pages.
- `tests/test_register/schema.py`, `tests/test_whitelabel/whitelabel.py` —
  updated to the portal-config mechanism.
- Run: `bin/run_tests -t test_portal -t test_auth -t test_register -t test_whitelabel`
- Full suite: 2120 passed, 0 failed (56 opt-in slow tests skipped).

### Docs Updated
- `docs/django_developer/account/portal_config.md`,
  `docs/web_developer/account/portal_config.md` *(new)* — schema, resolution,
  validation, retired-settings migration table, API reference.
- `docs/django_developer/account/auth_pages.md`,
  `docs/web_developer/account/auth_pages.md` — rewritten around portal config.
- Both `docs/*/account/README.md` indexes; `CHANGELOG.md` (v1.2.21 entry).

### Security Review
No concerns. `validate_custom_css` (rejects `<` and external URLs) runs on
every Group write via `on_rest_pre_save`; the method soft-gate touches no
existing auth/verification gate and fails open only as intended (no group
context = no restriction); `GET /api/auth/portal` exposes nothing not already
public; the `X-Mojo-Test-Portal-Config` override is test-mode gated; the
passkey register/complete endpoints are unchanged.

### Follow-up
- `uv.lock` had a pre-existing `django-mojo` version mismatch (1.2.19 → 1.2.20)
  that `bin/run_tests` synced; left unstaged as it is unrelated to this work.
- `registration.methods` accepts a `password` token that is currently only
  enforced via `registration.enabled` (password signup is the form itself);
  OAuth provider tokens (`google`/`apple`) are fully enforced. Revisit if a
  distinct "OAuth-only signup" config is needed.
