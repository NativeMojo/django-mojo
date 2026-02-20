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
    VIEW_PERMS = ["view_groups", "manage_groups", "manage_group"]
    SAVE_PERMS = ["manage_groups", "manage_group"]
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
