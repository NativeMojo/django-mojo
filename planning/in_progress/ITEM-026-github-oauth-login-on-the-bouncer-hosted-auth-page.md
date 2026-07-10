---
# id is assigned by /scope on pickup — leave it blank
id: ITEM-026
type: feature
title: GitHub OAuth login on the bouncer hosted auth pages (provider exists, never wired in)
priority: P2
effort: S
owner:
opened: 2026-07-10
depends_on: []
related: [github-auth, oauth_registration_gate_and_new_user_flag, bouncer-white-label-auth-pages, linked_oauth_accounts]
links: []
---

# GitHub OAuth login on the bouncer hosted auth pages (provider exists, never wired in)

## What & Why
The bouncer-gated hosted auth pages (`/auth` login, `/register`) offer OAuth
sign-in buttons only for Google and Apple. GitHub OAuth is fully implemented and
working at the provider + REST-API level — `GitHubOAuthProvider` is registered,
the provider-agnostic `auth/oauth/github/begin → callback → complete` flow works
today, is tested, and is documented as a supported provider — but it was never
wired into the hosted pages: `github` is not in the `LOGIN_METHODS` allowlist
(so a group cannot even enable it via auth config; validation rejects it), no
GitHub button exists in the templates, and `mojo-auth.js` ships no
`startGithubLogin` wrapper.

