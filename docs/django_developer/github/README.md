# GitHub App — Django Developer Reference

## Overview

The `mojo.apps.github` app provides two independent capabilities:

1. **GitHub OAuth login** — users can sign in with their GitHub account (via the standard OAuth provider system in `mojo.apps.account`)
2. **GitHub App integration** — server-to-server JWT authentication, installation access token management, and webhook signature verification for GitHub App installations

These are independent. A project can use OAuth login without the App service, the App service without OAuth login, or both.

---

## Setup

Add to `INSTALLED_APPS`:

```python
INSTALLED_APPS = [
    ...
    "mojo.apps.github",
]
```

Run migrations after adding the app:

```
python manage.py makemigrations
python manage.py migrate
```

---

## GitHub OAuth Login

GitHub OAuth login is handled by the standard OAuth provider system. Once configured, the framework endpoints `GET /api/auth/oauth/github/begin` and `POST /api/auth/oauth/github/complete` are available automatically.

See [OAuth / Social Login](../account/oauth.md) for full documentation on the OAuth flow, auto-link logic, MFA bypass, and frontend integration.

### Required Settings

```python
GITHUB_CLIENT_ID     = "your-github-oauth-app-client-id"
GITHUB_CLIENT_SECRET = "your-github-oauth-app-client-secret"
```

| Setting | Default | Purpose |
|---|---|---|
| `GITHUB_CLIENT_ID` | — | OAuth App client ID from GitHub Developer Settings |
| `GITHUB_CLIENT_SECRET` | — | OAuth App client secret |
| `GITHUB_SCOPES` | `"read:user user:email"` | OAuth scopes requested from GitHub |

### Private Email Fallback

GitHub may not return an email on the `/user` endpoint when the user's email is set to private. The `GitHubOAuthProvider` automatically falls back to `GET /user/emails` and picks the entry where `primary=True` and `verified=True`. If no verified email is found, the login fails with a `ValueError` (surfaced as a 400 to the client). No configuration is needed — the default `GITHUB_SCOPES` value covers both endpoints.

---

## GitHubInstall Model

`mojo/apps/github/models/github_install.py`

Tracks GitHub App installations. Each record represents one installation on a GitHub org or user account. The installation access token is stored encrypted via `MojoSecrets`.

```python
class GitHubInstall(MojoSecrets, MojoModel):
    group          = ForeignKey("account.Group", null=True, blank=True, on_delete=CASCADE)
    installation_id = BigIntegerField(unique=True, db_index=True)
    account_name   = CharField(max_length=255)
    token_expires_at = DateTimeField(null=True, blank=True)
    permissions    = JSONField(default=dict, blank=True)
    metadata       = JSONField(default=dict, blank=True)
    created        = DateTimeField(auto_now_add=True, db_index=True)
    modified       = DateTimeField(auto_now=True, db_index=True)
```

