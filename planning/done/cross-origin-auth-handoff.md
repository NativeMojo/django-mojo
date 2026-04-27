# Cross-Origin Auth Token Handoff

**Type**: request
**Status**: resolved
**Date**: 2026-04-13
**Priority**: low
**Deferred**: 2026-04-19 — not needed right now; revisit when a deployment actually hits the cross-origin loop in production.
**Resolved**: 2026-04-27

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

## Plan

**Status**: planned
**Planned**: 2026-04-27

### Objective
Authorization-code style token handoff so a fully authenticated user on the auth origin can hand a JWT to a different-origin app at the end of the bouncer redirect flow, without putting the JWT in the URL.

### Steps

1. `mojo/apps/account/services/auth_handoff.py` *(new)* — Redis-backed `create_handoff_code(user, ip=None)` / `consume_handoff_code(code)`. Mirror `services/mfa.py`. Key prefix `auth:handoff:`. TTL from `AUTH_HANDOFF_CODE_TTL` (default 60). Stored value `{"uid", "ip"}`. Single-use via `GET`+`DELETE`.
2. `mojo/apps/account/rest/user.py` — add two endpoints:
   - `@md.POST("auth/handoff")` `@md.requires_auth()` `@md.rate_limit("auth_handoff", ip_limit=30)` — returns `{status, data: {code, expires_in}}` for `request.user`.
   - `@md.POST("auth/exchange")` `@md.public_endpoint()` `@md.strict_rate_limit("auth_exchange", ip_limit=20, ip_window=60)` `@md.requires_params("code")` — consumes the code, loads user, raises 403 on inactive user, returns `jwt_login(request, user, source="handoff")`.
3. `mojo/apps/account/static/account/mojo-auth.js`:
   - Add `handoff: '/api/account/auth/handoff'` and `exchange: '/api/account/auth/exchange'` to `DEFAULT_ENDPOINTS`.
   - Add `MojoAuth.requestHandoffCode()` — POSTs `auth/handoff` with `Authorization: Bearer <access>`; returns `{code, expires_in}`.
   - Add `MojoAuth.exchangeAuthCode(code)` — POSTs `auth/exchange`, calls `saveTokens` on success.
   - Add `MojoAuth.handleAuthCodeFromURL()` — reads `?auth_code=` from `location.search`, exchanges it, scrubs the param via `history.replaceState`, returns the auth payload (or `null` if no param). Mirrors `handleMagicTokenFromURL`.
