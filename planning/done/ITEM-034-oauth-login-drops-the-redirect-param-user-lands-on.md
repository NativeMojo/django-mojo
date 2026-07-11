---
# id is assigned by /scope on pickup — leave it blank
id: ITEM-034
type: bug
title: OAuth login drops the ?redirect= param — user lands on / instead of back on the app
priority: P1
effort: S
owner: backend
opened: 2026-07-10
depends_on: []
related: []
links: [file:///Users/ians/Projects/mojo/maestro/api/planning/requests/bouncer-auth-portals.md]
---

# OAuth login drops the `?redirect=` param — user lands on `/` instead of back on the app

## What & Why
When a user reaches `/auth?redirect=<path>` and signs in with an OAuth provider
(Google/Apple/GitHub), the `redirect` query param is lost during the OAuth
round-trip, so after login the auth page falls back to its `ON_SUCCESS` default
(`/`) instead of returning the user to the app that sent them. Password, magic-link,
and passkey logins never leave the page, so their query string survives — only
OAuth breaks. Hit in production by maestro (2026-07-10): Google sign-in from
`https://maestromojo.com/auth?redirect=/workspaces/` lands on the homepage.
Framework-first rule: fix belongs here, not in a consumer workaround.

## Acceptance Criteria
- [ ] `MojoAuth.startOAuthLogin()` (no explicit `callbackUrl`) preserves the current
      page's query string in the default `redirect_uri`, minus any stale
      `code`/`state` params (so a retried login after a failed completion is clean)
- [ ] `on_oauth_callback` bounces to a well-formed URL when `frontend_uri` already
      carries a query — joins with `&`, never a second `?`; exactly one `code`/`state`
      pair in the result
- [ ] End-to-end: begin at `/auth?redirect=%2Fworkspaces%2F` → provider → callback
      bounce → `redirect` param still present on the auth page → post-login nav goes
      to `/workspaces/`
- [ ] Regression tests in `tests/test_oauth/oauth.py` cover both halves
- [ ] Docs + `CHANGELOG.md` updated

## Repro — bugs only
1. Visit `/auth?redirect=%2Fworkspaces%2F%23%2F` while logged out.
2. Click "Sign in with Google" (`MojoAuth.startGoogleLogin()` with no callback URL).
3. Complete the Google flow; the provider redirects to the backend callback, which
   bounces the browser back to the auth page with `?code=…&state=…`.
- Expected: browser returns to `/auth?redirect=%2Fworkspaces%2F%23%2F&code=…&state=…`;
  after token completion the page navigates to `/workspaces/#/`.
- Actual: browser returns to `/auth?code=…&state=…` (no `redirect`); the page falls
  back to `ON_SUCCESS` default `/` and the user lands on the homepage.

## Investigation
Root cause **confirmed** (code inspected at HEAD; matches the prod symptom traced
in the maestro 1.2.45 checkout — see linked maestro planning file for the full trace).

Two defects, both needed for the fix:

1. **Client drops the query string.**
   `mojo/apps/account/static/account/mojo-auth.js:650` — `startOAuthLogin()`:
   ```js
   var redirectUri = callbackUrl || (window.location.origin + window.location.pathname);
   ```
   The default return URL strips `location.search`, so `?redirect=` never enters the
   OAuth state. `on_oauth_begin` (`mojo/apps/account/rest/oauth.py:222-236`) stores
   this stripped URL as `frontend_uri`.

2. **Server joins the bounce URL naively.**
   `mojo/apps/account/rest/oauth.py:308` — `on_oauth_callback`:
   ```python
   return HttpResponseRedirect(f"{frontend_uri}?{params}")
   ```
   Once fix 1 makes `frontend_uri` carry a query, this produces a malformed
   double-`?` URL. Needs a proper join (`urlsplit`/`urlunsplit`, `&` when a query
   already exists).

Consumer side is already correct: `auth_base.html` reads `params.get("redirect")`
post-completion; it just never receives it.

Proposed fix sketch (from maestro planning, verified against this code):
- `startOAuthLogin()`: keep `location.search` in the default `redirectUri`, deleting
  `code` and `state` first (stale from a previous failed completion). Fixing the
  library default (not `login.html`) also covers the register page's OAuth buttons
  and every external consumer.
- `on_oauth_callback`: `urlsplit(frontend_uri)`, append params to the existing query
  with `&`, `urlunsplit` back.

Edge cases checked:
- **Allowlist:** `_validate_redirect_uri` (`oauth.py:50`) is a prefix match against
  `ALLOWED_REDIRECT_URLS` — appending a query string to the URI changes nothing.
- **Deep links:** the app hash lives percent-encoded *inside* the `redirect` param
  value, never as a bare fragment — survives both legs.
- **`group_uuid`** may now appear in both the preserved query and the callback's
  appended params — same value; harmless (1.2.44+ `request.DATA` is
  later-source-wins).
- The cross-origin token-handoff branch in `auth_base.html` is untouched.

Regression-test feasibility: **good** — existing suite `tests/test_oauth/oauth.py`
exercises begin/callback mechanics; add: (a) begin with
`redirect_uri=https://…/auth?redirect=%2Fworkspaces%2F` → allowlist passes, state
stores the full URI; (b) callback bounce preserves `redirect=`, exactly one `?`,
exactly one `code`/`state` pair. The Google leg itself is prod-only; the tests cover
the round-trip mechanics.

Related files: `mojo/apps/account/static/account/mojo-auth.js`,
`mojo/apps/account/rest/oauth.py`, `tests/test_oauth/oauth.py`, docs both tracks,
`CHANGELOG.md`. Downstream: maestro wants this published as 1.2.46 and will relock
(see linked file, "Plan — part 2").

## Plan

### Goal
Make the `?redirect=` query param survive the OAuth round-trip so a user who
signs in with Google/Apple/GitHub from `/auth?redirect=<path>` lands back on
`<path>`, exactly like password/passkey/magic-link logins already do.

### Context — what exists
- **JS default drops the query.** `mojo/apps/account/static/account/mojo-auth.js:648-651`,
  `startOAuthLogin(provider, callbackUrl)`:
  ```js
  var redirectUri = callbackUrl || (window.location.origin + window.location.pathname);
  var url = ep('oauthBegin', { provider: provider }) + '?redirect_uri=' + encodeURIComponent(redirectUri);
  ```
  `location.search` (which carries `?redirect=`) is stripped. Both `login.html:285`
  and `register.html:467` call `MojoAuth.startGoogleLogin()` with **no**
  `callbackUrl`, so both hit this default. The JSDoc at `mojo-auth.js:643` documents
  the strip ("Defaults to the current page URL (strip query/hash)").
- **Server stores it as-is.** `mojo/apps/account/rest/oauth.py:222-236`
  (`on_oauth_begin`): `request.DATA.get("redirect_uri")` →
  `_validate_redirect_uri()` (line 50, **prefix** match against
  `ALLOWED_REDIRECT_URLS`) → stored as `frontend_uri` in Redis-backed state
  (`mojo/apps/account/services/oauth/base.py:29-63` — `create_state` = uuid key +
  `setex` TTL 600s, `peek_state` = plain get, no Google call needed to round-trip).
- **Server bounce is a naive join.** `mojo/apps/account/rest/oauth.py:298-308`
  (`on_oauth_callback`): builds `code`/`state` (+ optional `group_uuid`) params and
  returns `HttpResponseRedirect(f"{frontend_uri}?{params}")` — malformed
  (double `?`) once `frontend_uri` carries a query. Imports at `oauth.py:15` are
  `from urllib.parse import urlencode, quote` — **no** `urlsplit`/`urlunsplit` yet.
- **Consumer side is already correct.** `templates/account/auth_base.html:78-79`
  reads `params.get("redirect") || params.get("next") || params.get("returnTo")`,
  falls back to `ON_SUCCESS` (`/`). OAuth completion trigger at
  `auth_base.html:207-219` (and a duplicate block in `login.html:240-249`):
  `if (params.get("code") && params.get("state"))` → `completeOAuthLogin()` →
  `window._mat.onAuthSuccess` → `window._mat.redirect()` navigates to
  `redirectTo`. If `redirect=` is present on the bounce URL it is honored — it
  just never arrives today.
- **Tests.** `tests/test_oauth/oauth.py` (716 lines), testit pattern
  (`@th.django_unit_test("...")`, `def test_xxx(opts)`). Begin endpoint:
  `GET /api/auth/oauth/google/begin?redirect_uri=...` (`oauth.py:77`,
  `PROVIDER = "google"`). Callback: real round-trip without Google —
  `test_oauth_callback_redirects` (`oauth.py:92-116`) does
  `svc = get_provider(PROVIDER)`;
  `state = svc.create_state(extra={"redirect_uri": ..., "frontend_uri": ...})`;
  `opts.client.get(f"/api/auth/oauth/{PROVIDER}/callback?code=testcode123&state={state}", allow_redirects=False)`
  and asserts 302 (it does NOT currently inspect `Location` — the natural
  regression point). `ALLOWED_REDIRECT_URLS` is pinned in the test project
  settings (see comment `oauth.py:75-76`); existing tests use
  `https://example.com/login` and `http://localhost:9009/...` as allowed frontend
  URIs — reuse those bases.

### Changes — what to do
1. `mojo/apps/account/static/account/mojo-auth.js:648-651` — preserve the query
   string in the default return URL, minus `code`/`state` (stale from a previous
   failed completion must not be re-sent):
   ```js
   var qp = new URLSearchParams(window.location.search);
   qp.delete('code'); qp.delete('state');
   var qs = qp.toString();
   var redirectUri = callbackUrl ||
       (window.location.origin + window.location.pathname + (qs ? '?' + qs : ''));
   ```
   Update the JSDoc at `mojo-auth.js:642-645` to say the default keeps the query
   string (minus `code`/`state`) and strips only the hash.
2. `mojo/apps/account/rest/oauth.py:15` — extend the import to
   `from urllib.parse import urlencode, quote, urlsplit, urlunsplit`.
3. `mojo/apps/account/rest/oauth.py:307-308` — join the bounce URL properly:
   ```python
   parts = urlsplit(frontend_uri)
   query = f"{parts.query}&{params}" if parts.query else params
   return HttpResponseRedirect(urlunsplit(
       (parts.scheme, parts.netloc, parts.path, query, parts.fragment)))
   ```
4. `tests/test_oauth/oauth.py` — two regression tests (see Tests).
5. Docs + `CHANGELOG.md` (see Docs).

### Design decisions
- **Fix the library default, not `login.html`** — covers the register page's OAuth
  buttons and every external consumer of `mojo-auth.js` in one place.
- **Strip `code`/`state` in the JS, not the server** — they are the only params the
  auth page itself appends between visits; deleting them client-side keeps the
  server join dumb (KISS) and avoids a retried login carrying stale completion
  params. No server-side dedup of the preserved query.
- **`urlsplit`/`urlunsplit` join instead of string concat** — also fixes the latent
  case of a `frontend_uri` with a fragment (naive concat would put `?code=…` after
  the `#`, hiding code/state from the server-visible URL and breaking completion).
- **No allowlist change** — `_validate_redirect_uri` is a prefix match; a query
  string appended to an allowed URL still matches.
- **Publish/version bump is out of scope for /build** — release is user-driven
  (interactive `publish.py`); the CHANGELOG entry goes in the rolling top block.
  Downstream maestro relock happens in the maestro repo after publish.

### Edge cases & risks
- **Stale `code`/`state` on retry** — handled by the JS `qp.delete()` before
  building the default `redirectUri`.
- **`group_uuid` may now appear twice** (preserved query + callback-appended) —
  same value; harmless since `request.DATA` is later-source-wins (1.2.44+).
- **`frontend_uri` with existing query** — joined with `&` (change 3); with a
  fragment — `urlunsplit` keeps the query before the fragment.
- **Deep links** — the app hash lives percent-encoded *inside* the `redirect`
  param value (e.g. `redirect=%2Fworkspaces%2F%23%2F`), never as a bare fragment;
  survives both legs unchanged.
- **Cross-origin token-handoff branch** in `auth_base.html` — untouched; this fix
  only changes what URL the browser returns to and how the server joins params.
- **JS caching** — `mojo-auth.js` is served with `Cache-Control: public,
  max-age=86400` in non-DEBUG (`mojo/apps/account/rest/bouncer/static.py:30-51`,
  no cache-buster), so deployed browsers may run the old JS for up to 24h after a
  prod upgrade. Accept; no change.
- **Behavior change for external consumers**: any consumer relying on the old
  strip-the-query default would now get its own query params echoed back on its
  callback URL. That is the documented intent of a "return to current page"
  default and matches how the non-OAuth flows already behave; note it in the
  CHANGELOG entry.

### Tests
Both in `tests/test_oauth/oauth.py`, testit conventions (descriptive assert
messages; setup cleans before create — not needed here, no rows created).
1. **Begin keeps the full URI** — `test_oauth_begin_preserves_query_in_frontend_uri`:
   `GET /api/auth/oauth/google/begin?redirect_uri=` +
   `quote("https://example.com/login?redirect=%2Fworkspaces%2F%23%2F", safe="")`
   → 200 (allowlist prefix still passes); extract `state` from the returned
   `auth_url` query, `svc.peek_state(state)["frontend_uri"]` equals the full URI
   including its query. (Guards the begin/allowlist leg — a fail here while JS is
   fixed would silently reintroduce the bug.)
2. **Callback joins, doesn't clobber** — `test_oauth_callback_preserves_frontend_query`:
   `svc.create_state(extra={"redirect_uri": <callback>, "frontend_uri":
   "http://localhost:9009/auth?redirect=%2Fworkspaces%2F%23%2F"})`; GET the
   callback with `code=testcode123&state=<state>`, `allow_redirects=False`;
   assert 302 and parse `Location`: exactly one `?`; query contains
   `redirect=/workspaces/#/` (parse with `parse_qs`, don't substring-match the
   encoding), exactly one `code` (= `testcode123`) and one `state` value.
   While broken this fails on the malformed double-`?` URL / missing redirect;
   fixed, it passes.

The Google leg itself is prod-only (no local creds) — these cover the full
begin→callback mechanics the fix touches. Run:
`bin/run_tests --agent -t test_oauth.oauth`.

### Docs
- `docs/django_developer/account/oauth.md` — "Per-Request redirect_uri" (line 95):
  default JS `redirect_uri` now keeps the page's query string (minus
  `code`/`state`); callback merges its params into an existing query.
- `docs/django_developer/account/auth_pages.md` — login-page section (~line 271):
  `?redirect=` now survives OAuth logins too.
- `docs/web_developer/account/oauth.md` — "JavaScript Example" (line 182) /
  flow: same default-behavior note for consumers.
- `docs/web_developer/account/auth_pages.md` — "URL Parameters" table (line 58):
  currently implies `?redirect=` is always preserved; state explicitly that it
  survives OAuth as of this fix.
- `CHANGELOG.md` — v1.2.46 was released 2026-07-10, so start a new rolling block
  above it for this entry:
  `**bug** — **OAuth login preserves ?redirect= across the provider round-trip** … (ITEM-034)`.

### Open questions
- none

## Notes
Filed from maestro's planning doc (`bouncer-auth-portals.md`, follow-up section
dated 2026-07-10), which contains the full prod trace and the downstream
publish/relock steps that stay in the maestro repo.

**Baseline (2026-07-10, before first edit)** — `bin/run_tests --agent`, per
`var/test_failures.json`: status `passed`, total 2423, passed 2367, failed 0,
skipped 56. Green baseline — every post-change failure is mine. (`test_incident`
243 + `test_security` 82 shown "failed" in the terminal are opt-in `--full`
modules excluded from the default agent suite — not part of the baseline.)

