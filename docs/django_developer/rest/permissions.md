# REST Permission System — Django Developer Reference

## How RestMeta Permissions Work

Every model with `RestMeta` gets automatic CRUD via `Model.on_rest_request(request, pk)`. The framework handles permission checks, list filtering, pagination, and serialization.

## Permission Flow

```
on_rest_request(request, pk)
  ├─ pk provided:
  │    GET    → on_rest_handle_get(request, instance)    → checks VIEW_PERMS on instance
  │    POST   → on_rest_handle_save(request, instance)   → checks SAVE_PERMS → VIEW_PERMS on instance
  │    DELETE  → on_rest_handle_delete(request, instance) → checks CAN_DELETE, then DELETE_PERMS → SAVE_PERMS → VIEW_PERMS on instance
  │
  └─ no pk:
       GET    → on_rest_handle_list(request)             → checks VIEW_PERMS (system-level first, then owner/group fallback)
       POST   → on_rest_handle_create(request)           → checks SAVE_PERMS → VIEW_PERMS
```

## RestMeta Properties

| Property | Default | Purpose |
|---|---|---|
| `VIEW_PERMS` | `[]` | Permissions needed to view/list. Empty = public. |
| `SAVE_PERMS` | `[]` | Permissions needed to create/update. Falls back to VIEW_PERMS if empty. |
| `DELETE_PERMS` | `[]` | Permissions needed to delete. Falls back to SAVE_PERMS → VIEW_PERMS. |
| `CREATE_PERMS` | `[]` | Permissions needed to create (POST without pk). Falls back to SAVE_PERMS. |
| `CAN_DELETE` | `False` | Must be `True` for DELETE to work at all. |
| `NO_REST_SAVE` | `False` | Blocks POST/PUT entirely. |
| `NO_REST` | `False` | Blocks all REST operations. |
| `OWNER_FIELD` | `"user"` | FK field name pointing to the owning user. Used with `"owner"` perm. |
| `GROUP_FIELD` | `"group"` | FK field name pointing to the owning group. |
| `DENY_AI` | `False` | Shorthand — denies all assistant model tools on this model regardless of verb. |
| `DENY_AI_VIEW` | `False` | Blocks the assistant's `describe_model`, `query_model`, `aggregate_model`, and `export_data`. |
| `DENY_AI_CREATE` | `False` | Blocks the create path of the assistant's `save_model_instance`. |
| `DENY_AI_UPDATE` | `False` | Blocks the update path of the assistant's `save_model_instance`. |
| `DENY_AI_DELETE` | `False` | Blocks the assistant's `delete_model_instance`. |

## Assistant Access Flags

The `DENY_AI_*` flags are **defense in depth on top of REST permissions**. They let model authors express "this model should not be accessible through the LLM assistant, even to users who have the REST perms for it." REST continues to work unchanged for human-driven requests — only the assistant tools honor the flags.

| When to use | Typical flag |
|---|---|
| A model an operator could touch via the UI but the LLM shouldn't (e.g. `account.User` membership edits) | `DENY_AI_UPDATE` |
| Append-only / audit-style rows (`LoginEvent`, `Click`) that should never be mutated via chat | `DENY_AI_CREATE` + `DENY_AI_UPDATE` + `DENY_AI_DELETE` |
| Models containing secrets the LLM should not even introspect | `DENY_AI_VIEW` |
| Anything the LLM should stay away from entirely | `DENY_AI` |

The gate runs **before** the REST permission check in the assistant tools, so denied requests return a distinct error — `"<model> is not available to the assistant"` — that tells the user a permission change will not help. Denials emit a level-4 informational `assistant_ai_denied` incident event for operator visibility; these are expected policy events, not attack signals.

```python
class RestMeta:
    VIEW_PERMS = ["view_users", "users"]
    SAVE_PERMS = ["manage_users", "users"]
    DENY_AI_UPDATE = True   # humans via UI still fine; assistant cannot update
    DENY_AI_DELETE = True
```

Tools that honor the flags: `describe_model`, `query_model`, `aggregate_model`, `export_data`, `save_model_instance` (create vs update picked from pk presence), `delete_model_instance`.

## Special Permission Strings

| String | Meaning |
|---|---|
| `"all"` | No authentication required (public) |
| `"authenticated"` | Any logged-in user |
| `"owner"` | The instance's `OWNER_FIELD` matches `request.user` |
| Any other string | Checked via `request.user.has_permission(perm)` |

## The "owner" Permission

`"owner"` is a special string in permission lists. It enables user-scoped access:

### For detail (GET/PUT/DELETE with pk):
`rest_check_permission` checks `instance.{OWNER_FIELD}.id == request.user.id`. If the requesting user owns the instance, permission is granted.

