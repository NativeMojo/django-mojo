# Apple OAuth: Invalid web redirect url

**Type**: bug
**Status**: Open
**Date**: 2026-03-21

## Error (original)

```
invalid_request
Invalid web redirect url.

https://appleid.apple.com/auth/authorize?client_id=com.mojoverify.app.login
  &redirect_uri=https%3A%2F%2Fmojoverify.com%2Fauth%2F
  &response_type=code&response_mode=form_post&scope=openid%20email
  &state=fd03931749804c18be25d1772ea45e95
```

## Error (current — after partial fix)

```
invalid_request
Invalid web redirect url.

https://appleid.apple.com/auth/authorize?client_id=com.mojoverify.app.login
  &redirect_uri=https%3A%2F%2Fmojoverify.com%2Fapi%2Fauth%2Foauth%2Fapple%2Fcallback
  &response_type=code&response_mode=form_post&scope=openid%20email
  &state=7ef6959780d54cd7a278cf2bc3e1f414
```

The backend callback URL is now correctly sent to Apple. Apple is still rejecting it because
`https://mojoverify.com/api/auth/oauth/apple/callback` is not registered as a Return URL
in Apple Developer Portal for service ID `com.mojoverify.app.login`.

## What Has Been Done

- `POST /api/auth/oauth/apple/callback` endpoint added to `rest/oauth.py` — receives Apple's
  form POST and bounces the browser to the frontend with `code` and `state` as query params.
- `on_oauth_begin` now derives the backend callback URL from request origin and passes it
  as `redirect_uri` to Apple, rather than using the frontend SPA URL.
- `redirect_uri` stored in OAuth state so `on_oauth_complete` uses the right URI for token exchange.

## Remaining Items

### 1. Register the callback URL in Apple Developer Portal (config — unblocks immediately)

Add the following Return URL to the `com.mojoverify.app.login` Service ID:

```
https://mojoverify.com/api/auth/oauth/apple/callback
```

Apple Developer Portal → Certificates, Identifiers & Profiles →
Identifiers → [Service ID] → Sign In with Apple → Return URLs

### 2. Add `APPLE_REDIRECT_URI` setting support (code)

The current code derives the callback URL from request origin at runtime. If origin detection
doesn't match the registered URL (proxy, staging vs prod, etc.) there is no override.

In `rest/oauth.py` `on_oauth_begin`, honour an explicit setting:

```python
apple_redirect_uri = (
    settings.get("APPLE_REDIRECT_URI", "")
    or f"{origin}/api/auth/oauth/apple/callback"
)
```

This lets operators pin the exact URL registered in Apple Developer Portal.

## Required Settings (after code change)

```python
# Optional — pin the backend callback URL registered in Apple Developer Portal.
# Falls back to {origin}/api/auth/oauth/apple/callback if not set.
APPLE_REDIRECT_URI = "https://mojoverify.com/api/auth/oauth/apple/callback"
```

## Files to Change

- `mojo/apps/account/rest/oauth.py` — honour `APPLE_REDIRECT_URI` in `on_oauth_begin`
- `mojo/apps/account/services/oauth/apple.py` — document setting in module docstring
- `docs/web_developer/account/oauth.md` — document `APPLE_REDIRECT_URI` setting