The owner's expectation ("I thought we support this?") is half-right: the
framework supports GitHub OAuth for API consumers who render their own button
(that's all `github-auth` scoped), but the hosted gated page does not expose it.
This item closes that gap: let groups enable `github` as a login method and have
the hosted pages render/handle the button exactly like Google/Apple.

Not a regression — nothing previously worked and broke. Feature parity gap.

## Acceptance Criteria
_Approved by owner 2026-07-10: **default-on, full parity** (github joins both
method tuples, so it behaves exactly like google/apple everywhere — enabled by
default, group-toggleable, login + register)._

- [ ] `validate_auth_config` accepts `"github"` in both `login.methods` and
      `registration.methods` (no longer rejected as unknown).
- [ ] Hosted `/auth` login page renders `btn-github` when github is enabled —
      including for groups with no explicit config (default-on) — and omits it
      when a group's explicit `login.methods` excludes github.
- [ ] Hosted `/register` page renders `btn-github` per `registration.methods`,
      same default/exclusion behavior.
- [ ] Clicking the button drives the existing `auth/oauth/github/begin →
      callback → complete` flow to a finished sign-in on the hosted page,
      including the callback spinner mapping to the github button.
- [ ] A group with ONLY github enabled still shows the OAuth row (the
      `auth_base.html` section-hide guard accounts for github).
- [ ] `begin` now gates github per group config: 403 when a resolved group's
      `login.methods` excludes github; still ungated when no group context.
      The two stale "github is never gated" comments in `rest/oauth.py` are
      updated.
- [ ] New-user github signups honor the per-group `registration.methods` gate
      (403 at complete when the state group disables github registration).
- [ ] Tests: render tests (login + register), begin-gate 403/200, registration
      gate, and `ALL_LOGIN_METHODS` updated in `tests/test_auth_config/` —
      suite green vs baseline.
- [ ] Docs updated in both tracks (method-token lists + GitHub OAuth setup
      block with the CORRECT setting names) and a `CHANGELOG.md` entry that
      calls out the default-on appearance and the new gating behavior.

## Investigation

### What exists (reuse, don't reinvent)
- **Provider**: `mojo/apps/account/services/oauth/github.py` —
  `GitHubOAuthProvider` fully implemented (`get_auth_url` / `exchange_code` /
  `get_profile`, private-email fallback via `/user/emails` at github.py:88-99).
  Settings already read: `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` /
  `GITHUB_SCOPES` (github.py:35-48). Registered in `PROVIDERS` at
  `mojo/apps/account/services/oauth/__init__.py:6-10`.
- **REST flow** (provider-agnostic, works for github today):
  `mojo/apps/account/rest/oauth.py` — `GET auth/oauth/<provider>/begin` (:193),
  `GET|POST .../callback` (:261), `POST .../complete` (:313). Tested:
  `tests/test_oauth/oauth_github.py`.
- **Hosted-page method plumbing**: `mojo/apps/account/rest/bouncer/views.py`
  passes `login_methods` from `auth_config.resolve_auth_config(group).login.methods`
  (views.py:269) into the templates. Method allowlist lives at
  `mojo/apps/account/services/auth_config.py:39`
  (`LOGIN_METHODS = ("password", "sms", "passkey", "magic", "google", "apple")`)
  and `:42` (`REGISTRATION_METHODS = ("password", "google", "apple")`);
  `validate_auth_config` rejects unknown methods (auth_config.py:254 → 197-201).
- **Templates/JS**: shared OAuth-button partial
  `mojo/apps/account/templates/account/_login_method_buttons.html` (renders
  `#btn-google` / `#btn-apple`, guarded by membership in `login_methods`);
  `login.html:31,164` gates the OAuth row on google/apple only, JS handlers at
  login.html:283-290; `register.html:106-119,459-466` same pattern.
  `mojo/apps/account/static/account/mojo-auth.js` already has a **generic**
  `startOAuthLogin(provider)` (:648) — only the `startGoogleLogin` /
  `startAppleLogin` wrappers (:660-665) and buttons are provider-specific.

### What changes (file-level sketch)
1. `mojo/apps/account/services/auth_config.py:39` — add `"github"` to
   `LOGIN_METHODS` (and `:42` `REGISTRATION_METHODS` if register parity is in
   scope).
2. `mojo/apps/account/templates/account/_login_method_buttons.html` — GitHub
   button branch guarded by `'github' in login_methods`.
3. `mojo/apps/account/templates/account/login.html` (row-gating conditions +
   button handler) and `register.html` (same, if in scope).
4. `mojo/apps/account/static/account/mojo-auth.js` — `startGithubLogin` wrapper
   over the existing generic `startOAuthLogin(provider)`; update the docstring
   provider list (:641-642).
5. Docs both tracks: `docs/django_developer/account/auth_pages.md:263-272`
   (rendered-method token list; OAuth setup section :105-129),
   `docs/web_developer/account/auth_config.md:73`, and
   `docs/*/account/oauth.md` if begin-gating semantics change. `CHANGELOG.md`.

### Constraints / design wrinkles for /scope
- **Begin-endpoint gating**: `mojo/apps/account/rest/oauth.py:214-216`
  deliberately exempts non-google/apple providers from the per-group method
  check ("other providers (e.g. github) are never gated here"). Once `github`
  is in `LOGIN_METHODS` and group-toggleable, decide: does `begin` gate github
  like google/apple (consistent, fail-closed) or stay always-on (back-compat
  for API-only consumers with no auth config — note maestro downstream pins
  released django-mojo)? This is the one real design decision in the item.
- **Registration gate**: `oauth_registration_gate_and_new_user_flag` (done)
  added an OAuth registration gate — if github joins `REGISTRATION_METHODS`,
  confirm the gate/new-user-flag behavior applies identically.
- **Do not conflate**: `mojo/apps/github/` + `mojo/decorators/github.py` are
  the GitHub **App** server-to-server integration (installation tokens,
  webhooks) — unrelated to user OAuth sign-in; not touched by this item.
- White-label/theming: buttons must respect the same white-label treatment as
  the existing Google/Apple buttons (`bouncer-white-label-auth-pages`).

## Plan

### Goal
Make `github` a first-class, group-toggleable login **and** registration method
on the bouncer-hosted auth pages (`/auth`, `/register`), with full google/apple
parity — default-on, gated per group, buttons wired end-to-end. No provider or
REST functional changes: those layers are already generic.

### Context — what exists

**Provider + REST are done and generic — do not touch functionally.**
- `mojo/apps/account/services/oauth/github.py` — `GitHubOAuthProvider`
  (`get_auth_url` :34-43, `exchange_code`, `get_profile` with private-email
  fallback :88-99). Settings: `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` /
  `GITHUB_SCOPES` (default `"read:user user:email"`). Registered in `PROVIDERS`
  at `mojo/apps/account/services/oauth/__init__.py:7-9`.
- `mojo/apps/account/rest/oauth.py` — `on_oauth_begin` (:193), `on_oauth_callback`
  (:261), `on_oauth_complete` (:313), all `<str:provider>`-generic. GitHub uses a
  plain GET callback like Google — none of Apple's quirks (`form_post` POST
  callback, ES256 JWT client secret, `id_token` decode) apply.
- No model change: `models/oauth.py` stores `provider` as a free CharField
  (only a `PROVIDER_GOOGLE` const exists at :17; apple has none either).

