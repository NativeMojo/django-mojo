# OAuth / Social Login — Django Developer Reference

## Overview

OAuth2 social login is built into the framework. The full flow — CSRF state management, provider token exchange, user resolution, and JWT issuance — is handled by the framework. Your project only needs to configure credentials and (optionally) register additional providers.

**Current providers:** `google`, `apple`, `github`

All three are toggleable auth-config methods (`LOGIN_METHODS` /
`REGISTRATION_METHODS` in `services/auth_config.py`): enabled by default,
disable per group via `login.methods` / `registration.methods`, and the hosted
auth pages render a button for each enabled provider — see
[Auth Pages](auth_pages.md).

---

## Architecture

```
GET  /api/auth/oauth/<provider>/begin    ->  OAuthProvider.create_state()
                                             OAuthProvider.get_auth_url()

POST /api/auth/oauth/<provider>/complete ->  OAuthProvider.consume_state()    (CSRF check)
                                             OAuthProvider.exchange_code()    (token exchange)
                                             OAuthProvider.get_profile()      (uid + email)
                                             _find_or_create_user()           (auto-link)
                                             jwt_login(request, user)         (issue JWT)
```

Key files:

| File | Purpose |
|---|---|
| `mojo/apps/account/rest/oauth.py` | REST endpoints + auto-link logic |
| `mojo/apps/account/models/oauth.py` | `OAuthConnection` model |
| `mojo/apps/account/services/oauth/base.py` | `OAuthProvider` base class |
| `mojo/apps/account/services/oauth/google.py` | Google implementation |
| `mojo/apps/account/services/oauth/apple.py` | Apple implementation |
| `mojo/apps/account/services/oauth/github.py` | GitHub implementation |
| `mojo/apps/account/services/oauth/__init__.py` | Provider registry |

---

## Required Settings

```python
# settings.py

GOOGLE_CLIENT_ID     = "your-client-id.apps.googleusercontent.com"
GOOGLE_CLIENT_SECRET = "your-client-secret"

# The URL Google redirects back to after login.
# Must match an authorised redirect URI in Google Cloud Console.
OAUTH_REDIRECT_URI = "https://your-app.example.com/api/oauth/google/complete"
```

### Apple Settings

```python
APPLE_CLIENT_ID    = "com.example.web"           # Service ID from Apple Developer portal
APPLE_TEAM_ID      = "ABCD1234EF"                # 10-character Team ID
APPLE_KEY_ID       = "ABCD123456"                # Key ID from the .p8 file
APPLE_PRIVATE_KEY  = "-----BEGIN PRIVATE KEY-----\n..."  # Full PEM content of the .p8 file
```

Apple does not use a static client secret. The framework generates a short-lived ES256 JWT from these four values on every token exchange. The `.p8` private key content should be stored as a multiline string (or loaded from an environment variable) — never committed to source control.

If `OAUTH_REDIRECT_URI` is not set, the server builds it from the request `Origin` header as `<origin>/auth/oauth/<provider>/complete`. This works for single-origin SPAs but is less reliable for server-rendered or multi-origin setups — prefer the explicit setting in production.

### GitHub Settings

```python
GITHUB_CLIENT_ID     = "your-github-oauth-app-client-id"
GITHUB_CLIENT_SECRET = "your-github-oauth-app-client-secret"
```

GitHub does not always return an email on the `/user` endpoint — if the user has marked their email as private, the provider falls back to `GET /user/emails` and picks the primary verified address. No extra configuration is needed; the default scope `read:user user:email` covers both cases.

| Setting | Default | Purpose |
|---|---|---|
| `GITHUB_CLIENT_ID` | — | OAuth App client ID from GitHub Developer Settings |
| `GITHUB_CLIENT_SECRET` | — | OAuth App client secret |
| `GITHUB_SCOPES` | `"read:user user:email"` | OAuth scopes requested from GitHub |

### Optional Settings

| Setting | Default | Purpose |
|---|---|---|
| `GOOGLE_SCOPES` | `"openid email profile"` | OAuth scopes requested from Google |
| `OAUTH_STATE_TTL` | `600` | Seconds a CSRF state token is valid (Redis-backed) |
| `ALLOWED_REDIRECT_URLS` | `[]` | Allowlist for per-request `redirect_uri` (see below) |

---

## Per-Request redirect_uri

For multi-app deployments (portal, urtiny, etc.) where each frontend has its own callback URL, the `begin` endpoint accepts an optional `redirect_uri` query parameter.

```
GET /api/auth/oauth/google/begin?redirect_uri=https://portal.example.com/auth/callback
```

### Allowlist Configuration

A `redirect_uri` is accepted only if it starts with a prefix on the allowlist. If no allowlist is configured and a `redirect_uri` is provided, the request returns `400`.

**Project-wide allowlist** (`settings.py`):

```python
ALLOWED_REDIRECT_URLS = [
    "https://portal.example.com/",
    "https://urtiny.example.com/",
]
```

