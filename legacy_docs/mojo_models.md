# Mojo Models: REST base and Secrets mixins

A lightweight way to build RESTful Django models with consistent CRUD semantics, robust permission checks, and easy secret storage (either encrypted locally or via AWS KMS).

- `MojoModel`: a REST mixin that handles list/get/create/update/delete, permission checks, filtering, sorting, pagination, hooks, and serialization via GraphSerializer.
- `MojoSecrets` and `KSMSecrets`: abstract model mixins to store encrypted secrets, with simple dict-like APIs. `KSMSecrets` uses AWS KMS envelope encryption.

This guide shows how to wire these into your Django app.

Contents
- Quickstart: REST with MojoModel
- URL wiring and minimal views
- RestMeta options
- List: filtering, search, sort, pagination, date-range
- Create/Update: field handling, relations, files, JSONField merging
- Hooks and lifecycle
- Permissions model
- Responses and serialization
- Settings
- Secrets: MojoSecrets (local encryption)
- Secrets: KSMSecrets (AWS KMS encryption)
- Tips and patterns


## Quickstart: REST with MojoModel

Define your model by mixing in `MojoModel` with a normal Django model. Add `RestMeta` with your options and serializer graphs.

    from django.db import models
    from mojo.models.rest import MojoModel

    class Widget(models.Model, MojoModel):
        name = models.CharField(max_length=120)
        description = models.TextField(blank=True, null=True)
        created = models.DateTimeField(auto_now_add=True)
        modified = models.DateTimeField(auto_now=True)

        class RestMeta:
            # Permissions (see "Permissions model" below)
            VIEW_PERMS = []        # empty → public read
            SAVE_PERMS = ["staff"] # edit permissions
            DELETE_PERMS = ["admin"]
            CAN_DELETE = True

            # GraphSerializer graphs (your GraphSerializer should know these)
            GRAPHS = {
                "list": {"fields": ["id", "name", "created"]},
                "default": {"fields": ["id", "name", "description", "created", "modified"]},
                "basic": {"fields": ["id", "name"]},
            }

            # List/search behavior
            SEARCH_FIELDS = ["name", "description"]

            # Optional
            LOG_CHANGES = True                  # emits model logs on create/update
            OWNER_FIELD = "user"                # used for owner checks if "owner" in perms
            CREATED_BY_OWNER_FIELD = "user"     # auto-set on create
            UPDATED_BY_OWNER_FIELD = "modified_by"
            GROUP_FIELD = "group"               # if you have group scoping
            NO_SAVE_FIELDS = ["id", "created", "modified"]
            POST_SAVE_ACTIONS = ["action"]      # actions executed after save (see Save workflow)
            FORMATS = {
                # Used for non-JSON exports. If not defined, falls back to graph fields.
                "csv_export": ["id", "name", "created"]
            }

Now wire an endpoint that delegates to the model’s `on_rest_request` handler for list/get/create/update/delete.


## URL wiring and minimal views

MojoModel does not require a custom view class; a small function view is enough.

    # views.py
    from django.views.decorators.csrf import csrf_exempt
    from django.utils.decorators import method_decorator
    from django.http import HttpRequest
    from .models import Widget

    @csrf_exempt
    def widgets_api(request: HttpRequest, pk=None):
        # You can add authentication, request.DATA hydration, and group scoping here.
        # request.DATA is expected to provide dict-like access; if your stack doesn't
        # pre-populate it, do so (e.g., merge GET/POST/JSON into request.DATA).
        return Widget.on_rest_request(request, pk)

    # urls.py
    from django.urls import path
    from .views import widgets_api

    urlpatterns = [
        path("api/widgets", widgets_api, name="widgets-list-or-create"),
        path("api/widgets/<int:pk>", widgets_api, name="widgets-detail"),
    ]

Supported methods and behavior
- GET /api/widgets → list (with filters, search, sort, pagination)
- POST /api/widgets → create
- GET /api/widgets/<pk> → retrieve
- PUT/POST /api/widgets/<pk> → update
- DELETE /api/widgets/<pk> → delete (if allowed)


## RestMeta options

Attach a `RestMeta` inner class on your model to customize behavior. Recognized properties:

