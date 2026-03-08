# MojoModel — Django Developer Reference

## What Is MojoModel

`MojoModel` is a mixin that makes any Django model instantly REST-capable. It provides automatic CRUD handling, permission enforcement, serialization, lifecycle hooks, logging, and incident reporting. It adds no database fields — only behavior.

## Inheritance Patterns

```python
# Standard model
from django.db import models
from mojo.models import MojoModel

class Book(models.Model, MojoModel):
    ...

# Model with encrypted secrets
from mojo.models import MojoSecrets, MojoModel

class Integration(MojoSecrets, MojoModel):
    ...
    # DO NOT also inherit models.Model — MojoSecrets already provides it
```

## Minimal Model Template

```python
from django.db import models
from mojo.models import MojoModel

class Book(models.Model, MojoModel):
    user = models.ForeignKey("account.User", null=True, on_delete=models.SET_NULL)
    group = models.ForeignKey("account.Group", null=True, on_delete=models.SET_NULL)
    title = models.CharField(max_length=255)
    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    class RestMeta:
        VIEW_PERMS = ["view_books", "owner"]
        SAVE_PERMS = ["manage_books", "owner"]
        CAN_DELETE = True
        SEARCH_FIELDS = ["title"]
        GRAPHS = {
            "list": {"fields": ["id", "title", "created"]},
            "default": {"fields": ["id", "title", "created", "modified"]},
        }
```

## RestMeta Reference

| Attribute | Type | Default | Description |
|---|---|---|---|
| `VIEW_PERMS` | list | `[]` | Permissions required to read |
| `SAVE_PERMS` | list | `[]` | Permissions required to create/update |
| `DELETE_PERMS` | list | `[]` | Permissions required to delete (falls back to SAVE_PERMS) |
| `CREATE_PERMS` | list | `[]` | Permissions required to create (falls back to SAVE_PERMS) |
| `CAN_DELETE` | bool | `False` | Must be `True` to allow DELETE requests |
| `CAN_CREATE` | bool | `True` | Set `False` to block POST creation |
| `CAN_BATCH` | bool | `False` | Allow batch create/update via `batched` param |
| `SEARCH_FIELDS` | list | all CharField/TextField | Fields searched by `?search=` param |
| `GRAPHS` | dict | `{}` | Serialization shapes (see [Graphs](graphs.md)) |
| `NO_SAVE_FIELDS` | list | `["id","pk","created","uuid"]` | Fields ignored on save |
| `NO_SHOW_FIELDS` | list | `[]` | Fields never included in responses |
| `LOG_CHANGES` | bool | `False` | Auto-log field changes via logit |
| `LOG_META_CHANGES` | bool | `False` | Auto-log key-level changes to all JSONFields via logit |
| `PROTECTED_JSON_PERMS` | list | `[]` | Permissions required to modify the `"protected"` root key in any JSONField |
| `OWNER_FIELD` | str | `"user"` | Field name for owner permission check |
| `GROUP_FIELD` | str | `"group"` | Field name for group scoping |
| `ALT_PK_FIELD` | str | `"uuid"` | Field used for non-integer PK lookups |
| `POST_SAVE_ACTIONS` | list | `["action"]` | Fields treated as post-save action triggers |
| `FORMATS` | dict | `None` | Field lists for download formats (CSV etc.) |

### Permission Values

- `"owner"` — grants access if `instance.user == request.user`
- `"all"` — public access, no authentication required
- Any string — must match a key in `user.permissions` or group permissions

## on_rest_request — The CRUD Entry Point

Route a URL to this class method to get full automatic CRUD:

```python
# rest/book.py
import mojo.decorators as md
from ..models.book import Book

@md.URL('book')
@md.URL('book/<int:pk>')
def on_book(request, pk=None):
    return Book.on_rest_request(request, pk)
```

Behavior by HTTP method and pk:

| Method | pk | Action |
|---|---|---|
| GET | None | List with filters/pagination |
| POST | None | Create new instance |
| GET | int | Retrieve single instance |
| POST/PUT | int | Update instance |
| DELETE | int | Delete instance (requires `CAN_DELETE=True`) |

## Permission Flow

`rest_check_permission` evaluates in this exact order:

1. If `"all"` in perms → allow unauthenticated
2. If user is not authenticated → deny + report incident
3. If instance provided and has `check_view_permission` or `check_edit_permission` → delegate
4. If `"owner"` in perms and `instance.user.id == request.user.id` → allow
5. If `request.group` set and model has `group` field → check group membership perms
6. Otherwise → check `request.user.has_permission(perms)`

Any denial is automatically reported to the incident system.

## Lifecycle Hooks

Override these on your model to inject custom logic:

