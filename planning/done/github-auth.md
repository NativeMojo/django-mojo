# GitHub Authentication — OAuth Login + App Service

**Type**: request
**Status**: resolved
**Date**: 2026-04-13
**Priority**: high

## Description

Two complementary GitHub auth capabilities:

1. **GitHub OAuth Provider** (user login) — A `GitHubOAuthProvider` subclass of `OAuthProvider`, following the exact same pattern as Google and Apple. Users can sign in with their GitHub account. This is the primary deliverable.

2. **GitHub App Service** (server-to-server) — A stateless helper module for GitHub App API authentication: JWT generation, installation token exchange, and webhook signature verification. This lets consuming apps (like Maestro) authenticate with GitHub's API for operations like cloning private repos.

## Context

- The OAuth login provider is the straightforward part — django-mojo already has a clean `OAuthProvider` base class and provider registry. Adding GitHub is a ~75-line file following the Google provider pattern exactly.
- The GitHub App service comes from a working prototype in the Maestro codebase (`mojo-orchestra/apps/mojo_orchestra/orchestra/services/github_app.py`). The generic parts (JWT signing, token exchange, webhook verification) belong in the framework; app-specific parts (models, clone URLs, webhook handlers) stay in Maestro.
- GitHub OAuth and GitHub App are independent — a project can use either or both.

## Acceptance Criteria

### Part 1: GitHub OAuth Provider
- [ ] `GitHubOAuthProvider` subclass of `OAuthProvider` in `mojo/apps/account/services/oauth/github.py`
- [ ] Registered in `PROVIDERS` dict in `oauth/__init__.py`
- [ ] Settings: `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`
- [ ] Implements `get_auth_url()`, `exchange_code()`, `get_profile()`
- [ ] GitHub API endpoints used: `https://github.com/login/oauth/authorize`, `https://github.com/login/oauth/access_token`, `https://api.github.com/user`
- [ ] Scopes: `read:user user:email` (to get email even if private)
- [ ] Email resolution: GitHub may not return email in profile — must also hit `GET /user/emails` and pick the primary verified email
- [ ] All existing OAuth infrastructure works automatically: begin/callback/complete endpoints, auto-link, OAuthConnection, JWT issuance, MFA bypass, frontend JS
- [ ] Docs updated in both `docs/django_developer/account/oauth.md` and `docs/web_developer/account/oauth.md`

### Part 2: GitHub App Service
- [ ] Service module at `mojo/helpers/github_app.py` (follows helper module pattern, not an app)
- [ ] `is_configured()` — checks `GITHUB_APP_ID` and `GITHUB_APP_PRIVATE_KEY` are set
- [ ] `generate_jwt()` — RS256 JWT signed with app private key (9 min TTL, 60s clock skew)
- [ ] `get_install_token(installation_id)` — stateless function, accepts installation_id string/int, returns `{token, expires_at, permissions}` dict. No model dependency. Let consuming app handle caching.
- [ ] `verify_webhook_signature(payload_body, signature_header)` — HMAC-SHA256 verification using `GITHUB_WEBHOOK_SECRET`
- [ ] Settings: `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY` (file path), `GITHUB_WEBHOOK_SECRET`
- [ ] Uses `PyJWT` (already a dependency) and `requests`

## Investigation

**What exists**:
- `OAuthProvider` base class: `mojo/apps/account/services/oauth/base.py` — state management, abstract contract
- Provider registry: `mojo/apps/account/services/oauth/__init__.py` — `PROVIDERS` dict + `get_provider()`
- Google provider: `mojo/apps/account/services/oauth/google.py` — canonical pattern to follow (76 lines)
- Apple provider: `mojo/apps/account/services/oauth/apple.py` — more complex (JWT client secret), less relevant
- REST endpoints: `mojo/apps/account/rest/oauth.py` — generic, works for any registered provider
- `OAuthConnection` model: `mojo/apps/account/models/oauth.py`
- Frontend JS: `mojo/apps/account/static/account/mojo-auth.js` — `startOAuthLogin(provider)` already generic
- Reference implementation: `/Users/ians/Projects/mojo/mojo-orchestra/apps/mojo_orchestra/orchestra/services/github_app.py`

**What changes**:

| File | Change |
|---|---|
| `mojo/apps/account/services/oauth/github.py` | **New** — GitHubOAuthProvider |
| `mojo/apps/account/services/oauth/__init__.py` | Add GitHub to PROVIDERS registry |
| `mojo/helpers/github_app.py` | **New** — GitHub App service (JWT, tokens, webhooks) |
| `docs/django_developer/account/oauth.md` | Add GitHub settings + notes |
| `docs/web_developer/account/oauth.md` | Add GitHub to provider list |