**The method system is tuple-driven — one edit fans out everywhere.**
`mojo/apps/account/services/auth_config.py:38-44`:
```python
LOGIN_METHODS = ("password", "sms", "passkey", "magic", "google", "apple")
REGISTRATION_METHODS = ("password", "google", "apple")
```
- `DEFAULT_AUTH_CONFIG` (:49-81) sets `login.methods = list(LOGIN_METHODS)` and
  `registration.methods = list(REGISTRATION_METHODS)` — so tuple membership ⇒
  **on by default** for any group without an explicit list (explicit lists
  replace wholesale; lists never deep-merge).
- `_validate_methods` (:194-201) rejects unknown tokens, called at :254 (login)
  and :265 (registration) — data-driven, no edit beyond the tuples.
- `resolve_auth_config(group, request)` (:135-159): code default ← `AUTH_CONFIG`
  Setting ← `group.metadata["auth_config"]` walked root→leaf; last wins.
- `assert_login_method(method, group)` (:293-306): no-op when `group is None`,
  else 403 `PermissionDeniedException` if method not in resolved
  `cfg.login.methods`.
- Begin gate, `rest/oauth.py:214-218`: `if provider in auth_config.LOGIN_METHODS:
  assert_login_method(provider, resolve_group_from_request(request))` — comment
  above it says "other providers (e.g. github) are never gated here" (goes stale).
- Registration gate for brand-new oauth users, `_find_or_create_user`
  (:101-186): create path checks `OAUTH_ALLOW_REGISTRATION` (:138), then
  `if provider_name in auth_config.REGISTRATION_METHODS:` (:143) resolves the
  state group and 403s when the group's `registration.methods` excludes the
  provider. Comment at :141-142 ("Only applies to ... google/apple") goes stale.
  Returns `(user, conn, created)`; new users fire `USER_REGISTERED_HANDLER`.

**Hosted-page rendering flow.**
- `mojo/apps/account/rest/bouncer/views.py` `_auth_context` (:254-350):
  `login_methods = list(cfg.login.methods or [])` (:269),
  `registration_methods` (:270), passed into template context (:325, :331).
  **No view change needed** — github flows through once resolved.
- OAuth round-trip: the hosted page itself is the OAuth landing. JS
  `startOAuthLogin(provider)` stores `sessionStorage.oauth_provider`, GETs
  `begin?redirect_uri=<current page>`; backend callback 302s the browser back
  to `<page>?code&state[&group_uuid]`; the shared handler in `auth_base.html`
  calls `completeOAuthLogin(provider)` → JWT. Group branding survives via
  `state_extra["group_uuid"]` (`oauth.py:241-243, 301-303`) — provider-agnostic.

**Templates/JS — where google/apple are hardwired (the actual gap):**
- `templates/account/_login_method_buttons.html` — login-page partial rendering
  `btn-passkey` / `btn-google` (:14) / `btn-apple` (:20), each guarded by
  `{% if '<m>' in login_methods %}`. Buttons are plain
  `mat-btn mat-btn-outline` + inline SVG + `mat-btn-text` + `mat-spinner`.
- `templates/account/login.html` — OAuth-row gates listing providers explicitly
  at :31 (signin-primary) and :164 (sms-primary); `btn-google`/`btn-apple`
  click handlers at :283-291 calling `MojoAuth.startGoogleLogin()` /
  `startAppleLogin()`. (Also its own duplicate callback handler :240-249 —
  see Edge cases.)
- `templates/account/register.html` — does NOT use the partial; inlines
  buttons gated on `registration_methods` at :106-123, handlers at :459-467
  (`on(...)` helper defined :457).
- `templates/account/auth_base.html` — **both pages extend this; it needs 4
  edits**:
  - :71-74 `hasMethod()` flags: `ENABLE_GOOGLE`, `ENABLE_APPLE`,
    `ENABLE_PASSKEYS` (fed from `{{ login_methods|json_script }}` :51).
  - :204-217 shared OAuth callback handler; spinner button picked via
    `$(provider === "apple" ? "btn-apple" : "btn-google")` (:208) —
    mis-selects google's button for github.
  - :235-238 hide-disabled-buttons block (`if (!ENABLE_GOOGLE) ...`).
  - :241-245 **section-hide guard**: `if (!ENABLE_GOOGLE && !ENABLE_APPLE &&
    !passkeysVisible)` hides every `.mat-divider` and `.mat-oauth-row` — a
    github-ONLY config would hide the github button unless this includes github.
  - `window._matConfig` (:87-101) exposes `enableGoogle`/`enableApple`-style
    flags — add github for parity.