### For list (GET without pk):
`on_rest_handle_list` has a fallback path. If the user doesn't have system-level permissions but `"owner"` is in `VIEW_PERMS`, it filters the queryset: `Model.objects.filter({OWNER_FIELD}=request.user)`.

**The flow is:**
1. Try system-level perm check (e.g., does user have `"view_admin"`?)
2. If YES → `on_rest_list(request)` with **all** objects (admin sees everything)
3. If NO and `"owner"` in VIEW_PERMS → `on_rest_list(request, filtered_queryset)` with **owner's objects only**
4. If NO and model has `group` field → check group-level permissions, filter by groups
5. Otherwise → 403 or empty list

### Example: Owner-scoped with admin override

```python
class Conversation(models.Model, MojoModel):
    user = models.ForeignKey("account.User", on_delete=models.CASCADE)

    class RestMeta:
        VIEW_PERMS = ["view_admin", "owner"]
        OWNER_FIELD = "user"
        CAN_DELETE = True
```

- User with `view_admin` → sees all conversations, can delete any
- User without `view_admin` but is owner → sees only their own, can delete their own
- User without `view_admin` and not owner → 403

### Example: Owner-only (no admin override)

```python
class UserNote(models.Model, MojoModel):
    user = models.ForeignKey("account.User", on_delete=models.CASCADE)

    class RestMeta:
        VIEW_PERMS = ["owner"]
        SAVE_PERMS = ["owner"]
        OWNER_FIELD = "user"
```

- Only the owning user can see, create, and edit their notes
- No admin override

## Group-Scoped Permissions

When a model has a `group` FK and the user doesn't have system-level permissions, the framework checks if the user has the required permissions within any group:

```python
groups_with_perms = request.user.get_groups_with_permission(perms)
queryset.filter(group__in=groups_with_perms)
```

The `request.group` context is set automatically when an instance has a `group` attribute.

## Delete Gating

DELETE requires two checks:
1. `CAN_DELETE = True` (model-level gate — without this, DELETE always returns 403)
2. Permission check: `DELETE_PERMS` → falls back to `SAVE_PERMS` → falls back to `VIEW_PERMS`

The permission check passes the `instance`, so `"owner"` works for delete too.

## NO_REST_SAVE

When `NO_REST_SAVE = True`, POST/PUT are blocked. This is for models where mutations happen through service functions (not direct REST save). GET (list/detail) and DELETE (if `CAN_DELETE = True`) still work normally.

## Standard CRUD Endpoint Pattern

```python
from mojo import decorators as md
from myapp.models import MyModel

@md.URL('mymodel')
@md.URL('mymodel/<int:pk>')
@md.uses_model_security(MyModel)
def on_mymodel(request, pk=None):
    return MyModel.on_rest_request(request, pk)
```

`@md.uses_model_security(Model)` is required — it sets up the permission context. Do NOT add `@md.requires_auth()` alongside it (VIEW_PERMS handles auth).

## Graphs

Graphs control serialization — what fields are included in the response.

```python
GRAPHS = {
    "default": {
        "fields": ["id", "title", "created"],
    },
    "detail": {
        "fields": ["id", "title", "metadata", "created", "modified"],
        "extra": ["computed_field"],        # @property on the model
        "graphs": {"user": "basic"},        # serialize user FK with "basic" graph
    },
    "list": {
        "fields": ["id", "title"],
    },
}
```

- `"fields"` — model fields to include
- `"extra"` — `@property` names on the model (computed/virtual fields)
- `"graphs"` — related object serialization: `{field_name: graph_name}`

The client selects a graph via `?graph=detail`. Default is `"default"`.

For list endpoints, the framework uses `"list"` graph if it exists, otherwise `"default"`.

## FK Assignment During Save

When a save payload assigns a ForeignKey field by primary key — e.g. `data={"group": 5}` — the framework looks up the target via `field.related_model.objects.get(pk=value)` and then runs `field.related_model.rest_check_permission(request, "VIEW_PERMS", related_instance)` before assigning. On denial the assignment is silently skipped (the parent save proceeds with the field unchanged) and an incident event is recorded by `rest_check_permission`.

This prevents cross-model privilege escalation: a user with SAVE_PERMS on model A but no view access to model B cannot attach B records to A. The same gate has always applied when the value is a dict (which triggers a cascading save and additionally requires SAVE_PERMS on the target).

Cases that do **not** require VIEW_PERMS on the target:

| Case | Reason |
|---|---|
| Self-reference (`a.parent = a.pk`) | Caller already authorized for self |
| Clear (`group=0` / `None` / `""`) | No target to view |
| Related model is non-MojoModel (no `rest_check_permission`) | Framework only gates models that opt in |
