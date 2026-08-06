# Metrics Permissions — Django Developer Reference

Metrics uses a custom permission system (not RestMeta) because metrics are stored in Redis, not Django models. Permissions are checked per-account at the REST layer.

## Account Types

Every metrics operation targets an **account**. The account determines which permission rules apply:

| Account | Format | Example | Who can access |
|---------|--------|---------|---------------|
| `public` | literal | `public` | **Read**: anyone (no auth). **Write**: `write_metrics`/`metrics`, unless explicitly opted into anonymous writes (see below) |
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

Same structure, but checks `["write_metrics", "metrics"]` instead of
`["view_metrics", "metrics"]` — **except for `public`**, which is NOT an
implicit allow on the write side:

```
check_write_permissions(request, "public"):
    per-account write perms == "public"?   → allowed (explicit opt-in)
    per-account write perms set?           → user must have that permission
    otherwise                              → user must have "write_metrics"/"metrics"
```

### Why public writes are gated

Every distinct slug recorded becomes a permanent member of the account's slug
registry (`mets:<account>:slugs`) plus never-expiring month/year counter keys.
An anonymous writer could therefore grow Redis without bound and pollute every
category listing. Deployments that genuinely want anonymous counters opt in
explicitly:

```python
from mojo.apps import metrics
metrics.set_write_perms("public", "public")   # restore anonymous public writes
```

Prefer an app-specific collect endpoint that records server-side with bounded
slug names over opening raw anonymous writes.

## Category Permission: `metrics`

The `metrics` category permission grants full read+write access to the standard
global, group, and user account types. It is the recommended permission for
administrators of those metrics. A custom account's configured permission
remains authoritative: `metrics` does not bypass an unrelated custom permission
string. A custom account is readable only when its policy is `public` or the
caller satisfies the configured permission.

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

## Discovery Permission Order

`GET /api/metrics/discover` dispatches among `accounts`, `categories`, and
`slugs` without introducing a second permission system.

Account enumeration first validates the complete query grammar, then checks
the existing `global` account view gate. Only after that does it check the
maintained account-index cardinality and materialize candidates. Every
candidate is passed through `check_view_permissions()` independently. An
expected `PermissionDeniedException` omits that name; Redis, database, or
programming errors abort the request. Therefore hidden candidates contribute
neither rows nor `count`, and a partial backend failure can never look like a
complete catalog.

Category and slug discovery validate the grammar and reserved account syntax,
then complete the explicit account view check before reading any Redis
category/slug registry. This preserves public, global, group, user, custom,
reference-key, override-key, and group-token behavior. It also means a global
`view_metrics` holder still cannot read a custom account whose configured
permission they do not hold. The endpoint performs no separate Group/User
existence query; syntactically valid missing group accounts keep the existing
identity-specific helper behavior.

The account catalog is authenticated global Admin discovery. Direct
category/slug discovery remains anonymous for `public` and custom accounts
whose view policy is `public`.

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
| `/api/metrics/discover?resource=accounts` | GET | `check_view_permissions(request, "global")`, then each candidate's view check |
| `/api/metrics/discover?resource=categories|slugs` | GET | `check_view_permissions(request, account)` before registry access |
| `/api/metrics/permissions` | GET/POST/DELETE | `@md.requires_global_perms("manage_incidents", "metrics", "manage_metrics")` |

Unlike the account-based checks above, `/api/metrics/permissions` (which
manages the per-account config every other endpoint reads) is gated with
`@md.requires_global_perms` — the caller's **global** `User.permissions` (or
superuser) only, no group/member fallback. See
[Global vs Group-Scoped Permission Checks](../core/permissions.md#global-vs-group-scoped-permission-checks).