- `static/account/mojo-auth.js` — `startOAuthLogin(provider, callbackUrl)`
  generic (:648-658); wrappers `startGoogleLogin` (:660-662) / `startAppleLogin`
  (:664-666); `completeOAuthLogin(provider)` (:675-684) generic; docstring
  provider list at :641-642 says "'google', 'apple'".

**Unconfigured-provider behavior (mirror, don't "fix"):** there is no
begin-time config check for google or github — unset `GITHUB_CLIENT_ID` yields
a 200 `auth_url` containing `client_id=None`; the error surfaces on GitHub's
authorize page. Identical to google today.

**Test landscape:**
- `tests/test_oauth/oauth_github.py` — 6 `@th.django_unit_test` cases: registry,
  auth-url (direct settings set w/ try/finally), `opts.client.get("/api/auth/
  oauth/github/begin")`, email selection, autolink/new-user via direct
  `_find_or_create_user("github", profile)`. No HTTP mocking anywhere —
  provider logic is driven directly. `GITHUB_CLIENT_ID = "test-client-id-123"`
  is pinned in `testproject/config/settings/local/__init__.py:16`
  (parallel-safe; no `server_settings` reload needed).
- `tests/test_auth_config/auth_config.py:23` hardcodes
  `ALL_LOGIN_METHODS = ["password", "sms", "passkey", "magic", "google",
  "apple"]`, asserted by `test_resolve_defaults` (:77-78) and
  `test_public_config_endpoint_default` (:255-256) — **adding github to the
  tuple breaks these two until the list is updated**. Grep the file for any
  other hardcoded method lists (registration) while there.
- `tests/test_auth/login_methods.py` — the render-test pattern to mirror
  (`_render_login` :135-144): `RequestFactory().get('/auth')` + `_auth_context`
  + `render(request, 'account/login.html', ctx)`, then assert on markup ids.
  Bypasses the bouncer gate entirely — no `mbp` cookie needed. Groups created
  with explicit `uuid=` + `is_active=True` +
  `metadata={"auth_config": {"login": {"methods": [...]}}}` (:46-51).
- `tests/test_auth/bouncer_forms.py:41-53` — parallel `_render(template_name,
  group=None)` that renders `register.html` the same way.
- Registration-gate test pattern: `tests/test_oauth/oauth.py:280-302` (direct
  `_find_or_create_user` + Django setting flip in try/finally).
- Hygiene: `Group.objects.create()` leaves `uuid=None` (lazily assigned) —
  always pass `uuid=` explicitly when the test drives `group_uuid` params;
  numeric/uuid group resolution filters `is_active=True`. Clean up test rows
  before creating (long-lived DB).

### Changes — what to do
1. `mojo/apps/account/services/auth_config.py:39,42` — add `"github"` to
   `LOGIN_METHODS` and `REGISTRATION_METHODS`. This single change makes the
   token valid in config validation, default-on via `DEFAULT_AUTH_CONFIG`, and
   opts github into both REST gates (begin :216, registration :143).
2. `mojo/apps/account/templates/account/_login_method_buttons.html` — add a
   `{% if 'github' in login_methods %}` block with `id="btn-github"`, mirroring
   the apple block (:20-25): `mat-btn mat-btn-outline`, GitHub octocat-mark SVG
   (`fill="currentColor"`, 18×18, `aria-hidden="true"`), label "GitHub",
   `mat-spinner`. Update the partial's top comment ("Passkey / Google / Apple").
3. `mojo/apps/account/templates/account/login.html` — add
   `or 'github' in login_methods` to BOTH row gates (:31 and :164); add a
   `btn-github` click handler next to :283-291 calling
   `MojoAuth.startGitHubLogin()` with the same setLoading/error pattern
   ("Could not start GitHub sign-in.").
4. `mojo/apps/account/templates/account/register.html` — add
   `or 'github' in registration_methods` to the divider gate (:106); add an
   inline `{% if 'github' in registration_methods %}` `btn-github` button block
   mirroring :116-121; add the click handler next to :459-467
   ("Could not start GitHub sign-up.").
5. `mojo/apps/account/templates/account/auth_base.html` —
   a. :71-74 add `var ENABLE_GITHUB = hasMethod("github");`
   b. :208 replace the two-way ternary with `var oauthBtn = $("btn-" + provider);`
      (null-safe — the very next line already guards `if (oauthBtn)`).
   c. :235-238 add `if (!ENABLE_GITHUB) { var gh = $("btn-github"); if (gh)
      gh.style.display = "none"; }`
   d. :241-245 section-hide guard becomes `if (!ENABLE_GOOGLE && !ENABLE_APPLE
      && !ENABLE_GITHUB && !passkeysVisible)`.
   e. `window._matConfig` (:87-101): add the github flag alongside the existing
      google/apple ones (match their key naming).
6. `mojo/apps/account/static/account/mojo-auth.js` — add
   `startGitHubLogin: function (callbackUrl) { return
   MojoAuth.startOAuthLogin('github', callbackUrl); }` after :666; update the
   `startOAuthLogin` docstring provider list (:641-642) to include 'github'.
7. `mojo/apps/account/rest/oauth.py` — comments only: rewrite :214-215 (begin
   gate) and :141-142 (registration gate) to say the gate applies to any
   provider present in the method tuples (google/apple/github today) and is
   skipped for providers outside them. No functional edits.
8. `tests/test_auth_config/auth_config.py:23` — append `"github"` to
   `ALL_LOGIN_METHODS`; fix any other hardcoded method-list assertions the
   change trips (check registration defaults too).
9. New tests — see Tests section.
10. Docs + CHANGELOG — see Docs section.

### Design decisions
- **Default-on, full parity (owner-approved 2026-07-10).** github joins both
  tuples and inherits `DEFAULT_AUTH_CONFIG` membership. Rejected: opt-in
  (excluding github from defaults) — it would 403 existing API consumers who
  pass `group_uuid` on github begin (group resolves → github absent from its
  defaulted methods), diverge from the tuple-driven design, and need a
  special-cased default list. The default-on upgrade surprise (a GitHub button
  appearing on default-config deployments) is the same posture google/apple
  already have and is handled by a loud CHANGELOG entry.
- **Register parity included (owner-approved).** Without it, a brand-new GitHub
  user clicking "Sign in" on the login page would still get an account created
  with NO per-group gate (the :143 gate only applies to tuple members) —
  login-only would silently leave github signups ungated.
- **No config-presence check before rendering the button.** An enabled-but-
  unconfigured github renders a button that dead-ends on GitHub's error page —
  exactly google's current behavior. Rejected: reading `GITHUB_CLIENT_ID` in
  `_auth_context` — inconsistent with google/apple and scope creep.
- **`$("btn-" + provider)` replaces the callback ternary** — correct spinner for
  all three providers, null-safe for junk sessionStorage values, and removes a
  per-provider branch instead of adding one.
- **Wrapper `startGitHubLogin`** rather than calling the generic inline —
  consistent with the google/apple API surface web devs already use.
- **Render tests via `RequestFactory` + `_auth_context` + `render`** (the
  `login_methods.py` pattern) — no bouncer bypass, no `mbp` cookie, no rate-
  limit hygiene needed.

### Edge cases & risks
- **GitHub-only group** (`login.methods: ["github"]`): handled by 5d — without
  it the section-hide guard hides the row containing the only enabled button.
  The markup-level render test asserts the button exists; the JS guard fix is
  code-reviewed, not unit-tested (no DOM runtime in testit).
- **Behavior change on upgrade (intended, must be called out):** a group with
  an explicit `login.methods` that omits github, whose users hit github begin
  WITH that `group_uuid`, now gets 403 — that is the operator's stated config
  finally being enforced. No-group API flows stay ungated
  (`assert_login_method` no-ops on `group=None`).
- **New-user signups via the login button** are now group-gated through
  `registration.methods` — a group disabling github registration 403s at
  complete ("Account registration via this provider is not permitted");
  existing users (OAuthConnection or email match) still log in.
- **Unset `GITHUB_CLIENT_ID`** → begin 200 with `client_id=None` in the URL,
  GitHub shows the error. Mirrors google; setup docs say to configure
  credentials before enabling.
- **Pre-existing duplicate-callback quirk (out of scope, do not fix here):**
  on `/auth`, both `auth_base.html:205` and `login.html:243` fire on
  `?code&state`; login.html re-fires with provider defaulted to "google" after
  auth_base consumed the state — a harmless late error flash behind the
  success overlay. GitHub inherits it exactly as google/apple do.
- **Test hygiene:** unique group names/uuids per test, explicit `uuid=`,
  `is_active=True`, cleanup-before-create, and run the baseline suite before
  any edit (build rule).

### Tests
All testit (`from testit import helpers as th`, `@th.django_unit_test()`,
descriptive assert messages). Run `bin/run_tests --agent` for a green baseline
FIRST, then per-module during work.

- `tests/test_auth/login_methods.py` (extend, mirror `_render_login`):
  1. Group with `login.methods: ["github"]` → rendered login.html contains
     `id="btn-github"`; 2. group with `["password", "sms"]` → no `btn-github`;
  3. `group=None` (defaults) → `btn-github` present — pins the default-on
     decision.
- `tests/test_auth/bouncer_forms.py` (extend, mirror `_render` on
  register.html): `registration.methods` including github → `btn-github`
  present; explicit list omitting github → absent.
- `tests/test_oauth/oauth_github.py` (extend):
  - Begin gate: active group with `metadata.auth_config.login.methods`
    excluding github, `opts.client.get(".../github/begin?group_uuid=<uuid>")`
    → 403; same call with a default-config group → 200 with `auth_url`.
  - Registration gate: `_find_or_create_user("github", profile, state_data)`
    with state carrying a group whose `registration.methods` excludes github →
    `PermissionDeniedException`; permissive group → `(user, conn, created=True)`.
    Mirror `tests/test_oauth/oauth.py:280-302` and the `_resolve_state_group`
    path for how the group rides in `state_data` (verify exact signature).
- `tests/test_auth_config/auth_config.py`: update `ALL_LOGIN_METHODS` (:23);
  confirm `test_validate_bad_method` still passes (data-driven) and defaults
  assertions (:77-78, :255-256) now expect github.

### Docs
- `docs/django_developer/account/auth_pages.md`:
  - Token list :263-272 — add `github` to the sentence and a
    "`github` — GitHub OAuth redirect flow" bullet.
  - :287-288 parenthetical "(SMS, passkey, Google, Apple)" → include GitHub.
  - :299 registration tokens — add `github`.
  - OAuth setup :105-129 — add a GitHub block using the REAL setting names
    `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` (+ optional `GITHUB_SCOPES`),
    add `"github"` to the example `login.methods`, and FIX the existing
    mismatch: docs say `GOOGLE_OAUTH_CLIENT_ID` / `APPLE_OAUTH_CLIENT_ID` but
    code reads `GOOGLE_CLIENT_ID` (`google.py:30`) / `APPLE_CLIENT_ID`
    (`apple.py:46`) — code is the truth, correct the doc in passing.
- `docs/web_developer/account/auth_config.md`: methods guidance (~:73) and the
  soft-gating "Affected endpoints" list (:111) — add github begin.
- `docs/django_developer/account/oauth.md` + `docs/web_developer/account/oauth.md`:
  one line each noting github is toggleable per group via
  `auth_config.login.methods` / `registration.methods`, like google/apple
  (github sections already exist; no other changes).
- `CHANGELOG.md` under `## Unreleased`: `**feature** — **GitHub OAuth is now a
  hosted-page login/registration method.**` — prose must call out (a) button
  appears by default for groups without an explicit methods list (disable via
  `login.methods`), (b) github begin/registration are now gated by group
  config like google/apple. End with `(ITEM-026)`.
- No README index updates (no new doc files).

### Open questions
- None — default enablement and register parity were the two open calls; both
  resolved by owner 2026-07-10 (default-on, full parity).

## Notes
- **Baseline (2026-07-10, before any edit):** `bin/run_tests --agent` → status
  passed, total 2395, passed 2339, failed 0, skipped 56 (opt-in modules
  test_incident/test_security + env-gated skips). All green — any later
  failure is attributable to this build.
- Filed from chat 2026-07-10: "i don't think our api bouncer gated logic page
  allows for github login oauth. I thought we support this?" — confirmed by
  exploration: provider + REST flow exist and are documented as supported
  (docs/web_developer/account/oauth.md:5, docs/django_developer/account/oauth.md:7);
  the hosted gated pages never exposed it (github absent from LOGIN_METHODS,
  templates, and mojo-auth.js wrappers).

## Resolution
- closed: YYYY-MM-DD
- branch:
- files changed:
- tests added:
