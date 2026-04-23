# auth_base.html session-check block hijacks /register and /contact pages

**Type**: bug
**Status**: open
**Date**: 2026-04-22
**Severity**: high

## Description

Any template that extends `account/auth_base.html` inherits an inline script
that, on page load, calls `MojoAuth.getRefreshToken()` and — if a refresh
token is present in `localStorage` — transparently calls
`MojoAuth.refreshToken()` and redirects the user to `success_redirect` (the
portal). This is login-specific behavior that was written for `login.html`
but leaks to every sibling page that extends the same base.

Two concrete breakages today:

- **`/register`** — a previously-authenticated visitor (or anyone with a
  cached `mojo_refresh_token`) cannot reach the registration page to create a
  second account. The page flashes "Checking session…" and redirects to the
  portal.
- **`/contact`** — a previously-authenticated visitor cannot submit a
  contact/support message through the bouncer-gated page. The page flashes
  "Checking session…" and redirects to the portal, invisibly cancelling the
  contact flow.

The form body is still present in the DOM, but the user is bounced to the
success redirect before they can interact with it.

## Context

The session-auto-redirect is essential on `/auth` — "you're already logged in,
go straight to the app" is the desired behavior there. It is wrong everywhere
else. The regression surfaced with the addition of the `/contact` page
(commit a740caf), but the `/register` page has always been affected — it was
presumably masked because nobody routinely hits `/register` while already
holding a valid refresh token, whereas `/contact` is expected to be reached
by logged-in marketing/product visitors.

Affected users:
- Authenticated users with a valid refresh token cookie/localStorage.
- Users with a stale-but-present refresh token (the catch branch logs them
  out silently — bad UX but not the primary bug).

Not affected:
- Logged-out visitors with no token.
- Users arriving via OAuth callback (`?code=&state=`) or magic link
  (`?token=ml:`) — those blocks take precedence.

## Acceptance Criteria

- Visiting `/contact` (any `kind`) with a valid refresh token in
  `localStorage` renders the contact form and does NOT auto-redirect.
- Visiting `/register` with a valid refresh token renders the registration
  form and does NOT auto-redirect. A user wanting to register a second
  account must be able to reach the form.
- Visiting `/auth` with a valid refresh token continues to auto-redirect to
  `success_redirect` (existing behavior preserved).
- The "Checking session…" overlay does not flash on `/register` or `/contact`.
- OAuth callback (`?code=&state=`) and magic link (`?token=ml:…`) handling
  continue to work on the login page; they are not relevant to register or
  contact and should not leak either.
- Solution is driven from the backend template context (not by duplicating
  templates), so future new pages that extend `auth_base.html` don't
  accidentally re-inherit the bug.

## Investigation

**Likely root cause**: The inline script in
[mojo/apps/account/templates/account/auth_base.html:127-142](mojo/apps/account/templates/account/auth_base.html:127)
unconditionally runs the "has refresh token → refresh and redirect" block for
every page that extends the base. There is no template context flag to opt a
page out.

**Confidence**: confirmed via code analysis.

**Code path**:

1. `mojo/apps/account/rest/bouncer/views.py:380-388` — `_serve_contact()`
   renders `account/contact.html` with the full `_auth_context` (same context
   shape as login/register).
2. `mojo/apps/account/templates/account/contact.html:1` — extends
   `account/auth_base.html`.
3. `mojo/apps/account/templates/account/auth_base.html:127-142` — inline
   script runs on every page load:
   ```js
   if (!params.get("token") && !(params.get("code") && params.get("state"))) {
       var refreshToken = MojoAuth.getRefreshToken();
       if (refreshToken) {
           window._mat.showOverlay("Checking session…", "Verifying your credentials");
           MojoAuth.refreshToken()
               .then(function () {
                   window._mat.showOverlay("Signed in!", "Taking you there now…");
                   setTimeout(window._mat.redirect, 800);
               })
               .catch(function () { MojoAuth.logout(); window._mat.hideOverlay(); });
       }
   }
   ```
4. `redirectTo` at line 65 resolves to
   `params.get("redirect") || params.get("next") || params.get("returnTo") || ON_SUCCESS`
   — i.e. the portal URL by default.
5. `setTimeout(window._mat.redirect, 800)` fires `window.location.href = redirectTo`.

Same flow for `/register` via
[mojo/apps/account/templates/account/register.html:1](mojo/apps/account/templates/account/register.html:1).

**Regression test**: not feasible from the backend test suite — this is a
client-side JavaScript behavior driven by `localStorage` state. A browser /
Playwright test would be the right fit but is out of scope for the existing
testit harness. Manual verification plan: log in through `/auth`, then
navigate to `/contact` (should render form, not redirect); repeat for
`/register`; also verify `/auth` itself still auto-redirects.

**Related files**:
- `mojo/apps/account/templates/account/auth_base.html` — the inline session-check block that needs to become conditional.
- `mojo/apps/account/templates/account/contact.html` — extends auth_base.
- `mojo/apps/account/templates/account/register.html` — extends auth_base; currently broken too.
- `mojo/apps/account/templates/account/login.html` — extends auth_base; must retain the auto-redirect.
- `mojo/apps/account/rest/bouncer/views.py` — `_auth_context()` and the three `_serve_*` helpers. Context flag likely added here.
- `mojo/apps/account/static/account/mojo-auth.js` — `MojoAuth.getRefreshToken()` / `MojoAuth.refreshToken()` definitions (for reference only, no change expected).

**Suggested fix shape** (not implementing here — design session should confirm):
Add a `page_mode` or `auto_session_check` context flag to `_auth_context`.
Wrap the session-check block in `{% if auto_session_check %}...{% endif %}`.
`_serve_login` sets it true; `_serve_login(page_mode='register')` and
`_serve_contact` leave it false (default). Also consider whether the
`Checking session…` overlay markup should only render when the flag is on —
so there's no flash / FOUC on the other pages.
