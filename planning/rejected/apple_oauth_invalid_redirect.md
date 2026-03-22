# Apple OAuth: Invalid web redirect url

**Type**: bug
**Status**: Open
**Date**: 2026-03-21

## Error

```
invalid_request
Invalid web redirect url.

https://appleid.apple.com/auth/authorize?client_id=com.mojoverify.app.login
  &redirect_uri=https%3A%2F%2Fmojoverify.com%2Fauth%2F
  &response_type=code&response_mode=form_post&scope=openid%20email
  &state=fd03931749804c18be25d1772ea45e95
```

## Root Cause

The URL encoding is now correct (`%3A%2F%2F`, `%20`). Apple is still rejecting the request.

The redirect_uri `https://mojoverify.com/auth/` is a **frontend SPA URL**. Apple's
`response_mode=form_post` requires the redirect_uri to be a backend server endpoint —
Apple POSTs `code`, `state`, and optionally `user` directly to it. Two problems:

1. **Apple's developer portal will only accept registered Return URLs.** The registered
   URL is likely a backend endpoint, not the SPA URL. Apple rejects at the auth URL
   stage before any redirect occurs.

2. **Even if the SPA URL were registered**, JavaScript running in the browser has no
   access to the POST body of the current page request — the SPA cannot read `code` or
   `state` from Apple's form POST.

## Required Architecture

Apple's form_post callback must land on a **backend endpoint**:

```
Apple → POST /api/auth/oauth/apple/callback  (backend receives code, state, user)
      ↓
Backend redirects browser → {frontend_url}?code=...&state=...
      ↓
Frontend reads query params, calls POST /api/auth/oauth/apple/complete  (existing)
```

## Fix Plan

1. Add `POST /api/auth/oauth/apple/callback` endpoint in `rest/oauth.py`:
   - Reads `code`, `state`, (optionally `user`) from Apple's form POST body
   - Redirects browser to a configured frontend URL with `code` and `state` as query params
   - Uses `APPLE_CALLBACK_REDIRECT` setting for the frontend destination URL

2. Add `APPLE_REDIRECT_URI` setting support in `AppleOAuthProvider.get_auth_url()`:
   - Apple's redirect_uri must always be the backend callback URL, not the generic
     `OAUTH_REDIRECT_URI` which is a frontend URL
   - Fall back to `{origin}/api/auth/oauth/apple/callback` if not set

3. Store `redirect_uri` in state (already done) so `on_oauth_complete` uses the right
   URI when exchanging the code with Apple's token endpoint.

## Required Settings

```python
APPLE_REDIRECT_URI        # Backend callback URL: https://mojoverify.com/api/auth/oauth/apple/callback
APPLE_CALLBACK_REDIRECT   # Frontend destination after callback: https://mojoverify.com/auth/
```

## Files to Change

- `mojo/apps/account/rest/oauth.py` — add `POST auth/oauth/apple/callback` endpoint
- `mojo/apps/account/services/oauth/apple.py` — use `APPLE_REDIRECT_URI` setting