**Constraints**:
- GitHub OAuth has a quirk: the `/user` endpoint may not return an email if the user's email is private. Must also call `GET /user/emails` with the access token to get the primary verified email. Google and Apple don't have this issue.
- The GitHub App service must be stateless — `get_install_token()` returns data, doesn't persist anything. The reference implementation couples to a `GitHubInstall` model; the framework version must not.
- `PyJWT` is already in the dependency tree (used by Apple OAuth for ES256). RS256 support requires `cryptography` which is also already present.
- No type hints per project conventions.
- Use `logit` for all logging, never stdlib `logging`.

**Related files**:
- `mojo/apps/account/services/oauth/base.py`
- `mojo/apps/account/services/oauth/google.py`
- `mojo/apps/account/services/oauth/__init__.py`
- `mojo/apps/account/rest/oauth.py`
- `mojo/apps/account/models/oauth.py`
- `mojo/apps/account/static/account/mojo-auth.js`
- `mojo/helpers/` (for the App service module)

## Settings

### GitHub OAuth (Part 1)
| Setting | Default | Purpose |
|---|---|---|
| `GITHUB_CLIENT_ID` | — | OAuth App client ID from GitHub |
| `GITHUB_CLIENT_SECRET` | — | OAuth App client secret from GitHub |
| `GITHUB_SCOPES` | `"read:user user:email"` | OAuth scopes requested |

### GitHub App Service (Part 2)
| Setting | Default | Purpose |
|---|---|---|
| `GITHUB_APP_ID` | — | GitHub App ID |
| `GITHUB_APP_PRIVATE_KEY` | — | Path to `.pem` private key file |
| `GITHUB_WEBHOOK_SECRET` | — | Webhook HMAC secret |

## Tests Required

### OAuth Provider
- Test `get_auth_url()` returns correct GitHub authorize URL with params
- Test `exchange_code()` calls correct token endpoint (mock `requests.post`)
- Test `get_profile()` handles both cases: email in `/user` response and email only in `/user/emails`
- Test `get_profile()` picks primary verified email from `/user/emails` list
- Test provider is registered and discoverable via `get_provider("github")`
- Integration test: full begin → complete flow (same pattern as existing OAuth tests)

### GitHub App Service
- Test `is_configured()` returns False when settings missing
- Test `generate_jwt()` produces valid RS256 JWT with correct claims
- Test `get_install_token()` calls correct GitHub API endpoint, returns parsed response
- Test `verify_webhook_signature()` accepts valid signatures, rejects invalid
- Test `verify_webhook_signature()` rejects when `GITHUB_WEBHOOK_SECRET` not set

## Out of Scope

- `get_clone_url()` — app-specific glue (stays in Maestro)
- Webhook event handlers (`handle_installation_event`, `handle_push_event`) — app-specific
- Webhook REST endpoint — consuming apps define their own
- How apps link installations to their domain models (e.g., Maestro links to Project via metadata)
- GitHub OAuth App / GitHub App creation/registration — done in GitHub's UI
- Multiple GitHub Apps per project — YAGNI; settings are per-project
- Listing installations via `GET /app/installations` — v2 if needed

## Plan

**Status**: resolved
**Planned**: 2026-04-13

### Objective

Add GitHub authentication to django-mojo: an OAuth login provider (like Google/Apple) and a GitHub App service with model, token management, webhook verification, and REST.

### Steps

#### Part 1: GitHub OAuth Provider (user login)

1. **`mojo/apps/account/services/oauth/github.py`** (new, ~80 lines)
   - Subclass `OAuthProvider`, set `name = "github"`
   - `get_auth_url(state, redirect_uri)` — build `https://github.com/login/oauth/authorize` URL with `GITHUB_CLIENT_ID`, scope (`GITHUB_SCOPES`, default `"read:user user:email"`), state, redirect_uri
   - `exchange_code(code, redirect_uri)` — POST to `https://github.com/login/oauth/access_token` with `Accept: application/json` header. Send `client_id`, `client_secret`, `code`.
   - `get_profile(tokens)` — GET `https://api.github.com/user` with `Authorization: Bearer {access_token}`. If `email` is null/empty (user has private email), GET `https://api.github.com/user/emails` and pick the entry where `primary=True` and `verified=True`. Raise `ValueError` if no verified email found. Return `{"uid": str(id), "email": email, "display_name": name or login}`.

