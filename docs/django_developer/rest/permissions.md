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
       POST with `batched` list (CAN_BATCH=True)
              → on_rest_handle_batch(request)            → class-level SAVE_PERMS → VIEW_PERMS gate, then a per-row
                                                           instance check (see "Batch Save Permissions" below)
```

## RestMeta Properties

| Property | Default | Purpose |
|---|---|---|
| `VIEW_PERMS` | `[]` | Permissions needed to view/list. Empty = public. |
| `SAVE_PERMS` | `[]` | Permissions needed to create/update. Falls back to VIEW_PERMS if empty. |
| `DELETE_PERMS` | `[]` | Permissions needed to delete. Falls back to SAVE_PERMS → VIEW_PERMS. |
| `CREATE_PERMS` | `[]` | Permissions needed to create (POST without pk). Falls back to SAVE_PERMS. |
| `CAN_DELETE` | `False` | Must be `True` for DELETE to work at all. |
| `CAN_UPDATE` | `True` | Set `False` to block PUT/POST against an existing instance (pair with `CAN_CREATE`/`CAN_DELETE`). Supersedes the deprecated `CAN_SAVE` — see below. |
| `NO_REST_SAVE` | `False` | Blocks POST/PUT entirely. |
| `NO_REST` | `False` | Blocks all REST operations. |
| `OWNER_FIELD` | `"user"` | FK field name pointing to the owning user. Used with `"owner"` perm. |
| `GROUP_FIELD` | `"group"` | FK field name pointing to the owning group. May be a related path (e.g. `"original_file__group"`, `"agent__project"`). Governs detail + list + `?group=` scoping and create-time auto-assign — see [Group-Scoped Permissions](#group-scoped-permissions). |
| `CREATED_BY_OWNER_FIELD` | `"user"` | FK field auto-stamped with `request.user` on create when the body omits it. Set to `None` to disable auto-stamping entirely. See "Create-time owner stamping" below. |
| `UPDATED_BY_OWNER_FIELD` | `"modified_by"` | FK field set to `request.user` on every update. Unlike `CREATED_BY_OWNER_FIELD`, the update-path stamp always overwrites — "who last modified" is an actor fact, not a body fact. |
| `DENY_AI` | `False` | Shorthand — denies all assistant model tools on this model regardless of verb. |
| `DENY_AI_VIEW` | `False` | Blocks the assistant's `describe_model`, `query_model`, `aggregate_model`, and `export_data`. |
| `DENY_AI_CREATE` | `False` | Blocks the create path of the assistant's `save_model_instance`. |
| `DENY_AI_UPDATE` | `False` | Blocks the update path of the assistant's `save_model_instance`. |
| `DENY_AI_DELETE` | `False` | Blocks the assistant's `delete_model_instance`. |

## `CAN_UPDATE` — block writes to existing instances

`CAN_UPDATE` gates PUT/POST on an existing pk, mirroring how `CAN_CREATE` gates creates and `CAN_DELETE` gates deletes. Default `True` — no existing model changes behavior unless you opt in. Set `False` to make a model append-only:

```python
class RestMeta:
    CAN_CREATE = False
    CAN_UPDATE = False   # blocks PUT to existing rows
    CAN_DELETE = False
```

On denial the REST layer returns `403` with `error = "UPDATE not allowed: <ModelName>"` and emits a `feature_disabled` incident event — a distinct category so operators can tell the block is policy, not a per-user permission shortfall. `CAN_DELETE=False`, `CAN_CREATE=False`, and `CAN_BATCH=False` use the same `feature_disabled` category, each with a distinguishable `branch` (`can_update_false`, `can_delete_false`, `can_create_false`, `can_batch_false`).

### `CAN_SAVE` is deprecated

Earlier versions referenced `CAN_SAVE` in RestMeta, but `rest.py` never read it — so `CAN_SAVE = False` on models like `LoginEvent` and `ShortLinkClick` did not actually block updates. `CAN_UPDATE` is the real gate. `CAN_SAVE` is now honored as a one-release deprecated alias: the framework prefers `CAN_UPDATE` when both are set, and emits a one-shot `logit.warning` for any class that still uses `CAN_SAVE` alone. Rename your models to `CAN_UPDATE` before the next release.

## Create-time owner stamping

When a row is created, the framework auto-assigns `CREATED_BY_OWNER_FIELD` (default `"user"`) to `request.user` — **but only when the body did not provide a value for that field**. This mirrors the long-standing behavior for `group`: body wins, auto-fill covers the omitted case.

```python
# Self-signup path (body omits user) — framework stamps request.user.
POST /api/shortlink/shortlink   body: {code: "abc"}          → user = request.user

