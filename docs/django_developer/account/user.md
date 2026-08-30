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
| `email` | EmailField (unique, nullable) | Email address. Nullable since migration 0043 — phone-only users have no email on file. |
| `first_name` | CharField | First name |
| `last_name` | CharField | Last name |
| `display_name` | CharField | Display name (auto-generated if blank) |
| `is_active` | BooleanField | Account enabled flag |
| `is_staff` | BooleanField | Django admin access |
| `is_superuser` | BooleanField | Superuser flag |
| `is_email_verified` | BooleanField | Email address verified flag |
| `is_phone_verified` | BooleanField | Phone number verified flag |
| `requires_mfa` | BooleanField | MFA required at login (superuser-only writable) |
| `is_dob_verified` | BooleanField | DOB verified flag (system-only, never REST-writable) |
| `dob` | DateField (nullable) | Date of birth — PII, cleared by `pii_anonymize()` |
| `permissions` | JSONField | Key-based permission dict |
| `metadata` | JSONField | Arbitrary user metadata. The `metadata["protected"]` sub-key is system-only — see below. |
| `org` | FK → Group | Primary organization/tenant |
| `avatar` | FK → fileman.File | Profile image |
| `last_activity` | DateTimeField | Last seen timestamp |
| `auth_key` | TextField | Per-user JWT signing key |
| `requires_password_change` | BooleanField | Server-managed durable temporary-password state; indexed and never REST-writable |

`account.0051_user_requires_password_change`, directly after
`account.0050_systemsetupoperation`, adds this field with a safe `False`
default for existing users.

### Avatar relation contract

`User.validate_avatar_file()` requires an active, completed, groupless File
classified as an image (`category == "image"` and an `image/*` content type)
with a real owner. This is classification validation, not deep image decoding.

A user may attach only their own File. A superuser or global
`users`/`manage_users` administrator editing another account may attach only a
File uploaded by that administrator. Ownership is not transferred to the
target user. Passing `null` clears the relation without deleting the detached
File. `resolve_avatar_file_upload_scope()` forces inline avatars to personal
scope (`group=None`) even when the request carries an ambient group.

## RestMeta Configuration

```python
class RestMeta:
    LOG_CHANGES = True
    VIEW_PERMS = ["view_users", "manage_users", "users", "owner"]
    SAVE_PERMS = ["manage_users", "users", "owner"]
    OWNER_FIELD = "self"           # owner = user is themselves
    NO_SHOW_FIELDS = ["password", "auth_key", "onetime_code"]
    NO_SAVE_FIELDS = ["auth_key", "last_activity", "is_dob_verified", "requires_password_change"]
    SEARCH_FIELDS = ["username", "email", "display_name", "phone_number"]
    POST_SAVE_ACTIONS = ["send_invite", "disable", "reactivate"]
    GRAPHS = {
        "basic": {"fields": ["id", "uuid", "display_name", "username", "last_login",
                             "last_activity", "is_active", "is_email_verified",
                             "is_phone_verified", "is_dob_verified",
                             "requires_password_change"]},
        "default": {"fields": ["id", "uuid", "first_name", "last_name", "display_name",
                               "username", "email", "phone_number", "last_login",
                               "last_activity", "permissions", "metadata", "is_active",
                               "is_superuser", "is_email_verified", "is_phone_verified",
                               "is_dob_verified", "dob", "requires_mfa",
                               "requires_password_change", "has_passkey"]},
        "full": {}
    }
```

## Name Helpers

### `full_name` property

Returns the best available display name in priority order:

1. `first_name` + `last_name` (if either is set)
2. `display_name`
3. `generate_display_name()` — priority chain: first+last → email local-part → friendly random placeholder ("BraveTiger"-style) → username. Phone numbers are intentionally NEVER used (PII).

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

When content_guard returns `decision="block"` for a name field, the result is treated as **advisory**: the name is logged as flagged and the save is **allowed**. This is intentional — content_guard's naive-substring matching over-blocks legitimate real names that merely contain a high-severity substring (e.g. Matsushita, Harshita, Scunthorpe). The guard's scoring logic is unchanged; only the response to "block" on name fields differs from other surfaces (comment/chat moderation still hard-blocks).

### Superuser Bypass

Both `validate_username()` and `validate_name_fields()` are bypassed entirely when `request.user.is_superuser`. This allows superusers to create accounts with reserved names like `admin`, `support`, or `root`.

## metadata.protected

