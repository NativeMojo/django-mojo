# Group Model — Django Developer Reference

## Overview

`Group` is the framework's multi-tenancy and organization model. Users belong to groups via `GroupMember`. Groups can form hierarchies via the `parent` FK.

## Inheritance

```python
class Group(MojoSecrets, MojoModel):
```

## Key Fields

| Field | Type | Description |
|---|---|---|
| `name` | CharField | Group name |
| `kind` | CharField | Type: `"group"`, `"organization"`, custom |
| `parent` | FK → Group (self) | Parent group for hierarchy |
| `is_active` | BooleanField | Active flag |
| `uuid` | CharField | Unique identifier |
| `metadata` | JSONField | Arbitrary group metadata (includes `timezone`, `short_name`) |
| `avatar` | FK → fileman.File | Group image |
| `last_activity` | DateTimeField | Last group activity |
| `auth_domain` | CharField (nullable, unique) | Custom hostname for white-label auth pages (e.g. `auth.yourproject.com`) |

## White-Label Auth Domain

`auth_domain` enables per-group white-label auth pages. When set, the bouncer
resolves the group from the request hostname and applies that group's
`AUTH_*` settings (logo, branding, OAuth state, success redirect, etc.).

```python
# Assign a white-label auth domain to a group
group.auth_domain = 'auth.clientbrand.com'
group.save()
```

The mapping is cached in Redis (`auth_domain:<hostname>` key, 24h TTL). The
cache is automatically invalidated when `auth_domain` or `is_active` changes.

### Resolving a group by hostname

```python
group = Group.resolve_by_auth_domain('auth.clientbrand.com')
# Returns the active Group with that auth_domain, or None.
# Result is Redis-cached (24h for hits, 1h for misses).
```

The bouncer calls this automatically on every page request — no setup beyond
setting `auth_domain` is required.

### Per-group AUTH_* settings

All `AUTH_*` settings resolve per-group when a group is detected. Set them
via the `Setting` model with the `group` argument:

```python
from mojo.helpers import settings
from mojo.apps.account.models import Group

group = Group.objects.get(uuid='...')

settings.set('AUTH_APP_TITLE', 'Client Brand', group=group)
settings.set('AUTH_LOGO_URL', 'https://cdn.client.com/logo.svg', group=group)
settings.set('AUTH_SUCCESS_REDIRECT', '/client-dashboard/', group=group)
settings.set('AUTH_ENABLE_GOOGLE', True, group=group)
```

Settings resolve with parent-chain fallback: group → parent group → global.

### Challenge page branding (opt-in)

By default, the bouncer challenge page always uses the default branding. To
override it for a specific group:

```python
settings.set('BOUNCER_CHALLENGE_LOGO_URL', 'https://cdn.client.com/logo.svg', group=group)
settings.set('BOUNCER_CHALLENGE_BRAND', 'CLIENT BRAND', group=group)
```

These settings are intentionally not global — they only take effect when a
group is detected. This preserves the default branding for the default flow.

## RestMeta

```python
class RestMeta:
    LOG_CHANGES = True
    LOG_META_CHANGES = True          # logs key-level changes to metadata
    VIEW_PERMS = ["view_groups", "manage_groups", "manage_group"]
    SAVE_PERMS = ["manage_groups", "manage_group"]
    PROTECTED_JSON_PERMS = ["manage_groups"]  # required to write metadata["protected"]
    POST_SAVE_ACTIONS = ['realtime_message', 'disable', 'reactivate']
    SEARCH_FIELDS = ["name"]
```

### Post-Save Actions

| Action | Body example | Effect |
|---|---|---|
| `realtime_message` | `{"realtime_message": {"topic": "group:<id>:...", "message": ...}}` | Publishes to a group-scoped realtime topic |
| `disable` | `{"disable": {"reason": "admin\|abuse\|archived", "note": "..."}}` | Flips `is_active=False`, writes `metadata.protected.disable.*` |
| `reactivate` | `{"reactivate": {"note": "..."}}` | Flips `is_active=True`, appends to `disable.history` (FIFO cap 20) |

`disable`/`reactivate` require `manage_groups` (stricter than the broader Group `SAVE_PERMS` since these are destructive). See [disable_lifecycle.md](disable_lifecycle.md).

## Hierarchy

```python
group.parent          # immediate parent
group.groups          # direct children (related_name)
group.get_children()  # all descendants (if implemented)
group.get_parents()   # all ancestors (if implemented)
```

`top_most_parent` returns the root ancestor.

## Metadata & Timezone

```python
tz = group.timezone                    # from group.metadata["timezone"]
short = group.short_name               # from group.metadata["short_name"]
local_day = group.get_local_day()      # (start, end) in UTC for today in group's timezone
local_time = group.get_local_time(utc_dt)  # convert UTC to group's local time
```

Store arbitrary config in `metadata`:

```python
group.metadata["max_users"] = 50
group.metadata["feature_flags"] = {"new_ui": True}
group.save()
```

### Protected Metadata

The reserved root key `"protected"` in `metadata` is write-protected at the framework level. Only a superuser or a user with a permission listed in `PROTECTED_JSON_PERMS` (e.g. `"manage_groups"`) can set or update it via the REST API. Any attempt by an unprivileged user raises a `403 PermissionDeniedException`.

Use it to store config that group editors should be able to read but never overwrite:

```python
group.metadata = {
    "timezone": "America/New_York",       # any group editor can change
    "theme": "dark",                       # any group editor can change
    "protected": {
        "stripe_account_id": "acct_123",  # requires manage_groups
        "webhook_secret": "whsec_abc",    # requires manage_groups
        "plan": "enterprise",
        "disable": {                      # see disable_lifecycle.md
            "exempt_from_auto_disable": True,
        },
    }
}
```