2. **`mojo/apps/account/services/oauth/__init__.py`** — add import + register in `PROVIDERS`:
   ```python
   from .github import GitHubOAuthProvider
   PROVIDERS["github"] = GitHubOAuthProvider
   ```

   Endpoints automatically available: `GET /api/auth/oauth/github/begin`, `POST /api/auth/oauth/github/complete`. No URL registration needed.

#### Part 2: GitHub App — model, service, decorator

3. **`mojo/apps/github/`** (new app)
   - `__init__.py`
   - `models/__init__.py`
   - `models/github_install.py`
   - `services/__init__.py`
   - `services/github_app.py`
   - `rest/__init__.py`
   - `rest/github_install.py`

4. **`mojo/apps/github/models/github_install.py`** — `GitHubInstall` model
   - Inherits `MojoSecrets, MojoModel` (no `models.Model`)
   - Fields:
     - `created` — `DateTimeField(auto_now_add=True, editable=False, db_index=True)`
     - `modified` — `DateTimeField(auto_now=True, db_index=True)`
     - `group` — `ForeignKey("account.Group", null=True, blank=True, on_delete=CASCADE, related_name="github_installs")` — null for global installs
     - `installation_id` — `BigIntegerField(db_index=True, unique=True)` — GitHub's installation ID
     - `account_name` — `CharField(max_length=255)` — GitHub org or user login
     - `token_expires_at` — `DateTimeField(null=True, blank=True)`
     - `permissions` — `JSONField(default=dict, blank=True)` — permissions granted by installation
     - `metadata` — `JSONField(default=dict, blank=True)` — app-specific data (consuming apps store `repo_full_name`, etc.)
   - `RestMeta`:
     ```python
     class RestMeta:
         VIEW_PERMS = ["github", "view_github", "manage_github"]
         SAVE_PERMS = ["github", "manage_github"]
         NO_SHOW_FIELDS = ["mojo_secrets"]
         GRAPHS = {
             "list": {"fields": ["id", "installation_id", "account_name", "token_expires_at", "created"]},
             "default": {"fields": ["id", "installation_id", "account_name", "token_expires_at", "permissions", "metadata", "group", "created", "modified"]},
         }
     ```

5. **`mojo/apps/github/services/github_app.py`** — GitHub App service (~100 lines)
   - `_get_app_id()` — reads `GITHUB_APP_ID` from settings
   - `_get_private_key()` — reads PEM file from path in `GITHUB_APP_PRIVATE_KEY` setting, logs error if file missing
   - `is_configured()` — returns `bool(_get_app_id() and _get_private_key())`
   - `generate_jwt()` — RS256 JWT, `iat = now - 60` (clock skew), `exp = now + 540` (9 min), `iss = app_id`. Raises `ValueError` if not configured.
   - `get_install_token(install)` — takes a `GitHubInstall` instance:
     1. If `install.secrets.get("token")` and `is_token_valid(install.token_expires_at)` → return cached `install.secrets.token`
     2. Otherwise: `generate_jwt()`, POST to `https://api.github.com/app/installations/{install.installation_id}/access_tokens`, parse response
     3. `install.set_secret("token", data["token"])`, update `install.token_expires_at` (parsed to datetime), update `install.permissions`, `install.save()`
     4. Return token string
     5. Raise `ValueError` on API failure
   - `is_token_valid(expires_at, buffer_seconds=300)` — returns `True` if `expires_at` is not None and more than `buffer_seconds` in the future
   - `verify_webhook_signature(payload_body, signature_header)` — HMAC-SHA256 using `GITHUB_WEBHOOK_SECRET`. Returns `False` if secret not configured or signature missing/invalid. Uses `hmac.compare_digest` for timing-safe comparison.

6. **`mojo/apps/github/rest/github_install.py`** — standard CRUD
   ```python
   @md.URL("github_install")
   @md.URL("github_install/<int:pk>")
   @md.uses_model_security(GitHubInstall)
   def on_github_install(request, pk=None):
       return GitHubInstall.on_rest_request(request, pk)
   ```

7. **`mojo/decorators/github.py`** (new) — webhook signature decorator
   - `requires_github_webhook()` — decorator that:
     1. Reads `request.body` and `request.META.get("HTTP_X_HUB_SIGNATURE_256")`
     2. Calls `verify_webhook_signature(body, sig)`
     3. If invalid → return 403 `"Invalid webhook signature"`
     4. Passes through to view function on success
   - Register on `md` so it's usable as `@md.requires_github_webhook()`

#### Part 3: Docs

