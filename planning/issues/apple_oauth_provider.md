# Add Apple Sign In OAuth provider

**Type**: feature
**Status**: Resolved — 2026-03-16
**Date**: 2026-03-16

## Goal

Add `AppleOAuthProvider` alongside `GoogleOAuthProvider` so that `GET /api/auth/oauth/apple/begin` and `POST /api/auth/oauth/apple/complete` work identically to the Google flow.

## Background

Apple Sign In uses standard OAuth2 authorization code flow from the frontend's perspective, but differs internally:

1. **client_secret is a short-lived JWT** — generated per-request, signed ES256 with the Apple `.p8` private key. Not a static secret.
2. **No userinfo endpoint** — profile (`sub`, `email`) is decoded from the `id_token` JWT returned by Apple's token endpoint.
3. **Name only on first login** — Apple sends the user's name in the redirect once. After that, not available. We don't rely on it (display_name is optional).

Frontend flow is identical to Google: frontend catches redirect → POSTs `code` + `state` to `/complete`.

## Required Settings

```python
APPLE_CLIENT_ID    # Service ID registered in Apple Developer portal (e.g. com.example.web)
APPLE_TEAM_ID      # 10-character Apple Developer Team ID
APPLE_KEY_ID       # Key ID from the .p8 file
APPLE_PRIVATE_KEY  # Full PEM string of the .p8 private key
```

## Scope

### Files to create
- `mojo/apps/account/services/oauth/apple.py` — `AppleOAuthProvider`

### Files to modify
- `mojo/apps/account/services/oauth/__init__.py` — register `"apple": AppleOAuthProvider`

### Explicitly out of scope
- `rest/oauth.py` — no changes needed
- Native iOS Sign In (different flow, not covered here)
- Storing the user's real name from first-login `user` field

## Implementation Steps

1. Create `apple.py` with `AppleOAuthProvider(OAuthProvider)`:
   - `get_auth_url(state, redirect_uri)` — build Apple auth URL (`https://appleid.apple.com/auth/authorize`) with `response_type=code`, `response_mode=form_post` scope `openid email`
   - `_build_client_secret()` — generate ES256 JWT signed with `APPLE_PRIVATE_KEY`, claims: `iss=TEAM_ID`, `sub=CLIENT_ID`, `aud=https://appleid.apple.com`, exp=5min
   - `exchange_code(code, redirect_uri)` — POST to `https://appleid.apple.com/auth/token` with generated client_secret
   - `get_profile(tokens)` — decode `id_token` from response (no sig verification needed — received directly from Apple over HTTPS), return `uid=sub`, `email`

2. Register in `__init__.py`

## Edge Cases

- `APPLE_PRIVATE_KEY` not set → raise clear `ValueError` at startup of `_build_client_secret`
- Apple relay email (`@privaterelay.appleid.com`) → store as-is, same as any other email
- `id_token` missing from response → raise `ValueError` in `get_profile`
- `email` absent from `id_token` (can happen if user hides email but relay is off) → raise `ValueError`

## Tests

- `tests/test_accounts/oauth_apple.py`:
  - `test_auth_url_contains_required_params` — assert URL has `client_id`, `state`, `redirect_uri`, `response_type=code`
  - `test_client_secret_is_valid_jwt` — assert generated JWT has correct claims and is ES256
  - `test_get_profile_decodes_id_token` — mock token response with a real `id_token`, assert `uid` and `email` extracted correctly
  - `test_get_profile_missing_id_token_raises` — assert `ValueError`

## Docs

- `docs/django_developer/account/oauth.md` — add Apple to provider list, document 4 required settings
- `docs/web_developer/account/oauth.md` — add Apple to supported providers table

## Dependencies

- `PyJWT` + `cryptography` — already installed (`jwtoken.py` uses both)