# Admin enrols another user (body sets user) — framework respects it.
POST /api/routes/operator       body: {user: 7, group: 1}    → user = 7

# Explicit null / 0 / "" → coerced to None → auto-stamp still kicks in.
POST /api/shortlink/shortlink   body: {user: null}           → user = request.user
```

### Security implications

This default is permissive: **any caller with `SAVE_PERMS` on the model can designate another user as the record's owner by including `user` in the body**, provided they also pass the per-FK `VIEW_PERMS` check on `account.User` (`view_users` / `manage_users` / `users`). The framework does not enforce "admin-only" semantics on the owner field — that is per-model policy.

If you need strict self-ownership for a model (i.e. the framework must guarantee `user == request.user` on create regardless of body), disable the auto-stamp and re-assert in `on_rest_pre_save`:

```python
class RestMeta:
    CREATED_BY_OWNER_FIELD = None   # disable framework auto-stamp
    SAVE_PERMS = ["owner", "manage_foo"]

def on_rest_pre_save(self, changed_fields, created):
    if created:
        self.user = self.active_user   # pin to the caller; ignore body
```

Or — for models like `Operator` where admins legitimately create rows for other users — keep the default and add an explicit gate in `on_rest_pre_save`:

```python
def on_rest_pre_save(self, changed_fields, created):
    if created and self.user_id != self.active_user.id:
        if not self.active_user.has_perm("manage_routes"):
            raise me.PermissionDeniedException("manage_routes required to create operator for another user")
        # plus membership / activity checks for the target user
```

### Opt-out quick reference

| Behavior needed | How |
|---|---|
| Default — self-signup works, admin-for-another-user works with proper perms | Do nothing. |
| Auto-stamp a field other than `user` (e.g. `created_by`) | `CREATED_BY_OWNER_FIELD = "created_by"` |
| No auto-stamp at all | `CREATED_BY_OWNER_FIELD = None` |
| Force caller identity on create regardless of body | `CREATED_BY_OWNER_FIELD = None` + re-assign in `on_rest_pre_save` |

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
4. If NO and the model is group-scoped (a direct `group` FK **or** a `RestMeta.GROUP_FIELD`) → check group-level permissions, filter by `{GROUP_FIELD}__in=<groups where the user holds the perm>`
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

A model is **group-scoped** when it has a direct `group` FK **or** it declares
`RestMeta.GROUP_FIELD`. When the user lacks system-level permissions, the
framework checks whether they hold the required permission within any group and
filters to those tenants:

```python
groups_with_perms = request.user.get_groups_with_permission(perms)
group_field = cls.get_rest_meta_prop("GROUP_FIELD", "group")   # may be a related path
queryset.filter(**{f"{group_field}__in": groups_with_perms})
```

This governs **all three** access paths consistently — the bare-list member
fallback (above), the `?group=` narrower (`on_rest_list`), and detail
permission checks. For detail (GET/POST/DELETE on a pk), the framework resolves
the **instance's** owning group by traversing `GROUP_FIELD` and checks
membership against that group — so a caller cannot read another tenant's row by
passing their own `?group=`. `request.group` is set automatically to the
resolved instance group.

### `GROUP_FIELD` — naming a non-`group` or indirect owning FK

`GROUP_FIELD` defaults to `"group"`. Set it when the owning-group FK has a
different name, or when the tenant is reached through a related path:

```python
class FileRendition(models.Model, MojoModel):
    original_file = models.ForeignKey("fileman.File", ...)   # File has the group FK

    class RestMeta:
        VIEW_PERMS = ["view_fileman", "manage_files", "files"]
        GROUP_FIELD = "original_file__group"    # related path — traversed to the Group
```

```python
class AgentTask(models.Model, MojoModel):
    agent = models.ForeignKey("maestro.Agent", ...)          # Agent.project is the tenant Group

    class RestMeta:
        GROUP_FIELD = "agent__project"          # FK named "project", reached via "agent"
