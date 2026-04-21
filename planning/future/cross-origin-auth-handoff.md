# Cross-Origin Auth Token Handoff

**Type**: request
**Status**: open
**Date**: 2026-04-13
**Priority**: low
**Deferred**: 2026-04-19 — not needed right now; revisit when a deployment actually hits the cross-origin loop in production.

## Description

When the auth server and the consuming app are on different origins (e.g. auth at `localhost:9009`, app at `127.0.0.1:8023`), the JWT stored in `localStorage` after login is inaccessible to the app domain. This causes an infinite redirect loop: app detects no JWT → redirects to auth → user logs in → JWT stored on auth origin → redirect back to app → app still has no JWT → loop.

Need a token handoff mechanism so the auth server can securely pass credentials to a cross-origin app during the post-login redirect. This is the standard "authorization code" pattern: auth issues a short-lived, single-use exchange code, appends it to the redirect URL, and the app exchanges it for a JWT via API call.

## Context

This is triggered by the bouncer redirect flow (`?redirect=<url>`) when the redirect target is a different origin. Same-origin deployments (auth and app on the same domain) work fine because `localStorage` is shared. Multi-service deployments — where a central auth domain serves multiple apps on different domains — are broken.

The codebase already has multiple patterns for short-lived, single-use, Redis-backed tokens: MFA tokens (`mojo/apps/account/services/mfa.py`), OAuth state tokens (`mojo/apps/account/services/oauth/base.py`), and magic login tokens (`mojo/apps/account/utils/tokens.py`). The handoff mechanism can follow the same pattern.

## Acceptance Criteria

- After login on the auth server, if `?redirect=` points to a different origin, the redirect URL includes a short-lived exchange code (not the JWT itself)
- The app calls a public API endpoint to exchange the code for access + refresh tokens
- Exchange codes are single-use (consumed on first exchange) and expire after a short TTL (e.g. 30–60 seconds)
- Same-origin redirects continue to work as they do today (localStorage, no code needed)
- The exchange endpoint is rate-limited to prevent brute-force
- mojo-auth.js gains a helper to detect and consume the exchange code from the URL on page load
- Works with both bouncer-gated and non-gated auth pages

## Investigation

**What exists**:
- `mojo/apps/account/services/mfa.py` — Redis-backed single-use token pattern (`create_mfa_token` / `consume_mfa_token`), 5-minute TTL. Almost identical to what's needed.
- `mojo/apps/account/services/oauth/base.py` — OAuth state token with `create_state` / `consume_state`, 10-minute TTL, stores arbitrary payload in Redis.
- `mojo/apps/account/utils/tokens.py` — Magic login tokens, signed with user key, single-use with JTI tracking. More complex than needed here.
- `mojo/apps/account/rest/user.py` — `jwt_login()` helper that creates JWT package and returns it. The exchange endpoint would reuse this.
- `mojo/apps/account/static/account/mojo-auth.js` — Client-side auth library. Already handles OAuth `?code=` + `?state=` auto-completion. The exchange code flow would be similar.
- `mojo/apps/account/templates/account/auth_base.html` — Post-login redirect logic (`window._mat.redirect()`). Needs to append exchange code for cross-origin redirects.
- `mojo/middleware/cors.py` — CORS allows all origins with `Authorization` header. Exchange endpoint will work cross-origin.

**What changes**:
- New service: `mojo/apps/account/services/auth_handoff.py` — `create_handoff_code(user)` / `consume_handoff_code(code)` following the MFA token pattern
- New REST endpoint: `POST /api/account/auth/exchange` — accepts `code`, returns JWT package
- Modify `auth_base.html` — `_mat.redirect()` detects cross-origin redirect, generates handoff code via API before redirecting
- Modify `mojo-auth.js` — add `MojoAuth.exchangeCode(code)` helper and auto-detection on page load
- New setting: `AUTH_HANDOFF_CODE_TTL` — TTL for exchange codes (default 60 seconds)

**Constraints**:
- Exchange codes must not contain the JWT or any user secret — they are opaque references to a Redis entry
- The exchange endpoint must be public (the app server may not have auth headers yet) but rate-limited
- Codes must be single-use to prevent replay
- The JWT in the redirect URL approach (token in fragment/query) was considered and rejected: tokens in URLs leak via Referer headers, browser history, and server logs
- Need to handle the case where the user has MFA enabled — the handoff code should only be generated after full authentication (including MFA)

**Related files**:
- `mojo/apps/account/services/mfa.py` — pattern to follow
- `mojo/apps/account/services/oauth/base.py` — pattern to follow
- `mojo/apps/account/rest/user.py` — `jwt_login()` to reuse
- `mojo/apps/account/static/account/mojo-auth.js` — client-side changes
- `mojo/apps/account/templates/account/auth_base.html` — redirect logic
- `mojo/apps/account/rest/bouncer/views.py` — bouncer redirect flow

## Endpoints

| Method | Path | Description | Permission |
|---|---|---|---|
| POST | `account/auth/exchange` | Exchange a handoff code for JWT access + refresh tokens | Public (rate-limited) |

## Settings

| Setting | Default | Purpose |
|---|---|---|
| `AUTH_HANDOFF_CODE_TTL` | `60` | Seconds before an exchange code expires |

## Tests Required

- Exchange code created after login, redeemable for JWT
- Exchange code is single-use (second attempt fails)
- Expired code is rejected
- Invalid/random code is rejected
- Rate limiting on exchange endpoint
- Same-origin redirect skips handoff (no code appended)
- Cross-origin redirect includes handoff code
- Full round-trip: login → redirect with code → exchange → JWT returned
- MFA flow: code only generated after MFA completion

## Out of Scope

- Shared cookie domain approach (requires same parent domain, not always possible)
- Token in URL fragment (security concerns)
- SSO / SAML / OpenID Connect protocols (separate feature)
- Allowlisting redirect origins (decided to allow any redirect URL)