`metadata["protected"]` is a system-controlled sub-key within the `metadata` JSON field. Writes to it via REST are blocked by `on_rest_update_jsonfield` in `mojo/models/rest.py` for any user who is not a superuser or does not have a permission listed in `PROTECTED_JSON_PERMS` (RestMeta prop, defaults to empty — superuser only).

Use it to record immutable system context that should never be overwritten by the user:

```python
user.metadata["protected"] = {
    "registration_source": "google",   # how the account was created
    "invited_by_id": 42,               # User.id of the person who sent the invite
    "invited_to_group_id": 7,          # Group.id of the first group invite
}
user.save(update_fields=["metadata"])
```

Only write these at creation time. Check `"protected" not in (user.metadata or {})` before writing to avoid overwriting existing context.

To allow a specific permission to write protected metadata:

```python
class RestMeta:
    PROTECTED_JSON_PERMS = ["manage_users"]
```

---

## Permission Tiers for User Field Writes

`users` (domain category) and `manage_users` (strict admin) are treated as
**equivalent** for User admin operations. Deployments simplify away the
`view_X` / `manage_X` split by holding only `users` for admin work; the
framework honours both perms wherever it would honour either.

### Superuser-Only Fields

These fields can only be written via REST by a superuser (`SUPERUSER_ONLY_FIELDS`):

- `is_dob_verified` — DOB verification compliance signal.

`is_superuser` and `is_staff` flips are also superuser-only via the
`set_is_superuser` / `set_is_staff` setters.

### Admin-Tier Fields

These fields require any admin tier — `users`, `manage_users`, or `is_superuser` (`ADMIN_ONLY_FIELDS`):

- `is_email_verified`, `is_phone_verified` — force-verify / unverify on behalf of another user.
- `requires_mfa` — admins manage MFA policy at the admin tier. Superuser is reserved for the single super-admin in deployments that follow that pattern.
- `is_active` — disable / reactivate (admin lifecycle op).
- `org`, `org_id` — org assignment (token TTLs, push routing).

`MANAGE_USERS_ONLY_FIELDS` is retained as an alias for `ADMIN_ONLY_FIELDS`
for back-compat with downstream code that imports it.

### Credential Field Writes (`email` / `username` / `phone_number` replace)

Gated by `_handle_existing_user_pre_save`:

- **Allowed** for any admin tier (`users` / `manage_users` / `is_superuser`).
- **Blocked** for self-acting users with only `owner` perm — they must use the dedicated change flows (`POST /api/auth/email/change/{request,confirm}` etc.) which verify ownership of the new channel via OTP/link.

Phone clear (setting `null`) and first-set (when the user has none) are
allowed for anyone with edit access.

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
# Returns: {"access_token": "...", "refresh_token": "..."}
# Expiry is baked into each token's `exp` claim, not returned alongside it.

# Validate token
user, error = User.validate_jwt(token_string)
```

`validate_jwt` branches on the token's `token_type`: ordinary session tokens,
`user_api_key` tokens, and `mcp` tokens — the resource-confined OAuth 2.1 access
tokens, which are accepted only on the request path their `aud` names and
require the `request` argument (see
[OAuth 2.1 Authorization Server](oauth_server.md)).

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
user.track()   # touch() + create/update UserDevice from active request
```

`touch()` is called automatically by `validate_jwt()` on every authenticated request — it updates `last_activity` via a targeted `UPDATE` (no full-model save, no row lock).

`track()` is **login-only**. Call it once at login time (via `jwt_login`) to create or update the `UserDevice` record for the current browser. Do not call it on every request — it performs a SELECT + conditional UPDATE on `account_userdevice` and will cause lock contention at scale.

```python
# Correct — called once at login
return jwt_login(request, user)   # jwt_login calls user.track() internally

# Incorrect — per-request device tracking causes lock contention
def on_my_endpoint(request):
    request.user.track()   # do NOT do this
```

**Device identity (`muid`) is write-once.** When a `UserDevice` record already has a `muid`, it is never overwritten by a new `_muid` cookie value. This preserves device identity across cookie resets.

## Group Membership

```python
groups = user.get_groups()                    # all groups
groups = user.get_groups(include_children=False)  # direct memberships only
groups_with_perm = user.get_groups_with_permission(["manage_users"])
```

## Password Change

### Self-service

Users change their own password by sending `new_password` and `current_password` to their own record:

```python
POST /api/user/<own_id>
{"new_password": "NewPass##123", "current_password": "OldPass##456"}
```

`current_password` is required for self-service changes.

### Admin password reset (for another user)

Admins with `manage_users` can set any user's password without knowing the current one:

```python
POST /api/user/<target_id>
{"new_password": "NewPass##123"}
```

