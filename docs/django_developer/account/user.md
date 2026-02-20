# User Model — Django Developer Reference

## Inheritance

```python
class User(MojoSecrets, AbstractBaseUser, MojoModel):
```

`User` inherits from `MojoSecrets` (encrypted storage), `AbstractBaseUser` (Django auth), and `MojoModel` (REST). Do not add `models.Model` — it is provided by the base classes.

## Key Fields

| Field | Type | Description |
|---|---|---|
| `username` | TextField (unique) | Login username (lowercased) |
| `email` | EmailField (unique) | Email address |
| `display_name` | CharField | Display name |
| `is_active` | BooleanField | Account enabled flag |
| `is_staff` | BooleanField | Django admin access |
| `is_superuser` | BooleanField | Superuser flag |
| `permissions` | JSONField | Key-based permission dict |
| `metadata` | JSONField | Arbitrary user metadata |
| `org` | FK → Group | Primary organization/tenant |
| `avatar` | FK → fileman.File | Profile image |
| `last_activity` | DateTimeField | Last seen timestamp |
| `auth_key` | TextField | Per-user JWT signing key |

## RestMeta Configuration

```python
class RestMeta:
    LOG_CHANGES = True
    VIEW_PERMS = ["view_users", "manage_users", "owner"]
    SAVE_PERMS = ["manage_users", "owner"]
    OWNER_FIELD = "self"           # owner = user is themselves
    NO_SHOW_FIELDS = ["password", "auth_key", "onetime_code"]
    SEARCH_FIELDS = ["username", "email", "display_name"]
    POST_SAVE_ACTIONS = ["send_invite"]
    GRAPHS = {
        "basic": {"fields": ["id", "display_name", "username", "last_activity", "is_active"]},
        "default": {"fields": ["id", "display_name", "username", "email", "phone_number",
                               "permissions", "metadata", "is_active"]},
        "full": {}
    }
```

## Permission System

Permissions are stored as a JSON dict on `user.permissions`:

```python
# Check single permission
user.has_permission("manage_users")

# Check any of multiple permissions
user.has_permission(["manage_users", "view_users"])

# Add / remove
user.add_permission("manage_reports")
user.remove_permission("manage_reports")
user.save()
```

**Protected permissions** — Certain permissions (e.g., `manage_users`) can only be granted by a user who themselves has `manage_users`. This is enforced via `USER_PERMS_PROTECTION` in settings.

## JWT Authentication

```python
from mojo.apps.account.utils.jwtoken import JWToken

# Create token pair
token_package = JWToken(user.get_auth_key()).create(uid=user.id)
# Returns: {"access_token": "...", "refresh_token": "...", "expires_in": 21600}

# Validate token
user, error = User.validate_jwt(token_string)
```

Token expiry is configured via settings:

```python
JWT_TOKEN_EXPIRY = 21600          # access token: 6 hours (seconds)
JWT_REFRESH_TOKEN_EXPIRY = 604800 # refresh token: 7 days (seconds)
```

## API Key Generation

```python
token = user.generate_api_token(
    allowed_ips=["1.2.3.4", "5.6.7.8"],
    expire_days=360
)
```

API keys are long-lived JWTs restricted to specific IPs.

## Activity Tracking

```python
user.touch()   # updates last_activity (rate-limited by USER_LAST_ACTIVITY_FREQ)
user.track()   # touch() + track device from active request
```

## Group Membership

```python
groups = user.get_groups()                    # all groups
groups = user.get_groups(include_children=False)  # direct memberships only
groups_with_perm = user.get_groups_with_permission(["manage_users"])
```

## Password Reset Flow

1. Call `POST /api/auth/forgot` with `email` and `method=code` or `method=link`
2. For `method=code`: a 6-digit code is stored in secrets and emailed
3. For `method=link`: a signed token is emailed
4. Reset via `POST /api/auth/password/reset/code` or `POST /api/auth/password/reset/token`

## Post-Save Actions

Add `send_invite` to `POST_SAVE_ACTIONS`. When creating a user with `send_invite=true` in the POST body, `on_action_send_invite` is called after save.

## Settings

| Setting | Default | Description |
|---|---|---|
| `JWT_TOKEN_EXPIRY` | `21600` | Access token TTL (seconds) |
| `JWT_REFRESH_TOKEN_EXPIRY` | `604800` | Refresh token TTL (seconds) |
| `PASSWORD_RESET_TOKEN_TTL` | `3600` | Password reset link TTL (seconds) |
| `PASSWORD_RESET_CODE_TTL` | `600` | Password reset code TTL (seconds) |
| `USER_LAST_ACTIVITY_FREQ` | `300` | Min seconds between activity updates |
| `USER_PERMS_PROTECTION` | (system defaults) | Dict of perm → required perm to grant it |