## Resolution
- closed: 2026-07-11
- branch: main
- files changed: CHANGELOG.md,docs/django_developer/account/bouncer.md,docs/django_developer/account/oauth.md,docs/web_developer/account/auth_pages.md,docs/web_developer/account/oauth.md,mojo/apps/account/rest/oauth.py,mojo/apps/account/static/account/mojo-auth.js,planning/.next_id,planning/in_progress/ITEM-034-oauth-login-drops-the-redirect-param-user-lands-on.md,tests/test_oauth/oauth.py,uv.lock
- tests added:
  - `tests/test_oauth/oauth.py::test_oauth_callback_preserves_frontend_query` —
    genuine regression: callback merges code/state into an existing frontend
    query with `&` (fails pre-fix on the malformed double-`?` URL, redirect lost)
  - `tests/test_oauth/oauth.py::test_oauth_begin_preserves_query_in_frontend_uri` —
    guard: a query-carrying redirect_uri passes the allowlist and is stored
    verbatim as frontend_uri
  - `tests/test_oauth/oauth.py::test_oauth_callback_strips_smuggled_params` —
    security: a smuggled `code`/`state` in frontend_uri's query is stripped so the
    server-set values are authoritative (prevents first-match shadowing that would
    sabotage a victim's login); the app's own `?redirect=` still survives

### Post-build agent results
- **test-runner**: full suite green — 2370 passed / 0 failed / 56 skipped (two
  `test_jobs` scheduled-task tests flaked on an hour-boundary timing assumption,
  unrelated to this change — passed on re-run; latent flakiness noted for a future
  item).
- **docs-updater**: verified both tracks; additionally updated
  `docs/django_developer/account/bouncer.md` "OAuth round-trip" section, which
  still described the pre-fix naive-append behavior.
- **security-review**: flagged a param-shadowing hole newly reachable once the
  bounce URL became well-formed — fixed in this item (server-side strip of
  reserved keys in `on_oauth_callback` + regression test above). Remaining INFO:
  consuming SPAs must validate any `?redirect=` client-side before navigating
  (pre-existing for all login methods; noted in docs).