No `current_password` needed. The `can_change_password()` method allows this for superusers and callers with `users` or `manage_users`. Password strength validation still applies.

## Password Reset Flow (Forgot Password)

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

The send endpoint returns **503** with a fixed safe-retry body when the email provider did not accept the message (no mailbox configured, provider refusal or outage) rather than a false success, and **400** when the account has no email address at all. Acceptance is classified by `mojo.apps.account.services.email_delivery.was_accepted()` — see [email/sending.md](../email/sending.md#knowing-whether-a-send-was-accepted). The token or code is still generated on the failure path; it is single-use and TTL-bounded, and the next request rotates it.

### Auto-Verify via Invite

When a user accepts an invite link (`POST /api/auth/password/reset/token`) and `last_login is None` (i.e. they have never logged in — this is their first access), `is_email_verified` is set automatically. The act of receiving and clicking the invite link is sufficient proof of email ownership.

Magic login (`POST /api/auth/magic/login`) also auto-verifies email on use, since the magic link itself proves inbox access.

## Phone Verification Flow

```
POST /api/auth/verify/phone/send     → sends 6-digit SMS code via phonehub
POST /api/auth/verify/phone/confirm  → user submits code, sets is_phone_verified=True
```

The phone number is normalized via `phonehub.normalize()` before the SMS is sent. An invalid or un-normalizable number returns a `ValueException` before any Twilio call is made.

The send endpoint returns **503** with a fixed safe-retry body when the SMS transport did not accept the message (misconfiguration, provider refusal or outage) rather than a false success, and **400** with fixed copy when the provider rejected the recipient number itself — Twilio's invalid-'To' (21211), blocked-recipient (21610) and non-SMS-capable (21614) codes, an allowlist; unknown codes fail toward the retryable 503. Acceptance is classified by `mojo.apps.account.services.sms_delivery` (`was_accepted()` / `recipient_rejected()`), the SMS twin of `email_delivery` above — with one deliberate deviation: no `provider_message_id` is required, because the mojo remote provider can accept a send without returning an id. The code is still generated on the failure path; it is single-use and TTL-bounded, and the next request rotates it.

Code TTL is configurable via `PHONE_VERIFY_CODE_TTL` (default 600 seconds). Codes are single-use and consumed on successful verification.

### Phone Change Flow

```
POST /api/auth/phone/change/request  → OTP to the NEW number, returns session_token
POST /api/auth/phone/change/confirm  → commits the new number, sets is_phone_verified=True
POST /api/auth/phone/change/cancel   → discards the pending change
```

The request endpoint reports its send with the same predicates as phone verification above: **503** with the fixed safe-retry body when `sms_delivery.was_accepted()` is False, **400** with fixed copy when `sms_delivery.recipient_rejected()` classifies the failure as the number itself. A transport exception is caught and answered as the 503 rather than escaping as a 500.

**Every failure path clears all four pieces of pending state** — `pending_phone`, `phone_change_otp`, `phone_change_otp_ts` and the `pc:` session-token JTI — through the shared `_clear_phone_change_state()` helper that `/cancel` also uses, so an outstanding `session_token` dies immediately rather than at TTL and the caller restarts cleanly at step 1. `phone_change_ts` is deliberately left alone, matching cancel's long-standing behavior.

The old number is notified only **after** the classification returns: a change that never started must never text the previous owner.

## Post-Save Actions

`POST_SAVE_ACTIONS = ['send_invite', 'disable', 'reactivate']`. The body key IS the action name. Each action requires `manage_users` (re-checked inside the handler).

| Action | Body example | Effect | Permission |
|---|---|---|---|
| `send_invite` | `{"send_invite": true}` | Sends an invite email | `manage_users` |
| `disable` | `{"disable": {"reason": "admin\|abuse", "note": "..."}}` | Flips `is_active=False`, writes `metadata.protected.disable.*`, emits incident event | `manage_users` |
| `reactivate` | `{"reactivate": {"note": "..."}}` | Flips `is_active=True`, appends to `disable.history` (FIFO cap 20) | `manage_users` |
| `change_username` | `{"change_username": {"username": "new"}}` | Self-service username change. Mirrors `POST /api/auth/username/change`. No `current_password` — see step-up auth. | self only |
| `revoke_sessions` | `{"revoke_sessions": {}}` | Self-service global logout — rotates `auth_key`. Mirrors `POST /api/auth/sessions/revoke`. No `current_password` — see step-up auth. NOTE: returns a status only, not a fresh JWT — caller must re-authenticate. | self only |
| `confirm_totp` | `{"confirm_totp": {"code": "123456"}}` | Self-service TOTP enrolment confirm. Mirrors `POST /api/account/totp/confirm`. Sets `requires_mfa=True` and returns recovery codes. | self only |
| `regenerate_totp_codes` | `{"regenerate_totp_codes": {"code": "123456"}}` | Self-service regenerate of recovery codes (requires valid TOTP code). Mirrors `POST /api/account/totp/recovery-codes/regenerate`. | self only |
| `disable_totp` | `{"disable_totp": true}` | Self-service TOTP disable. Mirrors `DELETE /api/account/totp`. | self only |

The full disable-lifecycle schema and service API are in [disable_lifecycle.md](disable_lifecycle.md).

`pii_anonymize()` records `reason="anonymized"` in the namespace before flipping the flag, preserving any prior cycle in `history`.

**"Self only"** — these actions are gated by standard model-save security (the record owner acting on self, or an admin with `manage_users`). No password is required — passwordless accounts must work too. Sensitive actions additionally call `_require_fresh_auth()` which raises HTTP 440 `reauth_required` when `FRESH_AUTH_WINDOW` is set and the caller's session is stale. See [step_up_auth.md](step_up_auth.md) for details. The dedicated `/api/auth/*` and `/api/account/totp/*` endpoints remain available for back-compat; new code should prefer the POST_SAVE_ACTIONS form.

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
| `ALLOW_USERNAME_CHANGE` | `True` | Feature flag — set `False` to disable the self-service username change endpoint |
| `ALLOW_SELF_DEACTIVATION` | `True` | Feature flag — set `False` to disable self-service account deactivation |
| `DEACTIVATE_TOKEN_TTL` | `900` | Account deactivation confirmation token TTL (seconds, 15 min default) |
| `ACCOUNT_CLOSURE_HANDLER` | `None` | Dotted path to a product callable that owns permanent account closure. Unset, the confirm endpoint anonymizes directly. See [disable_lifecycle.md](disable_lifecycle.md#account-closure-delegation-account_closure_handler) |
| `ALLOW_USER_REGISTRATION` | `False` | Enable built-in `POST /api/auth/register` endpoint |
| `REQUIRE_GROUP_ON_REGISTRATION` | `False` | Require `group_uuid` body param on register; rejects if missing or invalid |
| `REGISTRATION_EXTRA_FIELDS` | `[]` | Allowlist of extra body keys forwarded to `USER_REGISTERED_HANDLER` via `extra`; unrecognised keys are silently dropped |
| `PRE_REGISTER_VALIDATOR` | `None` | Dotted-path callable invoked before user creation; raise `ValueException` to reject |
| `USER_REGISTERED_HANDLER` | `None` | Dotted-path callable fired inside the registration transaction (and on OAuth new-user); raising rolls back |
| `USER_LOGIN_HANDLER` | `None` | Dotted-path callable fired on every successful `jwt_login()`; errors are swallowed |
| `FRESH_AUTH_WINDOW` | `0` | Step-up auth window in seconds. `0` (default) disables the gate entirely. When set, endpoints decorated with `@md.requires_fresh_auth()` require the caller's JWT `auth_time` to be within this many seconds of the request. See [step_up_auth.md](step_up_auth.md). |
| `FORCED_PASSWORD_TOKEN_TTL` | `600` | Lifetime in seconds of the single-use `tp:` credential issued after successful temporary-password authentication. |

## Administrator temporary passwords

`services/admin_passwords.py` is the only administrator issuance boundary. It
locks the selected User, generates and hashes a strong password, sets
`requires_password_change=True`, rotates `auth_key` in the same transaction,
disconnects realtime sessions, and audits only identifiers. The raw password
exists only in the returned service value and the authorized one-time Admin
dialog. Request and response body logging is replaced by a fixed marker.

A successful temporary-password login returns only a short-lived, single-use
`tp:` credential. It does not create an access token, refresh token, MFA token,
group token, login event, or normal login side effect. The client posts that
credential and a strong replacement to `POST /api/auth/password/forced`.
Completion locks the User, consumes the credential once, persists a permanent
password, clears the flag, and rotates `auth_key` again. Password-reset and
invite-password completion use the same permanent-password choke point.

The framework-hosted `/auth` page handles this response in place: it keeps the
`tp:` credential in page memory, presents the existing new-password form, and
calls `MojoAuth.completeForcedPassword()` before continuing the ordinary
post-login redirect. Custom clients must make the same explicit branch; a
forced-password response intentionally has no `access_token` to store.