- Permissions
  - `VIEW_PERMS`: list | str | [] (empty means public read)
  - `SAVE_PERMS`: required to create/update
  - `DELETE_PERMS`: required to delete
  - `CAN_DELETE`: bool (default False) master gate for delete

- Serialization
  - `GRAPHS`: mapping for GraphSerializer graphs, e.g., {"list": {"fields": [...]}, "default": {...}}
  - `FORMATS`: mapping for non-JSON export formats (e.g., CSV)

- Ownership and logging
  - `OWNER_FIELD`: owner attribute used when "owner" appears in required perms
  - `CREATED_BY_OWNER_FIELD`: auto-assigned to `request.user` on create if authenticated
  - `UPDATED_BY_OWNER_FIELD`: auto-assigned to `request.user` on update if authenticated
  - `LOG_CHANGES`: bool; if True, writes change logs via `log()` on create/update

- Group scoping
  - `GROUP_FIELD`: if set (or model has `group`) and `request.group` is present, list results are scoped to that group

- List/search
  - `SEARCH_FIELDS`: list of fields used when the `search` param is provided

- Save behavior
  - `NO_SAVE_FIELDS`: fields to ignore on create/update
  - `POST_SAVE_ACTIONS`: keys that trigger post-save handlers `on_action_<key>`


## List: filtering, search, sort, pagination, date-range

List endpoint (GET without pk) flows through:
- `on_rest_list(request, queryset=None)`
- `on_rest_list_filter(request, queryset)`
- `on_rest_list_date_range_filter(request, queryset)`
- `on_rest_list_sort(request, queryset)`
- `on_rest_list_response(request, queryset)`

Query parameters (request.QUERY_PARAMS)
- Filtering
  - Any model field name is supported (exact lookup by default).
  - Dot or double underscore for related traversals: "user.id=5" or "user__id=5".
  - Example:
        GET /api/widgets?name=foo&user.id=5
- Search
  - Param: `search` → matches `SEARCH_FIELDS` (icontains OR across fields)
  - Default `SEARCH_FIELDS` (if not set): all CharField/TextField on model
- Sort
  - Param: `sort` (from request.DATA); default "-id"
  - Example:
        GET /api/widgets?sort=name
        GET /api/widgets?sort=-created
- Pagination
  - Size: `size` or `limit` (default 10)
  - Start: `start` or `offset` (default 0)
  - Response contains total `count`, `start`, and `size`
- Date range
  - `dr_field` (default "created")
  - `dr_start`, `dr_end` as parseable datetimes (ISO recommended)
  - If `request.group` present, date inputs are localized via `group.get_local_time(...)`
  - Example:
        GET /api/widgets?dr_field=created&dr_start=2025-08-01T00:00:00Z&dr_end=2025-08-08T00:00:00Z

Notes
- Query params are normalized (e.g., "." → "__") for relation filtering.
- Boolean and date fields are parsed to native types automatically.
- Unknown fields are ignored.


## Create/Update: fields, relations, files, JSONField merging

Save handler
- `on_rest_handle_save(request, instance)` delegates to `instance.on_rest_save_and_respond(request)`.
- `on_rest_save(request, data_dict)` handles the actual update.

Field handling
- Skips keys in `NO_SAVE_FIELDS`.
- If your model defines a method `set_<field>(value)`, it is used as a custom setter and the change is tracked.
- Booleans: string/int coercion to bool.
- Date/DateTime: parsed from strings using `dates.parse_datetime(...)`.

ForeignKey handling
- If the incoming value is an integer (or numeric string): sets FK to that object id, 0/blank sets to None.
- If the incoming value is a dict and the related model allows edits:
  - If related model has `rest_check_permission`, it is called with ["SAVE_PERMS","VIEW_PERMS"] against the related instance.
  - On pass, `related_instance.on_rest_save(request, dict_value)` is executed (nested update).

Files
- If you include a top-level `files` mapping in the payload, `on_rest_save_files` iterates it and calls `on_rest_save_file` per key.
- If the field is a relation and the related model provides `create_from_file(file, field_name)`, it will be invoked to create/store the file object and set the relation.

JSONField merging
- For JSONField fields, updates merge into the existing dict (shallow merge) via `on_rest_update_jsonfield`.

Post-save actions
- Keys listed in `POST_SAVE_ACTIONS` are only executed after a successful save by invoking `on_action_<key>(value)` on your model.
- You can return a dict from the action to short-circuit the normal object response and send a custom payload.

