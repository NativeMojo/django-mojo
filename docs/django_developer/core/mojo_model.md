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
| `CAN_UPDATE` | bool | `True` | Set `False` to block PUT/POST against an existing instance (deprecated alias: `CAN_SAVE`) |
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
| `CREATED_BY_OWNER_FIELD` | str or None | `"user"` | Auto-stamped with `request.user` on create **only when the body omits it**. `None` disables. See [REST Permissions — Create-time owner stamping](../rest/permissions.md#create-time-owner-stamping). |
| `UPDATED_BY_OWNER_FIELD` | str or None | `"modified_by"` | Always set to `request.user` on update (who last modified). See [REST Permissions](../rest/permissions.md). |
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

### List response

`GET /api/myapp/book` returns a paginated envelope:

```json
{
  "status": true,
  "count": 42,
  "start": 0,
  "size": 10,
  "data": [
    {"id": 1, "title": "Book One", "created": "2024-01-15T10:30:00Z"},
    {"id": 2, "title": "Book Two", "created": "2024-01-16T08:00:00Z"}
  ]
}
```

Pagination request params (all optional):

| Param | Alias | Default | Description |
|---|---|---|---|
| `size` | `limit` | `10` | Number of items per page |
| `start` | `offset` | `0` | Starting index |
| `graph` | | `"list"` | Which `GRAPHS` shape to use |

```
GET /api/myapp/book?start=20&size=10&graph=list
```

### Single object response

`GET /api/myapp/book/1` returns:

```json
{
  "status": true,
  "data": {
    "id": 1,
    "title": "Book One",
    "created": "2024-01-15T10:30:00Z",
    "modified": "2024-01-15T10:30:00Z"
  }
}
```

### Create / update response

`POST /api/myapp/book` (create) and `POST /api/myapp/book/1` (update) both return the serialized instance:

```json
{
  "status": true,
  "data": {
    "id": 1,
    "title": "Updated Title",
    "created": "2024-01-15T10:30:00Z",
    "modified": "2024-01-20T09:00:00Z"
  }
}
```

### Error response

```json
{
  "status": false,
  "code": 403,
  "error": "GET permission denied: Book"
}
```

## Return Values — Always Plain Dicts

**Never import or use `JsonResponse` in a view function.** Return a plain dict or list — the framework wraps it automatically.

| Return value | What the client receives |
|---|---|
| `{"id": 1, "name": "Joe"}` | `{"status": true, "code": 200, "data": {"id": 1, "name": "Joe"}}` |
| `[{"id": 1}, {"id": 2}]` | `{"status": true, "code": 200, "data": [...], "size": 2}` |
| `{"status": False, "error": "not found"}` | passed through as-is |
| `{"status": True, "data": {...}}` | passed through as-is |
| `raise ValueError("bad input")` | `{"status": false, "error": "bad input", "code": 400}` |
| `raise PermissionError("denied")` | `{"status": false, "error": "denied", "code": 403}` |

```python
# Return just the data — framework wraps it
@md.GET('book/stats')
@md.requires_auth
def on_book_stats(request):
    return {"total": Book.objects.count()}
    # client gets: {"status": true, "data": {"total": 42}}

# Return an explicit error envelope
@md.GET('book/<int:pk>')
def on_book(request, pk=None):
    book = Book.objects.filter(pk=pk).first()
    if not book:
        return {"status": False, "error": "Book not found"}
    return Book.on_rest_request(request, pk)

# Raise for errors — auto-converted to 400/403
@md.POST('book/publish')
@md.requires_auth
def on_publish(request):
    book = Book.objects.get(pk=request.DATA.book_id)
    if not request.user.has_permission("publish_books"):
        raise PermissionError("Publish permission required")
    book.publish()
    return {"status": True}
```

`JsonResponse` is only for middleware and custom decorators — never inside a routed view function.

## Permission Flow

`rest_check_permission` is a **pure boolean predicate** — it evaluates permissions and returns `True` or `False` with no side effects (no event emission):

1. If `"all"` in perms → allow unauthenticated
2. If user is not authenticated → return False
3. If instance provided and has `check_view_permission` or `check_edit_permission` → delegate
4. If `"owner"` in perms and `instance.user.id == request.user.id` → allow
5. If `request.group` set and model has `group` field → check group membership perms
6. Otherwise → check `request.user.has_permission(perms)`

Use `rest_check_permission` directly when you need a boolean test with no action on denial (for example, list fallbacks that return a scoped result instead of a 403).

### `rest_check_permission_or_raise`

For framework handlers that must respond with 401 or 403 on denial, use `rest_check_permission_or_raise(request, permission_keys, instance=None)`. It runs the same evaluation logic and raises `PermissionDeniedException` on any False branch, carrying structured metadata for the dispatcher:

```python
# raises PermissionDeniedException on denial — never returns False
MyModel.rest_check_permission_or_raise(request, MyModel.RestMeta.VIEW_PERMS, instance)
```

### `PermissionDeniedException` metadata

`PermissionDeniedException` carries optional kwargs that the REST dispatcher reads to build the incident:

| Kwarg | Description |
|---|---|
| `branch` | Which check failed (e.g., `user.has_permission`, `instance.check_view_permission`) |
| `perms` | Resolved permission list |
| `permission_keys` | Raw `VIEW_PERMS` / `SAVE_PERMS` keys |
| `model_name` | Model class name |
| `instance` | The specific instance that denied (if any) |
| `event_type` | Incident category string (default: `"user_permission_denied"`) |
| `status` | HTTP response code (default 403; pass 401 for unauthenticated) |

The REST dispatcher in `mojo/decorators/http.py` is the **single emission site** for all denial events. It catches `PermissionDeniedException`, builds the incident from the exception's metadata, and honors `MOJO_APP_STATUS_200_ON_ERROR` uniformly. Application code should raise instead of calling `class_report_incident` manually on denial paths.

### Event categories emitted

| Category | HTTP | Trigger |
|---|---|---|
| `unauthenticated` | 401 | Unauth request hit a perm-gated endpoint |
| `user_permission_denied` | 403 | User lacks system-level perms |
| `view_permission_denied` | 403 | `instance.check_view_permission` rejected |
| `edit_permission_denied` | 403 | `instance.check_edit_permission` rejected |
| `group_member_permission_denied` | 403 | Group-scoped perm check failed |
| `feature_disabled` | 403 | `CAN_UPDATE/CAN_DELETE/CAN_CREATE/CAN_BATCH = False` |
| `fk_attach_denied` | n/a | FK save silently skipped — field unchanged, no HTTP error |

**Recovery paths emit no events.** When a list endpoint returns HTTP 200 with a scoped or empty result (Group list fallback, owner/group-filtered list, `MOJO_REST_LIST_PERM_DENY=False`), no denial event is recorded — the request succeeded.

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

## POST_SAVE_ACTIONS

`POST_SAVE_ACTIONS` lets you define request fields that trigger arbitrary methods **after** the model is saved, without being treated as model field updates. This is the standard pattern for operations that act on a saved instance — things like testing a connection, sending a notification, cloning a record, or kicking off a job.

### How it works

1. Any key in `POST_SAVE_ACTIONS` found in `request.DATA` is held aside — it is **never written to the model**.
2. The model is saved normally with all other fields.
3. After save, the framework calls `self.on_action_<key>(value)` for each held-aside key.
4. If the handler returns a non-`None` dict, that dict is used as the API response instead of the normal serialized instance. Return a plain dict — the framework wraps it automatically (see [Return Values](decorators.md#return-values)).

The default value is `["action"]`, meaning a field named `action` in any POST request is automatically treated as a post-save trigger.

### Setup

```python
class RestMeta:
    POST_SAVE_ACTIONS = ["action", "test_connection", "clone"]
```

### Handler signature

```python
def on_action_<name>(self, value):
    # value = whatever the client sent for that field
    # return None              → normal response (serialized instance)
    # return {"key": "val"}   → wrapped as {"status": True, "data": {"key": "val"}}
    # return {"status": False, "error": "..."} → passed through as error response
```

### Accessing the request inside a handler

Use `self.active_request` to get the current HTTP request and `self.active_request.DATA` to read any additional params the client sent:

```python
def on_action_send_report(self, value):
    request = self.active_request
    email = request.DATA.get("email")
    fmt = request.DATA.get("format", "pdf")
    send_report(self, email=email, format=fmt)
    return {"sent_to": email}
    # → {"status": True, "data": {"sent_to": "user@example.com"}}
```

### Examples

**Simple action flag** — client POSTs `{"action": "archive"}` to update and archive in one request:

```python
class RestMeta:
    POST_SAVE_ACTIONS = ["action"]

def on_action_action(self, value):
    if value == "archive":
        self.status = "archived"
        self.save()
        return {"archived": True}
        # → {"status": True, "data": {"archived": True}}
    if value == "publish":
        self.published = True
        self.save()
        return {"status": True}
        # → passed through as-is
```

**Named action** — test a connection after saving credentials:

```python
class RestMeta:
    POST_SAVE_ACTIONS = ["action", "test_connection"]

def on_action_test_connection(self, value):
    try:
        self.backend.test_connection()
        return {"status": True}
    except Exception as e:
        return {"status": False, "error": str(e)}
```

**Clone** — create a copy of the current record:

```python
def on_action_clone(self, value):
    new = MyModel(user=self.user, group=self.group, name=f"Copy of {self.name}")
    new.save()
    return {"id": new.id}
    # → {"status": True, "data": {"id": 42}}
```

**Using extra request params** — read additional data beyond the action field:

```python
def on_action_invite(self, value):
    request = self.active_request
    email = request.DATA.get("email")
    role = request.DATA.get("role", "member")
    if not email:
        return {"status": False, "error": "email required"}
    send_invite(self, email=email, role=role)
    return {"invited": email}
```

### Client usage

```
POST /api/myapp/integration/42
{"test_connection": true}

POST /api/myapp/integration/42
{"name": "Updated Name", "action": "archive"}

POST /api/myapp/integration/42
{"action": "invite", "email": "user@example.com", "role": "admin"}
```

Actions can be combined with normal field updates in a single POST — the model fields are saved first, then the action handler runs.

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
| `self.active_request` | Current HTTP request (via ContextVar) — available in any model method called during a request |
| `self.active_request.DATA` | Unified dict of all request data (POST body + GET params) |
| `self.active_user` | Current authenticated user (`self.active_request.user`) |

`self.active_request` is set automatically by the framework at the start of every REST request and is accessible from any model method — lifecycle hooks, `set_<field>` methods, `on_action_<name>` handlers, etc. Use it instead of threading request through every method call:

```python
def on_rest_saved(self, changed_fields, created):
    request = self.active_request
    if request and "status" in changed_fields:
        notify_status_change(self, user=request.user)

def on_action_resend(self, value):
    lang = self.active_request.DATA.get("lang", "en")
    send_notification(self, lang=lang)
    return {"status": True}
```

## Settings

| Setting | Default | Description |
|---|---|---|
| `MOJO_APP_STATUS_200_ON_ERROR` | `False` | Return HTTP 200 even on errors (for legacy clients) |
| `MOJO_REST_LIST_PERM_DENY` | `True` | Return 403 (vs empty list) when list perm denied |
