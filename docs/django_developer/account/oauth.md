# OAuth / Social Login — Django Developer Reference

## Overview

OAuth2 social login is built into the framework. The full flow — CSRF state management, provider token exchange, user resolution, and JWT issuance — is handled by the framework. Your project only needs to configure credentials and (optionally) register additional providers.

**Current providers:** `google`

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
| `mojo/apps/account/services/oauth/__init__.py` | Provider registry |

---

## Required Settings

```python
# settings.py

GOOGLE_CLIENT_ID     = "your-client-id.apps.googleusercontent.com"
GOOGLE_CLIENT_SECRET = "your-client-secret"

# The URL Google redirects back to after login.
# Must match an authorised redirect URI in Google Cloud Console.
OAUTH_REDIRECT_URI = "https://your-app.example.com/auth/oauth/google/complete"
```

If `OAUTH_REDIRECT_URI` is not set, the server builds it from the request `Origin` header as `<origin>/auth/oauth/<provider>/complete`. This works for single-origin SPAs but is less reliable for server-rendered or multi-origin setups — prefer the explicit setting in production.

### Optional Settings

| Setting | Default | Purpose |
|---|---|---|
| `GOOGLE_SCOPES` | `"openid email profile"` | OAuth scopes requested from Google |
| `OAUTH_STATE_TTL` | `600` | Seconds a CSRF state token is valid (Redis-backed) |

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
# services/oauth/github.py
import requests
from .base import OAuthProvider
from mojo.helpers.settings import settings

GITHUB_AUTH_URL  = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_USER_URL  = "https://api.github.com/user"


class GitHubOAuthProvider(OAuthProvider):

    name = "github"

    def get_auth_url(self, state, redirect_uri):
        client_id = settings.get("GITHUB_CLIENT_ID")
        return (
            f"{GITHUB_AUTH_URL}"
            f"?client_id={client_id}"
            f"&redirect_uri={requests.utils.quote(redirect_uri)}"
            f"&state={state}"
            f"&scope=user:email"
        )

    def exchange_code(self, code, redirect_uri):
        resp = requests.post(GITHUB_TOKEN_URL, json={
            "client_id": settings.get("GITHUB_CLIENT_ID"),
            "client_secret": settings.get("GITHUB_CLIENT_SECRET"),
            "code": code,
        }, headers={"Accept": "application/json"}, timeout=10)
        if not resp.ok:
            raise ValueError("Failed to exchange code with GitHub")
        return resp.json()

    def get_profile(self, tokens):
        resp = requests.get(GITHUB_USER_URL, headers={
            "Authorization": f"token {tokens['access_token']}",
            "Accept": "application/json",
        }, timeout=10)
        if not resp.ok:
            raise ValueError("Failed to fetch GitHub profile")
        data = resp.json()
        email = (data.get("email") or "").lower().strip()
        return {
            "uid": str(data["id"]),
            "email": email,
            "display_name": data.get("name") or data.get("login"),
        }
```

2. Register it in `services/oauth/__init__.py`:

```python
from .github import GitHubOAuthProvider

PROVIDERS = {
    "google": GoogleOAuthProvider,
    "github": GitHubOAuthProvider,
}
```

3. Add settings:

```python
GITHUB_CLIENT_ID     = "your-github-app-client-id"
GITHUB_CLIENT_SECRET = "your-github-app-client-secret"
```

The new provider is immediately available at:
- `GET /api/auth/oauth/github/begin`
- `POST /api/auth/oauth/github/complete`

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