Owner/group auto-assign
- On create:
  - If `CREATED_BY_OWNER_FIELD` exists and user is authenticated → set to `request.user`.
  - If `GROUP_FIELD` is configured or model has `group`, and `request.group` is set, backfill `group` if it’s currently None.
- On update:
  - If `UPDATED_BY_OWNER_FIELD` exists and user is authenticated → set to `request.user`.

Responses
- On success, `on_rest_save_and_respond` returns the serialized object (`on_rest_get`).
- If an action returned a response, that payload is returned instead.

Example: updating a FK, JSONField, and file

    # Payload (JSON)
    {
      "name": "New Name",
      "owner": 12,             # FK set to user pk 12
      "metadata": { "a": 1 },  # JSONField merge
      "files": {
        "photo": "<UploadedFile>"   # request.FILES binding from your view stack
      }
    }


## Hooks and lifecycle

Useful extension points on your model:

- `on_rest_created(self)`: after successful create (before response)
- `on_rest_pre_save(self, changed_fields, created)`: before saving to DB
- `on_rest_saved(self, changed_fields, created)`: after saving to DB
- `on_rest_pre_delete(self)`: before delete (inside try/except)
- `on_rest_delete(self, request)`: deletes the instance in a transaction

Change tracking in saves
- `self.__changed_fields__` is populated with {field: old_value} when a field’s value changes.
- Helpers:
  - `has_changed()`, `has_field_changed(field)`, `get_changes(data_dict)`

Logging and incidents
- `log(kind="model:changed", log=...)`
- `report_incident(details, event_type="info", level=1, request=None, **context)`
- `class_report_incident(...)` and `class_report_incident_for_user(...)`

Convenience constructors
- `create_from_dict(dict_data, request=...)`
- `create_from_request(request, **kwargs)`
- `update_from_dict(dict_data)`


## Permissions model

Permission checks happen via `rest_check_permission(request, permission_keys, instance=None)`.

- `VIEW_PERMS`, `SAVE_PERMS`, `DELETE_PERMS` can be:
  - empty/None → allowed
  - list of permission keys (checked via `request.user.has_permission(perms)`)
  - special tokens:
    - `"all"`: skips authentication check
    - `"owner"`: allowed if `instance.<OWNER_FIELD>.id == request.user.id`
- Group membership check:
  - If `request.group` and your model participates in the same group, we delegate to `request.group.user_has_permission(request.user, perms)`.
- Instance hooks:
  - If your instance implements `check_view_permission(perms, request)` or `check_edit_permission(perms, request)`, these are consulted first for instance-level checks.

List fallback for owner
- If list view denies and `VIEW_PERMS` includes `"owner"`, the list endpoint will return only objects owned by `request.user`, provided they’re authenticated.

Error responses
- Denials return a JSON payload from `rest_error_response` with `"status": False` and an HTTP status (403 by default).
- If `MOJO_APP_STATUS_200_ON_ERROR=True`, the HTTP status is forced to 200 with `"code"` capturing the original code.

Example RestMeta for “owner can read, staff can edit, admin can delete”:

    class RestMeta:
        VIEW_PERMS = ["owner", "all"]  # owner or any authenticated user (if you want stricter, drop "all")
        SAVE_PERMS = ["staff"]
        DELETE_PERMS = ["admin"]
        CAN_DELETE = True
        OWNER_FIELD = "user"


## Responses and serialization

- For list endpoints, `on_rest_list_response` builds a paginated JSON response using GraphSerializer.
- For single object responses, `on_rest_get` returns the serialized object.
- Graphs come from `RestMeta.GRAPHS`. Client can request a graph via `graph=<name>`.

Non-JSON export
- If `download_format` is provided (e.g., "csv" or "csv_export"), the format serializer is used:
  - The serializer key is derived from the format (e.g., "csv").
  - If `FORMATS[download_format]` exists, those fields are used for export; otherwise we fallback to the fields in a basic/default graph.
  - Response is streamed with a filename from `filename` or `<Model>.csv`.

Returned list response shape (example)

    {
      "status": True,
      "data": [...serialized items...],
      "count": 123,
      "start": 0,
      "size": 10
    }


## Settings

