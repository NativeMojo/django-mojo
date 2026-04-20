# logit App — Django Developer Reference

The `logit` app provides a database-backed structured logging model that complements the file-based `logit` helper. Every REST action, model change, and security event can write a structured record to the `Log` table.

## Log Model Fields

| Field | Type | Description |
|---|---|---|
| `created` | DateTimeField | Timestamp |
| `level` | CharField | `info`, `warn`, `error`, `debug` |
| `kind` | CharField | Dot-notation category (e.g., `model:changed`, `auth:login`) |
| `method` | CharField | HTTP method of active request |
| `path` | TextField | URL path of active request |
| `payload` | JSONField | Arbitrary structured data |
| `ip` | CharField | Client IP from active request |
| `duid` | CharField | Device unique ID |
| `uid` | IntegerField | User ID |
| `gid` | IntegerField | Group ID (auto-populated from model or request context) |
| `username` | CharField | Username |
| `user_agent` | TextField | User agent string |
| `log` | TextField | Human-readable message |
| `model_name` | CharField | `app_label.ModelName` |
| `model_id` | IntegerField | PK of the related model instance |

## Group ID Auto-Population

`gid` is automatically resolved — you rarely need to pass it manually:

- When calling `self.log()` on a model instance, `gid` is set from `self.group_id` if the model has a `group` FK.
- When calling `Log.logit(request, ...)` directly, `gid` is set from `request.group.id` if the active request has a group context.
- You can always override by passing `gid=<value>` explicitly.

Both `gid` and `(gid, kind)` are indexed for efficient per-group queries.

## Writing Logs

### From MojoModel instances

```python
# Instance log (model_name and model_id auto-populated)
self.log(log="Order processed", kind="order:processed")
self.log(log="Status changed", kind="order:status", level="info")

# Class-level log
Book.class_logit(request, "Bulk export triggered", kind="book:export", model_id=0)
```

### Direct Log creation

```python
from mojo.apps.logit.models import Log

Log.logit(request, "Custom event", kind="custom:event", model_name="myapp.Book", model_id=5)
```

### Automatic LOG_CHANGES

Enable automatic field-change logging in RestMeta:

```python
class RestMeta:
    LOG_CHANGES = True
```

When `True`, any REST save that modifies fields automatically writes a `model:changed` log entry with a diff of changed values (passwords and keys are masked).

## Log Levels

| Level | When to Use |
|---|---|
| `info` | Normal operations, state changes |
| `warn` | Unexpected but non-critical events |
| `error` | Failures requiring investigation |
| `debug` | Development/diagnostic data (use sparingly) |

## Querying Logs

```python
from mojo.apps.logit.models import Log

# By model
logs = Log.objects.filter(model_name="myapp.Book", model_id=5)

# By kind prefix
logs = Log.objects.filter(kind__startswith="order:")

# By user
logs = Log.objects.filter(uid=42, level="error")

# By group (uses composite index)
logs = Log.objects.filter(gid=7)
logs = Log.objects.filter(gid=7, kind__startswith="order:")

# Recent errors
from mojo.helpers import dates
logs = Log.objects.filter(
    level="error",
    created__gte=dates.utcnow() - datetime.timedelta(hours=24)
).order_by("-created")
```

## RestMeta

```python
class RestMeta:
    VIEW_PERMS = ["view_logs", "manage_users"]
    GRAPHS = {
        "basic": {"fields": ["id", "created", "level", "kind", "log", "uid", "gid", "username"]},
        "default": {"fields": ["id", "created", "level", "kind", "log", "uid", "gid", "username",
                               "model_name", "model_id", "path", "method", "ip"]},
    }
```

## Best Practices

- Use dot-notation `kind` values: `"app:entity:action"` (e.g., `"order:payment:failed"`)
- Always pass `kind` — it's the primary way to filter logs
- Use `self.log()` on models; use `logit.info()` for service/helper layer messages
- `LOG_CHANGES = True` provides automatic audit trails with no extra code
- Sensitive fields are automatically masked in change diffs — the full key list is `SENSITIVE_KEYS` in `mojo/helpers/logit.py` (single source of truth, see `docs/django_developer/helpers/logit.md`)
- The `payload` field is automatically sanitized before storage via `sanitize_dict()`, which shares the same `SENSITIVE_KEYS` list — plaintext credentials are never written to the Log table
