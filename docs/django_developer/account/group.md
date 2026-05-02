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
    SEARCH_FIELDS = ["name"]
```

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
        "no_disable": True,               # exempt from auto-disable sweep
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
exempted = group.get_protected_metadata("no_disable", default=False)

# Write a single key (saves immediately via update_fields=["metadata", "modified"])
group.set_protected_metadata("plan", "enterprise")
group.set_protected_metadata("no_disable", True)   # exempt from auto-disable sweep

# Delete a key by setting it to None
group.set_protected_metadata("disable_warned", None)
```

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
```

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

When a request includes `?group=<id>`, `MojoMiddleware` and auth decorators auto-populate `request.group` with the `Group` instance if the user is a member. All permission checks and list queries are then scoped to that group.

```python
# In a REST handler
if request.group:
    queryset = queryset.filter(group=request.group)
```

## Activity Tracking

```python
group.touch()   # updates last_activity (rate-limited by GROUP_LAST_ACTIVITY_FREQ)
```

## Settings

| Setting | Default | Description |
|---|---|---|
| `GROUP_LAST_ACTIVITY_FREQ` | `300` | Min seconds between activity updates |
