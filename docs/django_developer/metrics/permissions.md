# Metrics Permissions — Django Developer Reference

Metrics uses a custom permission system (not RestMeta) because metrics are stored in Redis, not Django models. Permissions are checked per-account at the REST layer.

## Account Types

Every metrics operation targets an **account**. The account determines which permission rules apply:

| Account | Format | Example | Who can access |
|---------|--------|---------|---------------|
| `public` | literal | `public` | Anyone (no auth required) |
| `global` | literal | `global` | Users with `view_metrics` / `write_metrics` / `metrics` permission |
| Group | `group-<id>` | `group-42` | Group members with the relevant permission, or users with system-level permission |
| User | `user-<id>` | `user-7` | The user themselves, or users with system-level permission |
| Custom | any string | `my-app` | Controlled by per-account permission config stored in Redis |

## Permission Levels

Two independent permission types exist per account:

- **View permissions** — who can `GET` (fetch) metrics data
- **Write permissions** — who can `POST` (record) metrics data

## How Permission Checks Work

All metrics REST endpoints call `check_view_permissions()` or `check_write_permissions()` from `mojo/apps/metrics/rest/helpers.py`.

### View Permission Flow

```
check_view_permissions(request, account):
    account == "global"?
        → user must have "view_metrics" or "metrics" permission
    account starts with "group-"?
        → user must have "view_metrics" or "metrics" at system level
          OR group-level "view_metrics"/"metrics" permission
    account starts with "user-"?
        → user must have "view_metrics" or "metrics" at system level
          OR be the user whose ID matches the account
    account == "public"?
        → allowed (no auth)
    otherwise (custom account)?
        → look up per-account view perms from Redis
        → if "public", allowed
        → if set, user must have that permission
        → if not set, denied
```

### Write Permission Flow

Same structure, but checks `["write_metrics", "metrics"]` instead of `["view_metrics", "metrics"]`.

## Category Permission: `metrics`

The `metrics` category permission grants full read+write access to all metrics across all account types (global, group, user). This is the recommended permission for admin users who need full metrics access.

Fine-grained alternatives:

| Permission | Grants |
|-----------|--------|
| `view_metrics` | Read metrics data from any account |
| `write_metrics` | Record metrics data to any account |
| `metrics` | Both read and write (category permission) |
| `manage_metrics` | Manage per-account permission configuration |

## Per-Account Permission Configuration

Custom accounts can have their own view/write permissions configured via the admin API or Python:

### Python API

```python
from mojo.apps import metrics

# Set who can view metrics for the "my-app" account
metrics.set_view_perms("my-app", "view_my_app_metrics")

# Set who can write metrics for the "my-app" account
metrics.set_write_perms("my-app", "manage_my_app")

# Make an account's metrics publicly viewable
metrics.set_view_perms("my-app", "public")

# Read current permissions
view = metrics.get_view_perms("my-app")   # returns string or None
write = metrics.get_write_perms("my-app")  # returns string or None

# List all accounts that have permissions configured
accounts = metrics.list_accounts()

# Remove permissions (denies all access)
metrics.set_view_perms("my-app", None)
metrics.set_write_perms("my-app", None)
```

### REST API

The permissions endpoint requires `manage_incidents`, `metrics`, or `manage_metrics` permission.

**List all accounts with permissions:**

```
GET /api/metrics/permissions
```

```json
{
  "data": [
    {
      "account": "my-app",
      "view_permissions": "view_my_app",
      "write_permissions": "manage_my_app"
    }
  ],
  "count": 1,
  "status": true
}
```

**Get permissions for a specific account:**

```
GET /api/metrics/permissions/<account>
```

**Set permissions:**

```
POST /api/metrics/permissions/<account>
```

```json
{
  "view_permissions": "public",
  "write_permissions": "manage_my_app"
}
```

Permission values are comma-separated strings. Use `"public"` to allow unauthenticated access.

**Remove all permissions for an account:**

```
DELETE /api/metrics/permissions/<account>
```

## Group-Scoped Permissions

When the account is `group-<id>`, the system checks permissions at two levels:

1. **System-level**: Does the user have `view_metrics` or `metrics` globally?
2. **Group-level**: Does the user have `view_metrics` or `metrics` within the group's membership permissions?

Either check passing grants access. If neither passes, a `PermissionDeniedException` is raised.

## User-Scoped Permissions

When the account is `user-<id>`:

1. **System-level**: Does the user have `view_metrics` or `metrics` globally?
2. **Identity check**: Is the requesting user the same as the user ID in the account?

Users can always see their own metrics. Admins with `metrics` can see anyone's.

## REST Endpoints and Their Permission Checks

| Endpoint | Method | Permission Check |
|----------|--------|-----------------|
| `/api/metrics/record` | POST | `check_write_permissions(request, account)` |
| `/api/metrics/fetch` | GET | `check_view_permissions(request, account)` |
| `/api/metrics/series` | GET/POST | `check_view_permissions(request, account)` |
| `/api/metrics/value/get` | GET | `check_view_permissions(request, account)` |
| `/api/metrics/value/set` | POST | `check_write_permissions(request, account)` |
| `/api/metrics/categories` | GET | `check_view_permissions(request, account)` |
| `/api/metrics/category_slugs` | GET | `check_view_permissions(request, account)` |
| `/api/metrics/category_delete` | DELETE | `check_write_permissions(request, account)` |
| `/api/metrics/permissions` | GET/POST/DELETE | `@md.requires_perms("manage_incidents", "metrics", "manage_metrics")` |
