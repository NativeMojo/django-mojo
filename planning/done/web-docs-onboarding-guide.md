# Web Docs — User Onboarding & Gated Login Guide

**Type**: request
**Status**: done
**Date**: 2026-04-11
**Priority**: medium

## Description

The web developer docs have no single guide for user onboarding (registration → email verification → first login). The individual pieces exist across 4+ files but a web dev building a custom flow has to discover and stitch them together. Additionally, the registration endpoint (`POST /api/auth/register`) is undocumented in the web_developer track entirely.

## What's Missing

### 1. Registration endpoint not documented

`POST /api/auth/register` exists in code (`mojo/apps/account/rest/user.py:88`) but is absent from both `authentication.md` and `user.md` in the web_developer docs. A web dev has no way to discover:

- The endpoint path
- Required fields (`email`, `password`) and optional fields (`first_name`, `last_name`)
- That `ALLOW_USER_REGISTRATION` must be `True` (defaults to `False`)
- That `bouncer_token` with `page_type='registration'` is required when bouncer is active
- Rate limiting: 5 requests per IP per 5 minutes

### 2. Verification gate response after registration

When `REQUIRE_VERIFIED_EMAIL=True`, registration returns `{status: true, requires_verification: true}` with no JWT. The web dev needs to know:

- No JWT is issued — user cannot call authenticated endpoints yet
- Show "check your email" UI, not "you're logged in"
- After clicking the email link, user must go through normal login flow
- When `REQUIRE_VERIFIED_EMAIL=False`, registration auto-logs in (JWT returned)

### 3. No end-to-end onboarding flow doc

There's no guide connecting: bouncer challenge → register → verify email → first login. The steps span `bouncer.md`, `auth_pages.md`, `authentication.md`, `user.md`, and `email_verification.md`.

### 4. Discoverability in README.md

The `account/README.md` index lists docs by feature but has no "getting started" or "common flows" section. A web dev looking for "how do I add user registration" has to guess which of the 20+ files to read.

## Proposed Changes

### A. Add registration to `authentication.md`

After the Login section, add a Registration section documenting `POST /api/auth/register` with:

- Request/response for both verified and non-verified modes
- `bouncer_token` + `duid` fields
- Error responses (registration disabled, email taken, weak password)
- Rate limit note

### B. Add a "Common Flows" section to `account/README.md`

At the top, before the individual file index, add a short section like:

```markdown
## Common Flows

### User Registration & Onboarding
1. [Auth Pages](auth_pages.md) — bouncer-gated `/register` page (or build your own)
2. [Authentication § Registration](authentication.md#registration) — `POST /api/auth/register`
3. [Email Verification](email_verification.md) — verification gate, send/confirm flow
4. [Authentication § Login](authentication.md#login) — first login after verification

### Securing the Login Flow
1. [Bouncer](bouncer.md) — bot detection gate, challenge page, token lifecycle
2. [Authentication](authentication.md) — login, MFA, token refresh
3. [Passkeys](passkeys.md) / [Magic Login](magic_login.md) — passwordless alternatives
```

### C. Add a "User Onboarding" entry to the top-level `README.md`

Under the account row in the API Reference table, or as a new "Common Flows" section, add a pointer so web devs searching the top-level index find it.

## Out of Scope

- Django developer docs for bouncer — already complete
- Bouncer admin APIs — already documented in `bouncer.md`
- Changes to the registration endpoint code itself — just docs

## Files to Change

- `docs/web_developer/account/authentication.md` — add Registration section
- `docs/web_developer/account/README.md` — add Common Flows section
- `docs/web_developer/README.md` — add onboarding pointer
