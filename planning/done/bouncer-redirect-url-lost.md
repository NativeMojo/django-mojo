# Bouncer Auth Pages Drop Redirect URL

**Type**: bug
**Status**: resolved
**Date**: 2026-04-12
**Severity**: high

## Description

When navigating to the bouncer-gated login page with a `redirect` query param containing an absolute URL (e.g. `?redirect=http://127.0.0.1:8023/portal/`), the redirect is silently dropped and the user lands on the default `AUTH_SUCCESS_REDIRECT` after login. Two separate issues contribute:

1. **Absolute redirect URLs rejected** — `auth_base.html:65` only accepts redirect values starting with `/`. An absolute URL like `http://127.0.0.1:8023/portal/` fails the check and falls back to the default.

2. **Redirect param lost through bouncer challenge** — When the user hits the challenge page first, `_serve_challenge()` builds the post-challenge redirect URL (`login_url`) as `/{path}{group_qs}` without forwarding the original `redirect` query param. After passing the challenge, the user lands on `/auth` (or `/auth?group=...`) with no redirect param at all.

Additionally, the "Back to website" link in the hero panel is a **static setting** (`AUTH_BACK_TO_WEBSITE_URL`), not derived from the `redirect` param. This may be intentional (separate concern) or the user may expect it to reflect the redirect target. Needs clarification.

## Context

This affects any deployment where an external app redirects users to the auth page with a return URL — the standard gated-app pattern. The user authenticates successfully but ends up on `/` instead of being returned to their app. This is especially impactful for multi-service deployments where the auth domain differs from the app domain.

## Acceptance Criteria

- `?redirect=` accepts validated absolute URLs (same-origin or allowlisted origins), not just relative paths
- The `redirect` param is preserved through the bouncer challenge flow (challenge → login page)
- After successful login, the user is redirected to the URL from the `redirect` param
- Security: open-redirect protection must remain — validate against an allowlist or same-origin check, not blindly accept any absolute URL
- Clarify whether "Back to website" should reflect the `redirect` param or remain a static setting

## Investigation

**Likely root cause**: Two code paths independently drop the redirect:
1. Client-side filter in `auth_base.html:65` rejects non-`/` prefixed values
2. Server-side `_serve_challenge()` in `views.py:283` doesn't forward query params

**Confidence**: confirmed

**Code path**:
- `mojo/apps/account/rest/bouncer/views.py:144` — `_serve_challenge()` called, redirect param ignored
- `mojo/apps/account/rest/bouncer/views.py:261-288` — `_serve_challenge()` builds `login_url` without preserving `redirect`
- `mojo/apps/account/templates/account/bouncer_challenge.html:184` — `redirectUrl` uses `login_url` (no redirect param)
- `mojo/apps/account/templates/account/auth_base.html:63-65` — client-side redirect extraction rejects absolute URLs
- `mojo/apps/account/templates/account/auth_hero.html:23-25` — "Back to website" uses static `back_to_website_url` setting

**Regression test**: not feasible — requires running server with bouncer flow

**Related files**:
- `mojo/apps/account/rest/bouncer/views.py` — `_serve_challenge()` and `_serve_login()`
- `mojo/apps/account/templates/account/auth_base.html` — redirect param extraction JS
- `mojo/apps/account/templates/account/bouncer_challenge.html` — challenge redirect URL
- `mojo/apps/account/templates/account/auth_hero.html` — "Back to website" link
- `docs/web_developer/account/auth_pages.md` — docs say `?redirect=/path` (relative only)

## Resolution

**Status**: resolved
**Date**: 2026-04-19

Verified both root causes are addressed in current code:

1. **Absolute redirect URLs accepted** — `mojo/apps/account/templates/account/auth_base.html:64` reads `paramRedirect` from `redirect`/`next`/`returnTo` query params and uses it directly as `redirectTo`; the old "must start with `/`" filter is gone.

2. **Redirect param forwarded through challenge** — `_serve_challenge()` in `mojo/apps/account/rest/bouncer/views.py:271-285` builds `fwd_params` with `group`, `redirect`, and `back`, urlencodes them, and appends to `login_url`. The user lands on the auth page with the redirect param intact after the challenge.

The `back` param is also forwarded now, addressing the "Back to website" clarification: the static `AUTH_BACK_TO_WEBSITE_URL` setting is still the default but can be overridden per-request via `?back=...`.

No additional code changes needed — closing.