```python
def on_rest_pre_save(self, changed_fields, created):
    # Called before save. changed_fields = dict of {field: old_value}
    # created = True if new instance
    if created:
        self.slug = slugify(self.title)

def on_rest_saved(self, changed_fields, created):
    # Called after save
    if "status" in changed_fields:
        notify_status_change(self)

def on_rest_created(self):
    # Called only on creation
    send_welcome_email(self)

def on_rest_pre_delete(self):
    # Called before deletion. Raise an exception to abort.
    if self.is_locked:
        raise ValueError("Cannot delete locked records")
```

## Save Field Customization

Define `set_<fieldname>` on the model to intercept field saves:

```python
def set_status(self, value):
    # Called automatically when 'status' key is in request.DATA
    if value not in ["active", "inactive"]:
        raise ValueError("Invalid status")
    self.status = value
```

## Batch Operations

Enable with `CAN_BATCH = True` in RestMeta. POST to the list endpoint with a `batched` array:

```json
{"batched": [{"title": "New"}, {"id": 5, "title": "Updated"}]}
```

Items with `id`/`pk` are updated; items without are created.

## Programmatic (Non-HTTP) Usage

```python
# Create from dict (service layer / management commands)
book = Book.create_from_dict({"title": "My Book"})

# Update from dict
book.update_from_dict({"title": "Updated Title"})

# Serialize
data = book.to_dict(graph="default")
data_list = Book.queryset_to_dict(Book.objects.all(), graph="list")
```

## Protected JSON Fields

Any `JSONField` on a MojoModel supports a reserved root key `"protected"`. Writes to `metadata["protected"]` (or any other JSONField's `"protected"` key) are blocked at the framework level unless the requesting user is a superuser or holds a permission listed in `PROTECTED_JSON_PERMS`.

### Setup

```python
class RestMeta:
    PROTECTED_JSON_PERMS = ["manage_groups"]  # who can write the "protected" key
    LOG_META_CHANGES = True                   # optional — audit all JSONField changes
```

### Behavior

- Any save attempt that includes `"protected"` in a JSONField value will raise a `403 PermissionDeniedException` if the user lacks the required permission.
- Changes to the `"protected"` key are **always** written to the audit log (`kind="meta:protected_changed"`) regardless of `LOG_CHANGES` or `LOG_META_CHANGES` — it is an unconditional security audit trail.
- When `LOG_META_CHANGES = True`, all root-level key changes to any JSONField are logged (`kind="meta:changed"`).

### Example — storing protected config

```python
# Only superusers or users with "manage_groups" can set this via the API
group.metadata = {
    "timezone": "America/New_York",      # normal — any editor can change
    "protected": {
        "stripe_account_id": "acct_123", # guarded — requires PROTECTED_JSON_PERMS
        "webhook_secret": "whsec_abc",
    }
}
```

### Audit log entries

| `kind` | When fired | Contents |
|---|---|---|
| `meta:protected_changed` | Any successful write to `"protected"` | Username, field name, changed keys, pk |
| `meta:changed` | Any JSONField key change (requires `LOG_META_CHANGES=True`) | Username, field name, changed keys, pk |

### Programmatic bypass

When calling `on_rest_update_jsonfield` directly outside a request context (e.g. from a management command or service), pass `request=None`. In that case `_can_edit_protected_json` returns `False`, so protected writes will still raise. Pass a `SYSTEM_REQUEST` or a real request with a superuser if the write is intentional.

## Logging

```python
# Instance-level log (writes to logit.Log model)
self.log(log="Something happened", kind="book:event")

# Class-level log
Book.class_logit(request, "bulk action", kind="book:bulk", model_id=0)
```

## Incident Reporting

```python
# Instance-level
self.report_incident("Suspicious edit attempt", event_type="security_alert", level=2)

# Class-level
Book.class_report_incident("Unauthorized list attempt", event_type="permission_denied", request=request)
```

## MojoSecrets

For models requiring encrypted storage:

```python
from mojo.models import MojoSecrets, MojoModel

class Integration(MojoSecrets, MojoModel):
    name = models.CharField(max_length=255)
    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    class RestMeta:
        NO_SHOW_FIELDS = ["mojo_secrets"]  # never expose in API
        GRAPHS = {"default": {"fields": ["id", "name"]}}
```

```python
# Set/get secrets (encrypted in DB as single JSON field)
integration.set_secret("api_key", "sk-abc123")
key = integration.get_secret("api_key", default=None)

# Access all secrets as objict
secrets = integration.secrets
```

Encryption is AES-based, keyed per-instance using the record's `created` timestamp and class name. Never create individual encrypted fields — always use the secrets system.

## Key Properties

| Property | Description |
|---|---|
| `self.active_request` | Current HTTP request (via ContextVar) |
| `self.active_user` | Current authenticated user |

## Settings

| Setting | Default | Description |
|---|---|---|
| `MOJO_APP_STATUS_200_ON_ERROR` | `False` | Return HTTP 200 even on errors (for legacy clients) |
| `MOJO_REST_LIST_PERM_DENY` | `True` | Return 403 (vs empty list) when list perm denied |