| Field | Description |
|---|---|
| `group` | Group scope. `NULL` for global (project-wide) installations. |
| `installation_id` | GitHub's numeric installation ID. Unique. |
| `account_name` | GitHub org or user login that installed the app. |
| `token_expires_at` | Expiry of the cached installation access token. |
| `permissions` | Permissions granted by the installation (populated from GitHub's token response). |
| `metadata` | App-specific data. Consuming apps can store repo names, linked model IDs, etc. |

The access token is stored encrypted via `MojoSecrets` using `install.get_secret("token")` / `install.set_secret("token", value)`. It is never exposed in REST graphs (`NO_SHOW_FIELDS = ["mojo_secrets"]`).

### RestMeta

```python
class RestMeta:
    VIEW_PERMS = ["github", "view_github", "manage_github"]
    SAVE_PERMS = ["github", "manage_github"]
    CAN_DELETE = True
    NO_SHOW_FIELDS = ["mojo_secrets"]
    GRAPHS = {
        "list": {
            "fields": ["id", "installation_id", "account_name", "token_expires_at", "created"],
        },
        "default": {
            "fields": [
                "id", "installation_id", "account_name", "token_expires_at",
                "permissions", "metadata", "group", "created", "modified",
            ],
        },
    }
```

---

## GitHub App Service

`mojo/apps/github/services/github_app.py`

Stateless helper functions for GitHub App API authentication. Logs to `github.log` via a named logger.

### Settings

| Setting | Default | Purpose |
|---|---|---|
| `GITHUB_APP_ID` | — | GitHub App ID (from GitHub App settings page) |
| `GITHUB_APP_PRIVATE_KEY` | — | Absolute path to the RSA `.pem` private key file |
| `GITHUB_WEBHOOK_SECRET` | — | HMAC secret for webhook signature verification |

### Functions

#### `is_configured()`

Returns `True` if both `GITHUB_APP_ID` and `GITHUB_APP_PRIVATE_KEY` are set and the key file exists.

```python
from mojo.apps.github.services.github_app import is_configured

if is_configured():
    ...
```

#### `generate_jwt()`

Generates a short-lived RS256 JWT for GitHub App API authentication. GitHub requires JWTs to be under 10 minutes; this function uses a 9-minute expiry with a 60-second clock-skew tolerance.

```python
from mojo.apps.github.services.github_app import generate_jwt

app_jwt = generate_jwt()
# Use in Authorization: Bearer <app_jwt> headers for /app/* endpoints
```

Raises `ValueError` if `GITHUB_APP_ID` or `GITHUB_APP_PRIVATE_KEY` is not configured.

#### `get_install_token(install)`

Returns a valid installation access token for a `GitHubInstall` instance. Checks the cached token first; refreshes from GitHub only when expired or within 5 minutes of expiry.

```python
from mojo.apps.github.models import GitHubInstall
from mojo.apps.github.services.github_app import get_install_token

install = GitHubInstall.objects.get(installation_id=12345)
token = get_install_token(install)

# Use token in GitHub API calls on behalf of the installation
resp = requests.get(
    "https://api.github.com/repos/myorg/myrepo/contents/",
    headers={"Authorization": f"Bearer {token}"},
)
```

On refresh, the new token and expiry are saved back to the `install` instance automatically.

Raises `ValueError` if the token refresh API call fails or the app is not configured.

#### `is_token_valid(expires_at, buffer_seconds=300)`

Returns `True` if `expires_at` is not `None` and is more than `buffer_seconds` in the future (default 5 minutes).

#### `verify_webhook_signature(payload_body, signature_header)`

Verifies a GitHub webhook payload using HMAC-SHA256 and `GITHUB_WEBHOOK_SECRET`. Uses `hmac.compare_digest` for timing-safe comparison.

```python
from mojo.apps.github.services.github_app import verify_webhook_signature

is_valid = verify_webhook_signature(request.body, request.META.get("HTTP_X_HUB_SIGNATURE_256", ""))
```

Returns `False` (does not raise) if the secret is not configured, the header is missing, or the signature does not match.

---

## `@md.requires_github_webhook()` Decorator

`mojo/decorators/github.py`

Validates the `X-Hub-Signature-256` header on incoming GitHub webhook requests. Returns `403` if the signature is missing, invalid, or `GITHUB_WEBHOOK_SECRET` is not configured.

```python
import mojo.decorators as md

@md.POST("webhook/github")
@md.public_endpoint()
@md.requires_github_webhook()
def on_github_webhook(request):
    event = request.META.get("HTTP_X_GITHUB_EVENT")
    payload = request.DATA

    if event == "installation":
        action = payload.get("action")
        installation = payload.get("installation", {})
        install_id = installation.get("id")
        account = installation.get("account", {}).get("login")
        # create or update GitHubInstall record
        ...

    return {"ok": True}
```

The decorator must come after `@md.public_endpoint()` in the decorator stack (i.e., listed after it, so it wraps the function closer). The endpoint must be public because GitHub does not send an Authorization header.

---

## Permissions

| Permission | Description |
|---|---|
| `github` | Domain category permission — baseline for any GitHub access |
| `view_github` | Read-only access to `GitHubInstall` records |
| `manage_github` | Full read/write/delete access to `GitHubInstall` records |

Include `github` alongside fine-grained perms on non-RestMeta endpoints that use `@md.requires_perms()`.

---

## See Also

- [GitHub REST API](../../../web_developer/github/README.md) — client-facing endpoint docs
- [OAuth / Social Login](../account/oauth.md) — full OAuth flow documentation
