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
| `username` | CharField | Username |
| `user_agent` | TextField | User agent string |
| `log` | TextField | Human-readable message |
| `model_name` | CharField | `app_label.ModelName` |
| `model_id` | IntegerField | PK of the related model instance |

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
        "basic": {"fields": ["id", "created", "level", "kind", "log", "uid", "username"]},
        "default": {"fields": ["id", "created", "level", "kind", "log", "uid", "username",
                               "model_name", "model_id", "path", "method", "ip"]},
    }
```

## Best Practices

- Use dot-notation `kind` values: `"app:entity:action"` (e.g., `"order:payment:failed"`)
- Always pass `kind` — it's the primary way to filter logs
- Use `self.log()` on models; use `logit.info()` for service/helper layer messages
- `LOG_CHANGES = True` provides automatic audit trails with no extra code
- Sensitive fields (`password`, `key`, `secret`, `token`) are automatically masked in change diffs