- `MOJO_APP_STATUS_200_ON_ERROR` (bool)
  - When True, error responses use HTTP 200 with a payload that contains an error `code`.
- `MOJO_REST_LIST_PERM_DENY` (bool, default False)
  - When True, permission-denied on list returns an error instead of an empty list.

Both are read from the central settings bridge (`mojo.helpers.settings.settings`).


## Secrets: MojoSecrets (local encryption)

Add encrypted secret storage to your model.

    from django.db import models
    from mojo.models.secrets import MojoSecrets

    class Integration(models.Model, MojoSecrets):
        name = models.CharField(max_length=120)
        created = models.DateTimeField(auto_now_add=True)
        # MojoSecrets adds: mojo_secrets = models.TextField(...)

Usage

- Setting secrets:
      obj = Integration.objects.create(name="Alpha")
      obj.set_secret("api_key", "xyz")
      obj.set_secrets({"token": "abc", "region": "us-east-1"})
      obj.save()  # secrets are automatically encrypted and persisted
- Reading secrets (dict-like):
      key = obj.get_secret("api_key")
      token = obj.secrets.get("token")
      all_secrets = obj.secrets  # returns a dict-like objict
- Clearing secrets:
      obj.clear_secrets()
      obj.save()

How it works
- Secrets are stored encrypted in `mojo_secrets` TextField.
- Encryption/decryption handled by `mojo.helpers.crypto` with a password derived by `_get_secrets_password()`.
- Default password scheme:
  - Uses `<created><pk><ClassName>` if `created` field exists; otherwise `<pk><ClassName>`.
  - Override `_get_secrets_password()` for custom key derivation (e.g., include tenant salt, environment).

Save behavior
- On first save (no PK yet), the model is saved, then secrets are encrypted and saved in a second write.
- On updates, secrets are encrypted before save in a single write.

Caveats
- Avoid logging secrets. The mixin may call `self.debug(...)` if present; ensure your logger scrubs sensitive data.


## Secrets: KSMSecrets (AWS KMS)

Like `MojoSecrets`, but encrypts with AWS KMS (envelope encryption via helper). Useful for stricter compliance and centralized key management.

    from django.db import models
    from mojo.models.secrets import KSMSecrets

    class CloudIntegration(models.Model, KSMSecrets):
        name = models.CharField(max_length=120)
        created = models.DateTimeField(auto_now_add=True)

Usage is identical to `MojoSecrets`:

- Set secrets:
      obj = CloudIntegration.objects.create(name="Beta")
      obj.set_secrets({"api_key": "xyz", "account": "123"})
      obj.save()
- Read secrets:
      val = obj.get_secret("api_key")
      data = obj.secrets

Configuration (required)
- `KMS_KEY_ID`: AWS KMS key id or ARN
- `AWS_REGION` or `AWS_DEFAULT_REGION`

Key notes
- Context binding: secrets are encrypted/decrypted with a context string
  `<app_label>.<ModelName>.<pk>.mojo_secrets` to add integrity (helps prevent ciphertext reuse across contexts).
- A small in-memory helper cache is used per (kms_key_id, region).

Error handling
- If decrypt fails (e.g., bad key or region), the mixin exposes an empty secrets object rather than raising (read returns empty dict-like).
- Save always re-encrypts the current secrets if changed.


## Tips and patterns

- Keep `VIEW_PERMS=[]` for public read APIs; add `SAVE_PERMS` for authenticated edits.
- Use `"owner"` in `VIEW_PERMS` to let users see only their own resources when global list is restricted.
- Prefer consistent slug-like `search` and pagination semantics across your endpoints for uniform UX.
- For nested FK updates, ensure the related models also derive from `MojoModel` so permission checks and save flows work the same.
- For file fields, create a small related model with a `create_from_file` helper to centralize storage logic.
- If you need custom field-level validation or transformation, implement `set_<field>(value)` on your model.
- Use `LOG_CHANGES=True` on models where auditability is important (it emits structured logs via `logit`).
- For secrets:
  - Choose `KSMSecrets` if you already operate in AWS and want centralized key control.
  - Override `_get_secrets_password()` in `MojoSecrets` to incorporate environment-specific salts if needed.
  - Always avoid logging secret contents; mask values in errors and logs.

That’s it. With these mixins, you get solid default REST behavior, concise model-centric configuration, and a sane way to store sensitive data.