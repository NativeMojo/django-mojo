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
| `first_name` | CharField | First name |
| `last_name` | CharField | Last name |
| `display_name` | CharField | Display name (auto-generated if blank) |
| `is_active` | BooleanField | Account enabled flag |
| `is_staff` | BooleanField | Django admin access |
| `is_superuser` | BooleanField | Superuser flag |
| `is_email_verified` | BooleanField | Email address verified flag |
| `is_phone_verified` | BooleanField | Phone number verified flag |
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

## Name Helpers

### `full_name` property

Returns the best available display name in priority order:

1. `first_name` + `last_name` (if either is set)
2. `display_name`
3. `generate_display_name()` — derived from username/email

```python
user.full_name  # e.g. "Alice Smith", "alice", or "Alice Smith" from "alice.smith@co.com"
```

### `infer_names_from_email()`

Best-effort extraction of `first_name` / `last_name` from a business email address.

Rules:
- Only runs when both `first_name` and `last_name` are empty
- Skips consumer domains (gmail, yahoo, hotmail, outlook, icloud, etc.)
- Only splits if the local part has **exactly two dot-separated parts**
- Skips single-character parts (e.g. `j.smith`)

Called automatically from `on_rest_created`. Names are written via a direct queryset `update()` to avoid triggering a second full save cycle.

```python
# john.smith@company.com → first_name="John", last_name="Smith"
# john@gmail.com         → skipped (consumer domain)
# j.smith@company.com    → skipped (single-char first part)
# info.support@co.com    → not blocked but relies on content_guard to catch obvious non-names
```

## Content Moderation

### Username

`validate_username()` runs `content_guard.check_username()` for non-email usernames. Catches profanity, reserved names, evasion variants (leet speak, skeleton matching, reversed text, edit distance).

```python
# Dots are allowed in usernames (policy override applied automatically)
# content_guard check is skipped when username == email
```

### Name Fields

`validate_name_fields()` runs `content_guard.check_text()` on `display_name`, `first_name`, and `last_name`. Called from `on_rest_pre_save` — only re-checks fields that actually changed on updates.

Uses a lowered block threshold (`text_block_threshold=50`) since short strings carry proportionally more weight.

### Superuser Bypass

Both `validate_username()` and `validate_name_fields()` are bypassed entirely when `request.user.is_superuser`. This allows superusers to create accounts with reserved names like `admin`, `support`, or `root`.

## Protected Field Setters

The REST framework calls `set_<field>()` before saving if the method exists. These setters enforce permission checks:

```python
# Only a superuser can grant superuser or staff status
user.set_is_superuser(True)   # raises PermissionDeniedException if not superuser
user.set_is_staff(True)       # raises PermissionDeniedException if not superuser
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

## Email Verification Flow

```
POST /api/auth/verify/email/send     → sends email_verify template to user's address
GET  /api/auth/verify/email/confirm  → public, token in query string (click-through link)
```

`is_email_verified` is set to `True` on confirm. Both endpoints require the user to be authenticated except the confirm link which is public so it works directly from an email client.

### Auto-Verify via Invite

When a user accepts an invite link (`POST /api/auth/password/reset/token`) and `last_login is None` (i.e. they have never logged in — this is their first access), `is_email_verified` is set automatically. The act of receiving and clicking the invite link is sufficient proof of email ownership.

Magic login (`POST /api/auth/magic/login`) also auto-verifies email on use, since the magic link itself proves inbox access.

## Phone Verification Flow

```
POST /api/auth/verify/phone/send     → sends 6-digit SMS code via phonehub
POST /api/auth/verify/phone/confirm  → user submits code, sets is_phone_verified=True
```

The phone number is normalized via `phonehub.normalize()` before the SMS is sent. An invalid or un-normalizable number returns a `ValueException` before any Twilio call is made.

Code TTL is configurable via `PHONE_VERIFY_CODE_TTL` (default 600 seconds). Codes are single-use and consumed on successful verification.

## Post-Save Actions

Add `send_invite` to `POST_SAVE_ACTIONS`. When creating a user with `send_invite=true` in the POST body, `on_action_send_invite` is called after save.

## Settings

| Setting | Default | Description |
|---|---|---|
| `JWT_TOKEN_EXPIRY` | `21600` | Access token TTL (seconds) |
| `JWT_REFRESH_TOKEN_EXPIRY` | `604800` | Refresh token TTL (seconds) |
| `PASSWORD_RESET_TOKEN_TTL` | `3600` | Password reset link TTL (seconds) |
| `PASSWORD_RESET_CODE_TTL` | `600` | Password reset code TTL (seconds) |
| `EMAIL_VERIFY_TOKEN_TTL` | `86400` | Email verification link TTL (seconds) |
| `PHONE_VERIFY_CODE_TTL` | `600` | Phone verification code TTL (seconds) |
| `USER_LAST_ACTIVITY_FREQ` | `300` | Min seconds between activity updates |
| `USER_PERMS_PROTECTION` | (system defaults) | Dict of perm → required perm to grant it |