4. `mojo/apps/account/templates/account/auth_base.html` — replace `_mat.redirect` with an async-aware version: parse `redirectTo`; if `new URL(redirectTo).origin === location.origin`, redirect as today; otherwise call `MojoAuth.requestHandoffCode()`, append `auth_code=<code>` (preserving existing query string) to the redirect URL, then navigate. On API failure, log and redirect anyway (no worse than today's loop).
5. `docs/django_developer/account/auth.md` + `docs/web_developer/account/auth_pages.md` + `docs/web_developer/account/authentication.md` — document the handoff service, endpoints, the `AUTH_HANDOFF_CODE_TTL` setting, the `auth_code` URL param, and the redirect-allowlist trade-off.
6. `CHANGELOG.md` — unreleased entry for the cross-origin auth handoff.

### Design Decisions

- **Two-step API (`auth/handoff` + `auth/exchange`)** instead of returning the code from the login response — issuance is triggered only when the auth-page JS detects a cross-origin redirect, so login responses are unchanged and the auto-session-resume path (`refreshToken()` in `auth_base.html`) gets handoff for free without server-side knowledge of the redirect target.
- **URL param `auth_code`** — distinct from OAuth `code+state` (handled at `auth_base.html:161`) to avoid collisions on auth-domain pages.
- **Reuse `jwt_login()` with `source="handoff"`** — preserves last-login bump, login-event tracking, and webapp-url metadata. `_check_verification_gate` only enforces `email`/`phone_number` sources, so `"handoff"` is a no-op there.
- **Raw `uuid.uuid4().hex` code, no prefix** — opaque random string; the `auth:handoff:` Redis namespace gives isolation.
- **Single-use via Redis `GET`+`DELETE`** as in `consume_mfa_token`. Race-tolerant for the brute-force threat model; TTL+rate-limit cap exposure.
- **No IP/device binding on the code** — would break legitimate flows where auth and app origins are reached via different network paths (e.g. mobile NAT). TTL + single-use + rate-limit are sufficient.
- **No redirect-origin allowlist** per request scope. Documented as a known trade-off — a malicious `?redirect=evil.example.com` on the auth page now hands a JWT to `evil` after auto-session-resume; deployments needing tighter control can layer an allowlist later.

### Use Cases

1. Same-origin `?redirect=/dashboard` — origin matches, no handoff, current behaviour.
2. Cross-origin `?redirect=https://app.example.com/portal` after fresh password login — handoff code appended.
3. Cross-origin redirect after auto-session-resume (`refreshToken()` succeeds in `auth_base.html`) — same path, handoff code appended.
4. Cross-origin redirect after MFA completion (TOTP/SMS verify) — `_mat.onAuthSuccess` → `_mat.redirect` → handoff path; works because JWT is already in localStorage.
5. Cross-origin redirect after OAuth completion — same.
6. App page boots with `?auth_code=…` — calls `MojoAuth.handleAuthCodeFromURL()` on startup, gets JWT, scrubs URL.
7. App page boots without `?auth_code=…` and without local JWT — bounces back to auth (existing behaviour).

### Edge Cases

- **Replay**: code consumed on first `exchange` call → second call returns 401. Codes also expire at TTL.
- **Brute force**: 32-hex-char space + 60s TTL + rate-limit 20/min/IP. Acceptable.
- **Inactive user between issue and exchange**: exchange handler checks `user.is_active`; raises 403.
- **Handoff issuance fails (Redis down, network)**: client logs and falls back to the existing redirect; the app will loop as it does today.
- **App loads `mojo-auth.js` from a different origin than its API**: `MojoAuth.init({ baseURL })` must point at the auth origin so `auth/exchange` hits the right server. Documented.
- **Auto-redirect with stale refresh token**: existing flow already calls `MojoAuth.logout()` on refresh failure; handoff path is only reached on success, so no spurious code requests.

### Testing

- `tests/test_auth/handoff.py` *(new)*:
  - `test_create_and_consume` — service round-trip.
  - `test_consume_is_single_use` — second consume returns `None`.
  - `test_consume_expired` — TTL via direct Redis delete or sleep.
  - `test_consume_invalid_code` — random hex returns `None`.
  - `test_handoff_endpoint_requires_auth` — anonymous → 401.
  - `test_handoff_endpoint_returns_code` — authed user gets `{code, expires_in}`.
  - `test_exchange_endpoint_returns_jwt` — code → access+refresh tokens; tokens validate against `/api/account/user/me`.
  - `test_exchange_single_use` — second exchange → 401.
  - `test_exchange_invalid_code` → 401.
  - `test_exchange_inactive_user` — flip `is_active=False` between issue and exchange → 403.
  - `test_exchange_rate_limit` — 21st call within window → blocked.
  - `test_full_round_trip` — login → handoff → exchange → authenticated request succeeds.
- JS paths (`_mat.redirect` cross-origin detection, `handleAuthCodeFromURL`) are covered by manual cross-origin verification, not backend tests.

### Docs

- `docs/django_developer/account/auth.md` — handoff service description, `AUTH_HANDOFF_CODE_TTL` setting, redirect-allowlist trade-off note.
- `docs/web_developer/account/auth_pages.md` — new "Cross-Origin Redirect Handoff" section: behaviour of `_mat.redirect` and the `?auth_code=` param.
- `docs/web_developer/account/authentication.md` — add `POST /api/account/auth/handoff` and `POST /api/account/auth/exchange` to the endpoint reference, plus `MojoAuth.handleAuthCodeFromURL()` example for app bootstrap.
- `CHANGELOG.md` — unreleased entry.

## Resolution

**Status**: resolved
**Date**: 2026-04-27
**Commits**: cd83949 (feature) + d538e2c (security hardening)

### What Was Built

Authorization-code-style handoff: the auth-origin page mints a single-use code via `POST /api/auth/handoff`, appends `?auth_code=<code>` to a cross-origin redirect URL, and the consuming app calls `POST /api/auth/exchange` to swap the code for an access + refresh token pair. Same-origin redirects unchanged.

### Files Changed

- `mojo/apps/account/services/auth_handoff.py` *(new)* — Redis-backed service. Atomic `GETDEL` for single-use, 32-hex-alphanumeric code validation, TTL from `AUTH_HANDOFF_CODE_TTL` (default 60).
- `mojo/apps/account/rest/user.py` — added `on_auth_handoff` (auth'd, `rate_limit("auth_handoff", ip_limit=30)`) and `on_auth_exchange` (`public_endpoint`, `strict_rate_limit("auth_exchange", ip_limit=20, ip_window=60)`). Exchange handler raises 401 on invalid/missing code, 403 on inactive user, otherwise calls `jwt_login(request, user, source="handoff")`.
- `mojo/apps/account/static/account/mojo-auth.js` — added `requestHandoffCode`, `exchangeAuthCode`, `handleAuthCodeFromURL`. `DEFAULT_ENDPOINTS` extended with `handoff` + `exchange`.
- `mojo/apps/account/templates/account/auth_base.html` — `_mat.redirect` now async-aware: same-origin → direct nav (unchanged), cross-origin + authenticated → `requestHandoffCode` + `?auth_code=` append, fallback to plain redirect on issuance failure. The two `setTimeout(_mat.redirect, …)` call sites were wrapped in lambdas so the async path runs.
- `tests/test_auth/handoff.py` *(new)* — 12 tests covering service round-trip, single-use, expiry/invalidation, endpoint auth, JWT round-trip, inactive-user rejection, and rate-limit trip.
- `docs/django_developer/account/auth.md`, `docs/web_developer/account/auth_pages.md`, `docs/web_developer/account/authentication.md` — handoff sections, security trade-off, bootstrap snippet.
- `CHANGELOG.md` — entry under "Unreleased (post v1.1.34)".

### Tests

- `tests/test_auth/handoff.py` — service-layer + endpoint coverage.
- Run: `bin/run_tests --agent -t test_auth.handoff` → 12/12 pass.
- Full suite: `bin/run_tests --agent` → 1905 pass / 1 fail / 56 skip. The single failure is a pre-existing race in `test_incident.rule_engine_comprehensive::bundling_time_window` (FK to a deleted RuleSet) and is unrelated to this work.

### Docs Updated

- `docs/django_developer/account/auth.md` — new "Cross-Origin Auth Handoff" section, `AUTH_HANDOFF_CODE_TTL` setting table, security note.
- `docs/web_developer/account/auth_pages.md` — new "Cross-Origin Redirect Handoff" section with bootstrap snippet.
- `docs/web_developer/account/authentication.md` — endpoint reference for both `/api/auth/handoff` (30/IP rate limit) and `/api/auth/exchange` (20/min/IP single-use, public).
- `CHANGELOG.md` — unreleased entry.

### Security Review

Findings actioned in commit d538e2c:

- **HIGH — Race on GET+DELETE consume path.** Replaced with atomic `GETDEL`. Two concurrent calls can no longer both win.
- **LOW — Unbounded `code` parameter could trigger oversized Redis key reads.** Added 32-char alphanumeric validation in `consume_handoff_code` before the lookup.

Findings acknowledged but not actioned (consistent with the planned design):

- **MEDIUM — `ip_limit=30` per-IP cap on issuance with no per-user cap.** Per-IP limit was the chosen trade-off; per-user caps would break legitimate multi-tab flows. Documented limitation.
- **MEDIUM — `auth_code` is briefly visible in `window.location.search` before `replaceState` runs.** Matches the existing `handleMagicTokenFromURL` pattern; same trade-off applies. With a 60s TTL and single-use enforcement, blast radius is bounded.
- **LOW — Silent fallback to plain redirect on issuance failure.** Documented behaviour — best-effort handoff is preferable to a hard error that strands the user.

The intentional non-allowlist on redirect destinations remains an acknowledged trade-off, documented in `docs/django_developer/account/auth.md` and the changelog.

### Follow-up

- Consider adding `ALLOWED_REDIRECT_URLS` (or per-group `allowed_redirect_urls`) enforcement on the bouncer redirect param if a deployment surfaces a need. The OAuth allowlist helper at `mojo/apps/account/rest/oauth.py:48` is a ready template.
- Pre-existing `test_incident.rule_engine_comprehensive::bundling_time_window` flake exposed by the full-suite run — unrelated to this work but worth opening separately.