**Per-group allowlist** (`Group.metadata["allowed_redirect_urls"]`):

```python
group.metadata["allowed_redirect_urls"] = [
    "https://tenant-a.example.com/",
]
group.save()
```

The group list is retrieved via `get_metadata_value()`, which traverses the parent chain. Project-wide and group lists are combined at validation time.

A `redirect_uri` may carry its own query string (e.g. an app passing
`?redirect=/workspaces/` through the login page). The allowlist is a **prefix**
match, so an appended query does not affect validation. The full URI — query
included — is stored as `frontend_uri` and reproduced when the callback bounces
the browser back: the callback merges its `code`/`state` (and any `group_uuid`)
into the existing query with `&` rather than appending a second `?`. This is what
lets a `?redirect=` target survive the OAuth round-trip. (The bundled
`mojo-auth.js` cooperates: when no explicit callback URL is given, its default
return URL keeps the current page's query string — minus any stale `code`/`state`
— and strips only the hash.)

### Security

- The validated `redirect_uri` is stored in the Redis state token (single-use, TTL-bound).
- The `complete` endpoint retrieves it from the state — the client never re-sends it.
- This prevents an attacker from substituting a different `redirect_uri` in the callback.
- Because the allowlist is a **prefix** match, the query part of a `frontend_uri`
  is not validated. So the callback strips any `code`/`state`/`group_uuid` the
  caller smuggled into that query before appending the server's own — otherwise a
  duplicate `?code=` placed first would shadow the real value (`URLSearchParams
  .get()` returns the first match) and sabotage the victim's login.

---

## OAuthConnection Model

```python
# mojo/apps/account/models/oauth.py

class OAuthConnection(MojoSecrets, MojoModel):
    user         = ForeignKey(User, related_name="oauth_connections")
    provider     = CharField(max_length=32)      # e.g. "google"
    provider_uid = CharField(max_length=255)     # provider's stable user ID (e.g. Google "sub")
    email        = EmailField(null=True)         # email as reported by provider at link time
    is_active    = BooleanField(default=True)
    created      = DateTimeField(auto_now_add=True)
    modified     = DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("provider", "provider_uid")]
```

Access/refresh tokens from the provider are stored encrypted in `mojo_secrets` (via `MojoSecrets`). They are refreshed on every successful OAuth login. They are never exposed in REST graphs.

**One connection per (user, provider) pair.** A user who has connected Google once will always reuse that connection on subsequent logins.

### REST Permissions

`OAuthConnection` exposes a REST endpoint via `MojoModel`:

- **View:** `owner` (the connected user) or `manage_users`
- **Save/Delete:** `manage_users` only
- `mojo_secrets` is always excluded from REST output (`NO_SHOW_FIELDS`)

---

## Auto-Link Logic

`_find_or_create_user(provider_name, profile)` resolves which account to use, in priority order:

1. **Existing `OAuthConnection`** for this `(provider, provider_uid)` → return that user directly
2. **Existing `User` with matching email** → create a new `OAuthConnection` linking this provider to that user
3. **No match** → create a new `User` + `OAuthConnection`

### Email Verification on Auto-Link

OAuth is treated as a trusted identity provider. When any of the three paths above runs, the framework guarantees `is_email_verified=True` on the resolved user:

- **Path 1 (existing connection):** no change to `is_email_verified` — the user was already verified when they first connected
- **Path 2 (email match):** if `is_email_verified` is currently `False`, it is set to `True` and saved — the provider has confirmed ownership of the address
- **Path 3 (new user):** `is_email_verified` is set to `True` at account creation time

This means a user who has never clicked a verification email will be automatically marked verified the first time they log in via OAuth. This is intentional: OAuth provider confirmation is considered equivalent to email link verification.

**`is_email_verified` is a `SUPERUSER_ONLY_FIELDS` write.** It cannot be cleared by a normal REST update. The only code paths that set it are internal (token flows, OAuth, magic login).

---

## MFA Behaviour

**OAuth logins bypass MFA.** A user with `requires_mfa=True` (TOTP/SMS enrolled) is not presented with an MFA challenge after completing an OAuth login. The JWT is issued directly.

### Rationale

OAuth is a trusted second factor in its own right:

- The user has already authenticated to a third-party identity provider (Google, etc.)
- The provider may have enforced its own MFA (Google Advanced Protection, Workspace policies, etc.)
- The CSRF `state` token (Redis-backed, single-use, 10-minute TTL) prevents replay and CSRF attacks
- The authorization `code` is exchanged server-side — it never passes through the browser unprotected

Requiring an *additional* TOTP or SMS step after a successful OAuth assertion would be redundant and would harm UX without meaningfully improving security. If your project has a strict policy requiring a local second factor regardless of OAuth, you can override `on_oauth_complete` in a project-level URL handler.

---

## Adding a New Provider

1. Create `mojo/apps/account/services/oauth/<provider>.py` subclassing `OAuthProvider`:

```python
# services/oauth/myprovider.py
import requests
from urllib.parse import urlencode, quote

from mojo.helpers import logit
from mojo.helpers.settings import settings
from .base import OAuthProvider

AUTH_URL  = "https://provider.example.com/oauth/authorize"
TOKEN_URL = "https://provider.example.com/oauth/token"
USER_URL  = "https://api.provider.example.com/user"


class MyOAuthProvider(OAuthProvider):

    name = "myprovider"

    def get_auth_url(self, state, redirect_uri):
        params = {
            "client_id": settings.get("MYPROVIDER_CLIENT_ID"),
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": "user email",
        }
        return f"{AUTH_URL}?{urlencode(params, quote_via=quote)}"

    def exchange_code(self, code, redirect_uri):
        resp = requests.post(TOKEN_URL, json={
            "client_id": settings.get("MYPROVIDER_CLIENT_ID"),
            "client_secret": settings.get("MYPROVIDER_CLIENT_SECRET"),
            "code": code,
        }, headers={"Accept": "application/json"}, timeout=10)
        if not resp.ok:
            logit.error("oauth.myprovider", f"Token exchange failed: {resp.status_code}")
            raise ValueError("Failed to exchange code")
        return resp.json()

    def get_profile(self, tokens):
        resp = requests.get(USER_URL, headers={
            "Authorization": f"Bearer {tokens['access_token']}",
            "Accept": "application/json",
        }, timeout=10)
        if not resp.ok:
            logit.error("oauth.myprovider", f"Profile fetch failed: {resp.status_code}")
            raise ValueError("Failed to fetch profile")
        data = resp.json()
        email = (data.get("email") or "").lower().strip()
        # Some providers don't return email directly — add a fallback here if needed
        if not email:
            raise ValueError("Could not retrieve verified email from provider")
        return {
            "uid": str(data["id"]),
            "email": email,
            "display_name": data.get("name"),
        }
```

> **Note on the `/user/emails` fallback:** GitHub may not return an email on the `/user` endpoint when the user's email is set to private. The built-in `GitHubOAuthProvider` handles this by falling back to `GET /user/emails` and picking the entry where `primary=True` and `verified=True`. If your provider has a similar pattern, add an equivalent fallback in `get_profile()` before raising.

2. Register it in `services/oauth/__init__.py`:

```python
from .myprovider import MyOAuthProvider

PROVIDERS = {
    "google": GoogleOAuthProvider,
    "apple": AppleOAuthProvider,
    "github": GitHubOAuthProvider,
    "myprovider": MyOAuthProvider,
}
```

3. Add settings:

```python
MYPROVIDER_CLIENT_ID     = "your-client-id"
MYPROVIDER_CLIENT_SECRET = "your-client-secret"
```

The new provider is immediately available at:
- `GET /api/auth/oauth/myprovider/begin`
- `POST /api/auth/oauth/myprovider/complete`

No URL registration or model changes are required.

---

## CSRF State Token

Each OAuth flow begins with a `state` token stored in Redis (key prefix `oauth:state:`). The token is:

- Generated as a random UUID hex string
- Stored in Redis with a TTL of `OAUTH_STATE_TTL` seconds (default 600)
- Consumed (deleted) immediately on use — single-use

If `consume_state()` returns `None` (expired, already used, or forged), the complete endpoint raises a `401`. This protects against CSRF and replay attacks regardless of provider.

Redis is a hard dependency for the OAuth flow. If Redis is unavailable, `begin` will raise and no state token will be issued.

---

## Incident Logging

All OAuth events are logged via `logit` under the `"oauth"` category:

| Event | Level |
|---|---|
| New user created via OAuth | `info` |
| Existing user email marked verified via OAuth | `info` |
| Successful login | `info` |
| Token exchange failure (provider error) | `error` |
| Profile fetch failure (provider error) | `error` |

These appear in your standard Mojo log output. Failed logins (invalid state, disabled account) raise `PermissionDeniedException` which is surfaced to the client as `401`/`403` and also recorded automatically by the framework's incident system.

---

## Security Design Notes

- **No password is created** for OAuth-only users. If a user registers via OAuth and later wants password login, they must use the "forgot password" / reset flow to set one.
- **`provider_uid` is the stable identifier**, not the email. A user who changes their Google email address is still matched correctly on the next OAuth login via the existing `OAuthConnection`.
- **Access and refresh tokens are stored encrypted** in `mojo_secrets` and updated on every login. They are available for server-side API calls on behalf of the user if needed, but are never exposed in REST responses.
- **`is_email_verified` cannot be downgraded** via REST by a non-superuser. Once set by OAuth (or any other trusted flow), it stays set.

---

## See Also

- [OAuth REST API](../../../web_developer/account/oauth.md) — client-facing flow, JavaScript examples, error table
- [Authentication Flow](auth.md) — JWT tokens, MFA, password reset
- [User Model](user.md) — `is_email_verified`, `requires_mfa`, `SUPERUSER_ONLY_FIELDS`