**Audit trail:** Every successful write to `metadata["protected"]` is unconditionally logged with `kind="meta:protected_changed"`, recording the username, changed keys, and instance pk — regardless of the `LOG_CHANGES` or `LOG_META_CHANGES` settings.

When `LOG_META_CHANGES = True`, all root-level key changes across the entire `metadata` field are also logged with `kind="meta:changed"`.

See [MojoModel — Protected JSON Fields](../core/mojo_model.md#protected-json-fields) for full framework details.

### Python Helpers for Protected Metadata

`Group` provides helpers to read and write individual keys within `metadata["protected"]` without loading and re-saving the whole metadata dict:

```python
# Read a single key from metadata["protected"]
plan = group.get_protected_metadata("plan", default=None)

# Write a single key (saves immediately via update_fields=["metadata", "modified"])
group.set_protected_metadata("plan", "enterprise")

# Delete a key by setting it to None
group.set_protected_metadata("plan", None)
```

For disable lifecycle state (`metadata.protected.disable.*`) use the
[disable service](disable_lifecycle.md) — `disable_entity`, `reactivate_entity`,
`mark_warning`, etc. — instead of writing the namespace directly.

`set_protected_metadata` always persists immediately. It does not bypass the REST write-protection — that gate applies only to REST requests. Direct Python calls are unrestricted.

## Membership

```python
from mojo.apps.account.models import GroupMember

# Get a user's membership in a group
member = group.get_member_for_user(user, check_parents=True)

# Check permission within group context
has_perm = group.user_has_permission(user, ["manage_members"])

# Invite a user by email
member = group.invite("alice@example.com")

# Active direct member count (excludes inactive memberships and descendants)
n = group.member_count
```

`member_count` is exposed via REST as an `extra` on the `default` graph (not `basic` — that stays minimal). List endpoints use `default` by, well, default, so the field appears in list payloads without an explicit `?graph=` parameter. Backed by `members.filter(is_active=True).count()` per record — at very large scale, consider an annotated queryset instead.

> **Non-`User` identities (e.g. API keys).** `get_member_for_user` returns `None`
> for any identity that is not a request `User`. An `ApiKey` authenticating a
> request (`Authorization: apikey <token>`) has no `GroupMember` row, so it is
> treated as "not a member" rather than raising `Must be "User" instance.` from
> `members.filter(user=…)`. `user_has_permission` likewise returns a bool for an
> API key: it grants/denies via `ApiKey.has_permission` (group-scoped; `sys.*`
> always denied) and never runs a `User`-typed membership query. Every group
> permission gate is therefore safe to call with `request.user` whether the
> caller authenticated with a JWT or an API key.
>
> **`check_view_permission` confines a key to its own group.** The gate behind
> `GET /api/group/<pk>` special-cases an ApiKey identity directly — it returns
> `api_key.is_group_allowed(self)` before any of the fallbacks above run, so a
> key can never read another tenant's group by pk (list is confined the same
> way via `ApiKey.get_groups`). It does **not** get the "any member sees a
> basic-graph view" fallback that applies to a real `User`. See
> [API Keys — Group Scoping](api_keys.md#group-scoping).

## GroupMember Model

```python
class GroupMember(models.Model, MojoModel):
    group = models.ForeignKey(Group, related_name="members", ...)
    user = models.ForeignKey(User, related_name="memberships", ...)
    permissions = models.JSONField(default=dict)
    is_active = models.BooleanField(default=True)
```

Member-level permissions override group-level defaults:

```python
member.has_permission("manage_content")
member.permissions["manage_content"] = True
member.save()
```

## request.group

When a request includes `?group=<id>`, `MojoMiddleware` and auth decorators auto-populate `request.group` with the `Group` instance if the user is a member. Only **active** groups resolve (`Group.get_active`) — an inactive group's id behaves exactly like a nonexistent one (`request.group` stays `None`, no touch side effect), for both the `group=` and `group_uuid=` params. All permission checks and list queries are then scoped to that group — **except** endpoints gated with `@md.requires_global_perms`, which never consult `request.group` at all (global `User.permissions` or superuser only). See [Global vs Group-Scoped Permission Checks](../core/permissions.md#global-vs-group-scoped-permission-checks).

```python
# In a REST handler
if request.group:
    queryset = queryset.filter(group=request.group)
```

## Webhook Secret

`Group` stores a per-tenant HMAC-SHA256 signing secret inside its `MojoSecrets` blob (no migration). Three methods manage it:

```python
# Read — safe for verify paths; returns None if not yet minted
secret = group.get_webhook_secret()                  # auto_create=False

# Read with auto-mint — use on the emitter side
secret = group.get_webhook_secret(auto_create=True)

# Full record with timestamps
info = group.get_webhook_secret_info(auto_create=True)
# objict(value="wsec_…", created_at="…", last_rotated_at="…")

# Rotate — new value, preserves created_at, advances last_rotated_at
info = group.rotate_webhook_secret()
```

The secret format is `"wsec_"` + 48 alphanumeric characters. The REST endpoint `POST /api/group/webhook_secret` exposes read and rotation to operators with `manage_group` permission. See [Webhook Signing](webhook_signing.md) for the full signing and verification pattern.

Each Group's active webhook delivery targets are managed through the `WebhookSubscription` model (`group.webhook_subscriptions` related name). See [Webhook Subscriptions](webhook_subscriptions.md) for the subscription registry and fan-out dispatcher.

## Activity Tracking

```python
group.touch()   # updates last_activity (rate-limited by GROUP_LAST_ACTIVITY_FREQ)
```

## Settings

| Setting | Default | Description |
|---|---|---|
| `GROUP_LAST_ACTIVITY_FREQ` | `300` | Min seconds between activity updates |
