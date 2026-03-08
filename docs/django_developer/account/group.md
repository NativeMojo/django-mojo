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
    }
}
```

**Audit trail:** Every successful write to `metadata["protected"]` is unconditionally logged with `kind="meta:protected_changed"`, recording the username, changed keys, and instance pk — regardless of the `LOG_CHANGES` or `LOG_META_CHANGES` settings.

When `LOG_META_CHANGES = True`, all root-level key changes across the entire `metadata` field are also logged with `kind="meta:changed"`.

See [MojoModel — Protected JSON Fields](../core/mojo_model.md#protected-json-fields) for full framework details.

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