```

Notes:
- The path is traversed null-safe hop-by-hop; a null link along the way yields
  "no group" (fail-closed — the flat user/superuser check still applies).
- **Create-time auto-assign** stamps a **direct**-FK `GROUP_FIELD` from
  `request.group` when the body omits it (body wins), mirroring the historical
  `group` behavior. A **related-path** `GROUP_FIELD` has no local field to
  stamp — its tenant is derived from the FK the body provides, which is itself
  gated by the FK-attach VIEW check (attaching a foreign-tenant FK is denied).
- A model scoped only via `GROUP_FIELD` (no direct `group` attribute) is still
  correctly confined for `ApiKey` callers; a *truly* groupless model (no `group`
  FK and no `GROUP_FIELD`) denies keys by default (see `ALLOW_API_KEY_GLOBAL`).

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

When a save payload assigns a ForeignKey field by primary key — e.g. `data={"group": 5}` — the framework looks up the target via `field.related_model.objects.get(pk=value)` and then runs `field.related_model.rest_check_permission(request, "VIEW_PERMS", related_instance)` before assigning. On denial the assignment is silently skipped (the parent save proceeds with the field unchanged) and a `fk_attach_denied` incident event is emitted directly by `on_rest_save_related_field` via `_report_fk_attach_denied`.

The event carries these metadata fields: `field_name`, `related_model`, `related_id`, `branch`. It is not a 403 response — the request still returns 200 with the field left at its previous value.

This prevents cross-model privilege escalation: a user with SAVE_PERMS on model A but no view access to model B cannot attach B records to A. The same gate has always applied when the value is a dict (which triggers a cascading save and additionally requires SAVE_PERMS on the target).

**Models with a custom `on_rest_related_save`** (e.g. `fileman.File`) are gated the same way: when the assigned value is an **integer pk** (an "attach existing"), the VIEW_PERMS check runs on the target *before* `on_rest_related_save` is dispatched, honoring `NO_FK_VIEW_CHECK_FIELDS` on the parent. A **string / base64 / data-URL** value is an inline *create* (a new record the caller owns), so it skips the gate. Before ITEM-033 these models bypassed the check entirely — any authenticated caller could attach any File by id (cross-user/cross-tenant).

Cases that do **not** require VIEW_PERMS on the target:

| Case | Reason |
|---|---|
| Self-reference (`a.parent = a.pk`) | Caller already authorized for self |
| Clear (`group=0` / `None` / `""`) | No target to view |
| Related model is non-MojoModel (no `rest_check_permission`) | Framework only gates models that opt in |
| Inline create via `on_rest_related_save` (base64 / data-URL string) | New record the caller owns — a create, not an attach |
| Field listed in the parent's `NO_FK_VIEW_CHECK_FIELDS` | Model opts the field out (guarded elsewhere) |

## Batch Save Permissions

`on_rest_handle_batch` (`CAN_BATCH = True` + a `batched` list in the payload)
gates once at class level with `["SAVE_PERMS", "VIEW_PERMS"]` and no instance,
then re-checks **every row individually** with the same evaluation as the
single-instance paths:

- **Update rows** (`id`/`pk` resolves to an instance): `rest_check_permission(request, ["SAVE_PERMS", "VIEW_PERMS"], instance)` — the owner match, group/`GROUP_FIELD` tenant binding, and `check_view/edit_permission` hooks all apply per row, exactly as in `on_rest_handle_save`.
- **Create rows** (no `id`/`pk`, or the pk doesn't resolve): `rest_check_permission(request, ["CREATE_PERMS", "SAVE_PERMS", "VIEW_PERMS"])`, exactly as in `on_rest_handle_create`.

Denial is drop-with-audit, mirroring the FK-attach gate: the row is skipped,
the response's `errors` list gains `{"index": N, "error": "permission denied"}`,
and a `batch_row_denied` incident is emitted (level 2, metadata: `branch`
(`batch_update`/`batch_create`), `index`, `instance_id`, `model_name`,
`request_path`). It is not a 403 — the batch response is still 200 and the
remaining rows proceed. Rows are written sequentially with no transaction, so
failing the whole batch could not undo rows already written.

`request.group` is restored between rows: the per-instance check binds
`request.group` to each row's owning group (the row's true tenant), and the
handler resets it to the caller's original group before every row so one row's
tenant binding cannot leak into the next row's check or save.

Without this per-row gate, a caller who cleared the class-level gate for their
own group could update rows belonging to other tenants in the same batch.