8. **`docs/django_developer/account/oauth.md`**
   - Add GitHub to "Current providers" line
   - Add GitHub settings section (GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET, GITHUB_SCOPES)
   - Update the "Adding a New Provider" example to include the `/user/emails` fallback (it's already a GitHub example but incomplete)

9. **`docs/web_developer/account/oauth.md`**
   - Add `github` to supported providers list
   - Add GitHub settings to configuration section
   - Add GitHub flow note (standard redirect, same as Google)

10. **`docs/django_developer/github/README.md`** (new)
    - GitHub App overview — what it is, when to use it
    - GitHubInstall model fields and RestMeta
    - Service functions: `is_configured()`, `generate_jwt()`, `get_install_token()`, `is_token_valid()`, `verify_webhook_signature()`
    - Settings table: `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY`, `GITHUB_WEBHOOK_SECRET`
    - `@md.requires_github_webhook()` decorator usage
    - Example: webhook endpoint pattern

11. **`docs/web_developer/github/README.md`** (new)
    - GitHubInstall REST API: list, detail, create, update, delete
    - Permissions required
    - Response examples

12. **`docs/django_developer/README.md`** — add github to Built-in Apps table

13. **`docs/web_developer/README.md`** — add github to index (if applicable)

#### Part 4: Infrastructure

14. **`INSTALLED_APPS`** — add `mojo.apps.github` to the default app list (or document it as opt-in)
15. **`bin/create_testproject`** — run after model is added to generate migrations
16. **`CHANGELOG.md`** — document the new feature

### Design Decisions

| Decision | Rationale |
|---|---|
| OAuth provider in `services/oauth/github.py` | Same location as Google/Apple — consistent pattern |
| GitHub App as `mojo/apps/github/` (not `mojo/helpers/`) | Has a model + REST — it's an app, not a utility |
| `get_install_token(install)` takes model instance | Handles caching internally — no boilerplate in consuming apps |
| `group` FK is nullable | `null` = global install (not tied to a specific group) |
| `installation_id` is `BigIntegerField` + `unique=True` | GitHub IDs are large ints; one install record per GitHub installation |
| Permission category is `"github"` | Specific to the domain; `"integrations"` is too abstract |
| `is_token_valid()` as a public utility | Small but standardizes the buffer logic across consumers |
| Webhook decorator in `mojo/decorators/github.py` | Consistent with `bouncer.py`, `limits.py`, and other `@md.*` decorators |
| `expires_at` parsed to datetime by the service | Every consumer needs a datetime; parse once in the framework |
| No multi-app support | One GitHub App per project via settings. Multi-app would need DB-stored config — different pattern entirely. YAGNI. |

### Edge Cases

| Risk | Handling |
|---|---|
| GitHub email is private, no verified email in `/user/emails` | `get_profile()` raises `ValueError("Could not retrieve verified email from GitHub")` — auto-link requires email |
| Multiple verified emails in `/user/emails` | Pick the one with `primary=True` |
| `GITHUB_APP_PRIVATE_KEY` file doesn't exist | `_get_private_key()` logs error via logit, returns None; `generate_jwt()` raises `ValueError` |
| GitHub token endpoint returns error | `get_install_token()` raises `ValueError` with status code |
| Cached token expires between check and use | 5-minute buffer (`TOKEN_BUFFER_SECONDS = 300`) mitigates this |
| `request.body` already consumed before decorator reads it | Django caches `request.body` — safe to read multiple times |
| `verify_webhook_signature` called without `GITHUB_WEBHOOK_SECRET` set | Returns `False`, logs warning — fail-closed |

### Testing

| Scenario | File |
|---|---|
| GitHub OAuth: begin returns auth_url, email fallback from `/user/emails`, provider registration | `tests/test_oauth/oauth_github.py` |
| GitHubInstall: model CRUD, secret encryption roundtrip, REST permissions | `tests/test_github/github_install.py` |
| GitHub App service: JWT generation, token caching, `is_token_valid()`, webhook signature | `tests/test_github/github_app.py` |
| Webhook decorator: valid sig passes, invalid sig returns 403, missing secret returns 403 | `tests/test_github/github_app.py` |

### Docs

| File | Change |
|---|---|
| `docs/django_developer/account/oauth.md` | Add GitHub provider, settings, fix example |
| `docs/web_developer/account/oauth.md` | Add GitHub to providers list + settings |
| `docs/django_developer/github/README.md` | New — GitHub App service + model docs |
| `docs/web_developer/github/README.md` | New — GitHubInstall REST API docs |
| `docs/django_developer/README.md` | Add github to Built-in Apps table |
| `CHANGELOG.md` | Document new feature |
