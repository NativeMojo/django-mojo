"""Models domain tools — let the LLM introspect and query any MojoModel."""
import objict

from django.apps import apps

from mojo.apps.assistant import tool
from mojo.helpers import logit

logger = logit.get_logger("assistant", "assistant.log")

MAX_LIMIT = 200
DEFAULT_LIMIT = 50

# Fields containing these substrings are excluded from describe output and rejected as filter keys
SENSITIVE_SUBSTRINGS = ("password", "auth_key", "onetime_code", "secret", "token")

# Django field type name mapping for cleaner output
FIELD_TYPE_MAP = {
    "AutoField": "integer",
    "BigAutoField": "integer",
    "SmallAutoField": "integer",
    "CharField": "string",
    "TextField": "text",
    "IntegerField": "integer",
    "BigIntegerField": "integer",
    "SmallIntegerField": "integer",
    "PositiveIntegerField": "integer",
    "PositiveSmallIntegerField": "integer",
    "PositiveBigIntegerField": "integer",
    "FloatField": "float",
    "DecimalField": "decimal",
    "BooleanField": "boolean",
    "NullBooleanField": "boolean",
    "DateTimeField": "datetime",
    "DateField": "date",
    "TimeField": "time",
    "EmailField": "email",
    "URLField": "url",
    "UUIDField": "uuid",
    "SlugField": "slug",
    "IPAddressField": "ip",
    "GenericIPAddressField": "ip",
    "FileField": "file",
    "ImageField": "image",
    "JSONField": "json",
    "BinaryField": "binary",
    "ForeignKey": "fk",
    "OneToOneField": "fk",
    "ManyToManyField": "m2m",
}


def _is_sensitive_field(name):
    """Check if a field name contains sensitive substrings."""
    name_lower = name.lower()
    return any(s in name_lower for s in SENSITIVE_SUBSTRINGS)


def _resolve_model(app_name, model_name):
    """Resolve and validate a model. Returns (model_class, error_dict)."""
    from mojo.models import MojoModel

    try:
        model = apps.get_model(app_name, model_name)
    except LookupError:
        return None, {"error": f"Model '{app_name}.{model_name}' not found"}

    if not issubclass(model, MojoModel):
        return None, {"error": f"'{app_name}.{model_name}' is not a MojoModel"}

    if getattr(model, "RestMeta", None) is None:
        return None, {"error": f"'{app_name}.{model_name}' has no RestMeta"}

    if getattr(model.RestMeta, "NO_REST", False):
        return None, {"error": f"'{app_name}.{model_name}' is not available for querying"}

    return model, None


# ---------------------------------------------------------------------------
# AI access gate — per-model, per-verb opt-out via RestMeta
# ---------------------------------------------------------------------------

# Per-verb flag names. `DENY_AI` (no suffix) is a shorthand that denies all.
_VERB_TO_FLAG = {
    "view": "DENY_AI_VIEW",
    "create": "DENY_AI_CREATE",
    "update": "DENY_AI_UPDATE",
    "delete": "DENY_AI_DELETE",
}


def _check_ai_access(model, verb, user, request=None):
    """Check per-model assistant opt-out flags. Returns None on allow, else an
    error dict.

    ``DENY_AI = True`` (shorthand) denies every verb regardless of the
    per-verb flag. Per-verb flags (``DENY_AI_VIEW`` / ``DENY_AI_CREATE`` /
    ``DENY_AI_UPDATE`` / ``DENY_AI_DELETE``) deny that verb specifically.

    Denials emit a level-4 informational security event — this is expected
    policy, not a permission probe. The error message is intentionally
    distinct from "Permission denied" so users do not chase a perm fix.
    """
    flag = _VERB_TO_FLAG.get(verb)
    if flag is None:
        return None

    blanket = model.get_rest_meta_prop("DENY_AI", False)
    specific = model.get_rest_meta_prop(flag, False)
    if not (blanket or specific):
        return None

    model_label = f"{model._meta.app_label}.{model.__name__}"
    reason = "DENY_AI" if blanket else flag
    details = (
        f"AI access denied: {verb} on {model_label} by user "
        f"{getattr(user, 'id', 'anon')} (flag={reason})"
    )
    logger.info(details)
    _report_security_event(
        "assistant_ai_denied", 4, details, user,
        model_name=model_label, request=request,
    )
    return {"error": f"{model_label} is not available to the assistant"}

    return model, None


def _audit_user_log(user, kind, action, model_label, pk, request=None,
                    conversation=None, fields=None):
    """Write a per-mutation audit entry to logit.Log against the target model.

    Field NAMES are recorded in the message and payload; values are not.
    Conversation id is stored in the payload when available so the audit
    trail ties back to the assistant turn. The synthetic ``request`` is
    passed through so uid/ip/user_agent flow into the Log row.
    """
    if user is None:
        return
    parts = [action, model_label, f"pk={pk}"]
    if fields:
        parts.append(f"fields=[{','.join(fields)}]")
    message = " ".join(parts)
    payload = {}
    if conversation is not None:
        conv_pk = getattr(conversation, "pk", None)
        if conv_pk is not None:
            payload["conversation_id"] = conv_pk
    if fields:
        payload["fields"] = list(fields)
    try:
        import ujson
        from mojo.apps.logit.models import Log
        Log.logit(
            request, message, kind=kind,
            model_name=model_label, model_id=pk if pk is not None else 0,
            payload=ujson.dumps(payload) if payload else None,
        )
    except Exception:
        logger.exception("Failed to write audit log entry")


def _report_security_event(category, level, details, user, model_name=None,
                           request=None, **extra):
    """Report a security event through the incident system.

    When ``request`` is provided (typically the synthetic request built by
    ``_build_request``), its ip is forwarded so the event records the
    originating source ip rather than defaulting to None.
    """
    from mojo.apps.incident import reporter
    if request is not None and "source_ip" not in extra:
        ip = getattr(request, "ip", None)
        if ip and ip != "assistant":
            extra["source_ip"] = ip
    reporter.report_event(
        details,
        title=details[:80],
        category=category,
        level=level,
        scope="assistant",
        uid=user.id if user else None,
        model_name=model_name,
        **extra,
    )


def _build_request(user, filters=None, method="GET", path="/assistant/query_model", request_meta=None):
    """Build a synthetic request object for permission checking.

    When ``request_meta`` is provided (an objict with ip, user_agent, path,
    method), its values seed the synthetic request so downstream incident
    events record the originating HTTP context instead of "assistant".
    """
    req = objict.objict()
    req.user = user
    req.DATA = objict.objict(filters or {})
    req.QUERY_PARAMS = objict.objict(filters or {})
    req.method = method
    req.group = None
    req.bearer = None
    req.ip = "assistant"
    req.path = path
    req.META = {}
    if request_meta is not None:
        if request_meta.get("ip"):
            req.ip = request_meta.ip
        if request_meta.get("user_agent"):
            req.META["HTTP_USER_AGENT"] = request_meta.user_agent
        if request_meta.get("path"):
            req.META["HTTP_HOST"] = ""
    if hasattr(user, "api_key"):
        req.api_key = user.api_key
    else:
        req.api_key = None
    return req


def _get_field_info(model):
    """Extract field metadata from a model, excluding sensitive fields."""
    fields = []
    for field in model._meta.get_fields():
        if not hasattr(field, "name"):
            continue
        if _is_sensitive_field(field.name):
            continue

        internal_type = getattr(field, "get_internal_type", lambda: "unknown")()
        info = {
            "name": field.name,
            "type": FIELD_TYPE_MAP.get(internal_type, internal_type),
        }

        if hasattr(field, "null"):
            info["nullable"] = field.null
        if hasattr(field, "choices") and field.choices:
            info["choices"] = [c[0] for c in field.choices]
        if hasattr(field, "related_model") and field.related_model:
            info["related_to"] = f"{field.related_model._meta.app_label}.{field.related_model.__name__}"

        fields.append(info)
    return fields


def _get_valid_field_names(model):
    """Get set of valid filterable field names for a model."""
    names = set()
    for field in model._meta.get_fields():
        if hasattr(field, "name"):
            names.add(field.name)
    return names


def _validate_filter_keys(filters, valid_fields, user, model_label):
    """Validate filter keys against model fields. Returns error dict or None."""
    # ORM lookup suffixes that are not field names
    ORM_SUFFIXES = {"in", "not", "not_in", "isnull", "gte", "gt", "lte", "lt",
                    "exact", "iexact", "contains", "icontains", "startswith",
                    "istartswith", "endswith", "iendswith", "range", "isnull",
                    "regex", "iregex", "date", "year", "month", "day", "hour",
                    "minute", "second", "week_day"}

    for key in filters:
        parts = key.split("__")
        base = parts[0]

        # Check every segment for sensitive content (blocks relational traversal)
        for segment in parts:
            if segment in ORM_SUFFIXES:
                continue
            if _is_sensitive_field(segment):
                details = f"Sensitive field filter attempt: {key} on {model_label} by user {user.id}"
                logger.warning(details)
                _report_security_event(
                    "assistant_sensitive_field",
                    7,
                    details,
                    user,
                    model_name=model_label,
                )
                return {"error": f"Filtering on '{segment}' is not allowed"}

        if base not in valid_fields:
            return {"error": f"Unknown field '{base}' on {model_label}"}

    return None


def _apply_owner_group_filter(model, request, queryset):
    """Apply owner/group filtering based on VIEW_PERMS, same as on_rest_handle_list."""
    perms = model.get_rest_meta_prop("VIEW_PERMS", [])
    if not perms:
        return queryset

    # If user has direct permission, no owner/group filtering needed
    if request.user.has_permission(perms):
        return queryset

    # Owner filtering
    if "owner" in perms and request.user.is_authenticated:
        owner_field = model.get_rest_meta_prop("OWNER_FIELD", "user")
        if owner_field == "self":
            return queryset.filter(pk=request.user.pk)
        return queryset.filter(**{owner_field: request.user})

    # Group filtering
    if request.user.is_authenticated and hasattr(model, "group"):
        groups_with_perms = request.user.get_groups_with_permission(perms)
        if groups_with_perms.exists():
            group_field = model.get_rest_meta_prop("GROUP_FIELD", "group")
            return queryset.filter(**{f"{group_field}__in": groups_with_perms})

    return queryset.none()


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

@tool(
    name="describe_model",
    domain="models",
    permission="view_admin",
    core=True,
    description=(
        "Describe a MojoModel's fields, available graphs, permissions, and search fields. "
        "Use this to discover what data is available before querying. "
        "Example: describe_model(app_name='account', model_name='User')"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "app_name": {
                "type": "string",
                "description": "Django app label (e.g. 'account', 'incident', 'jobs')",
            },
            "model_name": {
                "type": "string",
                "description": "Model class name (e.g. 'User', 'Incident', 'Job')",
            },
        },
        "required": ["app_name", "model_name"],
    },
)
def _tool_describe_model(params, user):
    """Describe a model's fields, graphs, and permissions."""
    app_name = params.get("app_name", "").strip()
    model_name = params.get("model_name", "").strip()

    if not app_name or not model_name:
        return {"error": "Both 'app_name' and 'model_name' are required"}

    model, err = _resolve_model(app_name, model_name)
    if err:
        return err

    err = _check_ai_access(model, "view", user)
    if err:
        return err

    logger.info("describe_model", app_name, model_name, f"user={user.id}")

    fields = _get_field_info(model)

    # Graphs
    graphs = {}
    raw_graphs = model.get_rest_meta_prop("GRAPHS", {})
    for name, graph in raw_graphs.items():
        graph_fields = graph.get("fields", [])
        # Filter out sensitive fields from graph info
        safe_fields = [f for f in graph_fields if not _is_sensitive_field(f)]
        graphs[name] = safe_fields

    # Permissions
    view_perms = model.get_rest_meta_prop("VIEW_PERMS", [])
    save_perms = model.get_rest_meta_prop("SAVE_PERMS", [])

    # Search fields
    search_fields = getattr(model.RestMeta, "SEARCH_FIELDS", None) or []

    return {
        "model": f"{app_name}.{model_name}",
        "fields": fields,
        "graphs": graphs,
        "permissions": {
            "view": view_perms,
            "save": save_perms,
        },
        "search_fields": search_fields,
    }


@tool(
    name="query_model",
    domain="models",
    permission="view_admin",
    core=True,
    description=(
        "Query any MojoModel and return results inline as JSON. "
        "Best for small result sets (detail lookups, spot-checking records). "
        "Respects RestMeta permissions and owner/group filtering. Max 200 rows. "
        "For CSV/file exports use export_data instead. "
        "For counts, sums, averages use aggregate_model instead. "
        "Use describe_model first to discover available fields and graphs. "
        "Example: query_model(app_name='account', model_name='User', "
        "filters={'is_active': true}, ordering='-created', limit=10)"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "app_name": {
                "type": "string",
                "description": "Django app label (e.g. 'account', 'incident', 'jobs')",
            },
            "model_name": {
                "type": "string",
                "description": "Model class name (e.g. 'User', 'Incident', 'Job')",
            },
            "filters": {
                "type": "object",
                "description": "ORM filter dict (e.g. {'status': 'active', 'created__gte': '2026-01-01'})",
            },
            "search": {
                "type": "string",
                "description": "Free-text search (uses model's SEARCH_FIELDS)",
            },
            "ordering": {
                "type": "string",
                "description": "Order by field, prefix with - for descending (e.g. '-created')",
            },
            "limit": {
                "type": "integer",
                "description": "Max results to return (default 50, max 200)",
            },
            "graph": {
                "type": "string",
                "description": "Serialization graph name (default 'default')",
            },
            "count_only": {
                "type": "boolean",
                "description": "If true, return only the count (no data)",
            },
        },
        "required": ["app_name", "model_name"],
    },
)
def _tool_query_model(params, user):
    """Query a model with filters, search, ordering, and format options."""
    app_name = params.get("app_name", "").strip()
    model_name = params.get("model_name", "").strip()

    if not app_name or not model_name:
        return {"error": "Both 'app_name' and 'model_name' are required"}

    model, err = _resolve_model(app_name, model_name)
    if err:
        return err

    model_label = f"{app_name}.{model_name}"

    # Build synthetic request for permission checking
    filters = params.get("filters") or {}
    request = _build_request(user, filters)

    err = _check_ai_access(model, "view", user, request=request)
    if err:
        return err

    # Permission check — model's own VIEW_PERMS
    if not model.rest_check_permission(request, "VIEW_PERMS"):
        details = f"Permission denied: query_model on {model_label} by user {user.id}"
        logger.warning(details)
        _report_security_event(
            "assistant_permission_denied",
            5,
            details,
            user,
            model_name=model_label,
        )
        return {"error": f"Permission denied for {model_label}"}

    # Validate filter keys
    valid_fields = _get_valid_field_names(model)
    filter_err = _validate_filter_keys(filters, valid_fields, user, model_label)
    if filter_err:
        return filter_err

    # Validate ordering field — must be a direct field, no relational traversals
    ordering = params.get("ordering", "").strip()
    if ordering:
        order_field = ordering.lstrip("-")
        if "__" in order_field:
            return {"error": f"Relational ordering is not supported"}
        if _is_sensitive_field(order_field):
            return {"error": f"Ordering on '{order_field}' is not allowed"}
        if order_field not in valid_fields:
            return {"error": f"Unknown ordering field '{order_field}' on {model_label}"}

    # Build queryset
    queryset = model.objects.all()

    # Apply owner/group filtering (same as REST layer)
    queryset = _apply_owner_group_filter(model, request, queryset)

    # Apply search if provided
    search = params.get("search", "").strip()
    if search:
        request.DATA["search"] = search
        queryset = model.on_rest_list_search(request, queryset)

    # Apply filters
    if filters:
        try:
            queryset = queryset.filter(**filters)
        except Exception as e:
            logger.warning("query_model filter error", model_label, str(e))
            return {"error": "Invalid filter parameters"}

    # Apply ordering
    if ordering:
        queryset = queryset.order_by(ordering)
    elif hasattr(model._meta, "ordering") and model._meta.ordering:
        pass  # Use model default
    else:
        queryset = queryset.order_by("-pk")

    # Limit
    limit = min(params.get("limit", DEFAULT_LIMIT), MAX_LIMIT)

    # Count only mode
    count_only = params.get("count_only", False)
    if count_only:
        count = queryset.count()
        logger.info("query_model", model_label, f"count_only={count}", f"user={user.id}")
        return {"model": model_label, "count": count}

    # Serialization
    graph = params.get("graph", "default").strip()
    results = model.queryset_to_dict(queryset[:limit], graph=graph)
    total = queryset.count()

    logger.info("query_model", model_label, f"results={len(results)}", f"total={total}", f"user={user.id}")

    return {
        "model": model_label,
        "results": results,
        "count": len(results),
        "total": total,
    }


@tool(
    name="delete_model_instance",
    domain="models",
    permission="view_admin",
    description=(
        "Delete a single model instance by primary key. "
        "Respects RestMeta permissions: the model must have CAN_DELETE=True and "
        "the user must pass the model's DELETE_PERMS/SAVE_PERMS/VIEW_PERMS chain. "
        "Use query_model or describe_model first to find the instance. "
        "IMPORTANT: Deletion is permanent and irreversible. Always confirm the "
        "exact instance (model, pk, name/title) with the user before executing. "
        "Never delete without explicit user approval."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "app_name": {
                "type": "string",
                "description": "Django app label (e.g. 'account', 'incident', 'jobs')",
            },
            "model_name": {
                "type": "string",
                "description": "Model class name (e.g. 'Skill', 'RuleSet', 'Conversation')",
            },
            "pk": {
                "type": "integer",
                "description": "Primary key of the instance to delete",
            },
        },
        "required": ["app_name", "model_name", "pk"],
    },
    mutates=True,
)
def _tool_delete_model_instance(params, user, *, request_meta=None, conversation=None):
    """Delete a model instance with full RestMeta permission checking."""
    import json

    app_name = params.get("app_name", "").strip()
    model_name = params.get("model_name", "").strip()
    pk = params.get("pk")

    if not app_name or not model_name or pk is None:
        return {"error": "'app_name', 'model_name', and 'pk' are all required"}

    model, err = _resolve_model(app_name, model_name)
    if err:
        return err

    model_label = f"{app_name}.{model_name}"

    # AI access gate — fails fast before any DB work
    err = _check_ai_access(model, "delete", user)
    if err:
        return err

    # Gate 1: CAN_DELETE must be True
    if not model.get_rest_meta_prop("CAN_DELETE", False):
        return {"error": f"Deletion is not allowed on {model_label}"}

    # Look up instance
    instance = model.objects.filter(pk=pk).first()
    if instance is None:
        return {"error": f"{model_label} with pk={pk} not found"}

    # Gate 2: full REST permission chain (DELETE_PERMS > SAVE_PERMS > VIEW_PERMS)
    request = _build_request(
        user, method="DELETE",
        path=f"/assistant/delete_model/{model_label}/{pk}",
        request_meta=request_meta,
    )
    if not model.rest_check_permission(request, ["DELETE_PERMS", "SAVE_PERMS", "VIEW_PERMS"], instance):
        details = f"Permission denied: delete_model_instance on {model_label} pk={pk} by user {user.id}"
        logger.warning(details)
        _report_security_event(
            "assistant_permission_denied",
            6,
            details,
            user,
            model_name=model_label,
            request=request,
        )
        return {"error": f"Permission denied to delete {model_label} pk={pk}"}

    # Gate 3: execute deletion via the model's own on_rest_delete (honors pre_delete hooks, atomic)
    response = instance.on_rest_delete(request)

    # Parse the JsonResponse into a dict
    try:
        result = json.loads(response.content)
    except Exception:
        result = {"status": "unknown"}

    if response.status_code >= 400:
        # Log the full error server-side but return a sanitized message to the LLM
        logger.warning("delete_model_instance error", model_label, f"pk={pk}",
                        f"status={response.status_code}", result.get("error", ""))
        return {"error": f"Delete failed for {model_label} pk={pk}"}

    logger.info("delete_model_instance", model_label, f"pk={pk}", f"user={user.id}")
    _audit_user_log(
        user, "assistant:model:deleted", "Deleted",
        model_label, pk, request=request, conversation=conversation,
    )
    return {"ok": True, "model": model_label, "pk": pk, "status": result.get("status", "deleted")}


# ---------------------------------------------------------------------------
# Save (create or update) a model instance
# ---------------------------------------------------------------------------

# Field names the underlying on_rest_save loop ignores; we use the same default
# to compute the audited field list.
_DEFAULT_NO_SAVE_FIELDS = {"id", "pk", "created", "uuid"}


def _changed_field_names(model, data):
    """Return the field names from `data` that on_rest_save will actually consider.

    Strips the model's NO_SAVE_FIELDS (or the framework default). Names only —
    values are never recorded in audit metadata.
    """
    no_save = set(model.get_rest_meta_prop("NO_SAVE_FIELDS", list(_DEFAULT_NO_SAVE_FIELDS)))
    return [k for k in data.keys() if k not in no_save]


@tool(
    name="save_model_instance",
    domain="models",
    permission="view_admin",
    mutates=True,
    description=(
        "Create or update a single MojoModel instance. "
        "Pass `pk` to update an existing row; omit `pk` to create a new one. "
        "Respects RestMeta permissions exactly like the REST API: creates require "
        "CAN_CREATE plus CREATE_PERMS/SAVE_PERMS/VIEW_PERMS; updates require "
        "SAVE_PERMS/VIEW_PERMS on the target instance. "
        "FK fields can be set by primary key in `data` (e.g. {\"group\": 5}); "
        "the related model's permissions are checked automatically. "
        "The save runs inside a transaction — partial failures roll back. "
        "Use describe_model first to discover available fields. "
        "IMPORTANT: This mutates data and is irreversible. Always confirm the "
        "exact instance, fields, and values with the user before executing. "
        "Never save without explicit user approval. "
        "Examples: "
        "Create: save_model_instance(app_name='assistant', model_name='Skill', "
        "data={'name': 'My Skill', 'description': '...'}). "
        "Update: save_model_instance(app_name='assistant', model_name='Skill', "
        "pk=42, data={'description': 'New description'})."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "app_name": {
                "type": "string",
                "description": "Django app label (e.g. 'account', 'incident', 'assistant')",
            },
            "model_name": {
                "type": "string",
                "description": "Model class name (e.g. 'User', 'Skill', 'Conversation')",
            },
            "pk": {
                "type": "integer",
                "description": "Primary key of an existing instance to update. Omit to create.",
            },
            "data": {
                "type": "object",
                "description": "Dict of field names to values. FK fields accept the related instance's pk.",
            },
        },
        "required": ["app_name", "model_name", "data"],
    },
)
def _tool_save_model_instance(params, user, *, request_meta=None, conversation=None):
    """Create or update a model instance with full RestMeta permission checking."""
    app_name = params.get("app_name", "").strip()
    model_name = params.get("model_name", "").strip()
    pk = params.get("pk")
    data = params.get("data")

    if not app_name or not model_name:
        return {"error": "Both 'app_name' and 'model_name' are required"}
    if not isinstance(data, dict):
        return {"error": "'data' must be an object of field/value pairs"}

    model, err = _resolve_model(app_name, model_name)
    if err:
        return err

    model_label = f"{app_name}.{model_name}"
    is_create = pk is None

    # AI access gate — fails fast before any DB work
    err = _check_ai_access(model, "create" if is_create else "update", user)
    if err:
        return err

    request = _build_request(
        user,
        filters=data,
        method="POST" if is_create else "PUT",
        path=f"/assistant/save_model/{model_label}" + ("" if is_create else f"/{pk}"),
        request_meta=request_meta,
    )

    if is_create:
        # Gate 1: CAN_CREATE flag
        if not model.get_rest_meta_prop("CAN_CREATE", True):
            return {"error": f"Creation is not allowed on {model_label}"}

        # Gate 2: full create permission chain
        if not model.rest_check_permission(request, ["CREATE_PERMS", "SAVE_PERMS", "VIEW_PERMS"]):
            details = f"Permission denied: save_model_instance create on {model_label} by user {user.id}"
            logger.warning(details)
            _report_security_event(
                "assistant_permission_denied", 6, details, user,
                model_name=model_label, request=request,
            )
            return {"error": f"Permission denied to create {model_label}"}

        instance = model()
    else:
        instance = model.objects.filter(pk=pk).first()
        if instance is None:
            return {"error": f"{model_label} with pk={pk} not found"}

        # Gate: update permission chain
        if not model.rest_check_permission(request, ["SAVE_PERMS", "VIEW_PERMS"], instance):
            details = (
                f"Permission denied: save_model_instance update on {model_label} pk={pk} "
                f"by user {user.id}"
            )
            logger.warning(details)
            _report_security_event(
                "assistant_permission_denied", 6, details, user,
                model_name=model_label, request=request,
            )
            return {"error": f"Permission denied to update {model_label} pk={pk}"}

    # Compute audit field names *before* the save, in case on_rest_save raises.
    # The underlying on_rest_save is NOT atomic today (rest.py:save_now calls
    # transaction.commit() directly, which forbids wrapping in transaction.atomic
    # here). Partial-failure recovery relies on the same behavior REST sees.
    fields = _changed_field_names(model, data)

    # Bind the synthetic request to ACTIVE_REQUEST so that field-level setters
    # (e.g. account.User.set_is_superuser, set_permissions) which gate themselves
    # on ``self.active_user`` see the assistant's user instead of None or a
    # stale request leaked from another thread in the executor pool.
    from mojo.models.rest import ACTIVE_REQUEST
    token = ACTIVE_REQUEST.set(request)
    try:
        try:
            action_resp = instance.on_rest_save(request, data)
        except Exception as e:
            logger.exception("save_model_instance error %s pk=%s", model_label, pk)
            _audit_user_log(
                user, "assistant:model:save_failed",
                f"Save failed ({type(e).__name__})",
                model_label, pk if pk is not None else 0,
                request=request, conversation=conversation, fields=fields,
            )
            return {"error": f"Save failed for {model_label}"}

        saved_pk = instance.pk
        kind = "assistant:model:created" if is_create else "assistant:model:updated"
        action = "Created" if is_create else "Updated"
        _audit_user_log(
            user, kind, action, model_label, saved_pk,
            request=request, conversation=conversation, fields=fields,
        )
        logger.info(
            "save_model_instance", model_label, f"pk={saved_pk}",
            f"created={is_create}", f"user={user.id}",
        )

        result = {
            "ok": True,
            "model": model_label,
            "pk": saved_pk,
            "created": is_create,
        }

        # POST_SAVE_ACTIONS may return a JsonResponse — surface its body inline
        if action_resp is not None:
            try:
                import json
                body = getattr(action_resp, "content", None)
                if body is not None:
                    result["action_response"] = json.loads(body)
                else:
                    result["action_response"] = action_resp
            except Exception:
                result["action_response"] = str(action_resp)

        return result
    finally:
        ACTIVE_REQUEST.reset(token)


# ---------------------------------------------------------------------------
# Aggregate functions map
# ---------------------------------------------------------------------------

AGGREGATE_FUNCS = {"count", "sum", "avg", "min", "max", "count_distinct"}

# Django field types that support sum/avg
NUMERIC_FIELD_TYPES = {
    "IntegerField", "BigIntegerField", "SmallIntegerField",
    "PositiveIntegerField", "PositiveSmallIntegerField", "PositiveBigIntegerField",
    "FloatField", "DecimalField", "AutoField", "BigAutoField", "SmallAutoField",
    "DurationField",
}

MAX_GROUP_ROWS = 200
DEFAULT_GROUP_LIMIT = 50


def _get_field_type(model, field_name):
    """Return the internal type string for a model field, or None if not found."""
    try:
        field = model._meta.get_field(field_name)
        return getattr(field, "get_internal_type", lambda: None)()
    except Exception:
        return None


@tool(
    name="aggregate_model",
    domain="models",
    permission="view_admin",
    core=True,
    description=(
        "Run aggregate queries (count, sum, avg, min, max) on any MojoModel. "
        "Supports group_by for grouped results (e.g. count by status). "
        "Use this for summaries — never pull rows just to count or sum them. "
        "For row-level data use query_model; for file exports use export_data."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "app_name": {
                "type": "string",
                "description": "Django app label (e.g. 'account', 'incident', 'jobs')",
            },
            "model_name": {
                "type": "string",
                "description": "Model class name (e.g. 'User', 'Incident', 'Job')",
            },
            "filters": {
                "type": "object",
                "description": "ORM filter dict (e.g. {'status': 'active', 'created__gte': '2026-01-01'})",
            },
            "aggregations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "field": {
                            "type": "string",
                            "description": "Field to aggregate (use 'id' for counting rows)",
                        },
                        "func": {
                            "type": "string",
                            "enum": ["count", "sum", "avg", "min", "max", "count_distinct"],
                            "description": "Aggregate function",
                        },
                        "alias": {
                            "type": "string",
                            "description": "Result key name (optional, auto-generated as func_field)",
                        },
                    },
                    "required": ["field", "func"],
                },
                "description": "List of aggregations to compute",
            },
            "group_by": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Fields to group by (e.g. ['status'] or ['status', 'category'])",
            },
            "ordering": {
                "type": "string",
                "description": "Order grouped results (e.g. '-total' or 'status')",
            },
            "limit": {
                "type": "integer",
                "description": "Max grouped rows to return (default 50, max 200)",
            },
        },
        "required": ["app_name", "model_name", "aggregations"],
    },
)
def _tool_aggregate_model(params, user):
    """Run aggregate queries on a model."""
    from django.db.models import Count, Sum, Avg, Min, Max

    app_name = params.get("app_name", "").strip()
    model_name = params.get("model_name", "").strip()

    if not app_name or not model_name:
        return {"error": "Both 'app_name' and 'model_name' are required"}

    model, err = _resolve_model(app_name, model_name)
    if err:
        return err

    model_label = f"{app_name}.{model_name}"

    # Permission check
    filters = params.get("filters") or {}
    request = _build_request(user, filters)

    err = _check_ai_access(model, "view", user, request=request)
    if err:
        return err

    if not model.rest_check_permission(request, "VIEW_PERMS"):
        details = f"Permission denied: aggregate_model on {model_label} by user {user.id}"
        logger.warning(details)
        _report_security_event("assistant_permission_denied", 5, details, user, model_name=model_label)
        return {"error": f"Permission denied for {model_label}"}

    # Validate filters
    valid_fields = _get_valid_field_names(model)
    if filters:
        filter_err = _validate_filter_keys(filters, valid_fields, user, model_label)
        if filter_err:
            return filter_err

    # Validate aggregations
    aggregations = params.get("aggregations") or []
    if not aggregations:
        return {"error": "At least one aggregation is required"}

    import re
    _ALIAS_RE = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*$')

    agg_map = {
        "count": lambda f: Count(f),
        "count_distinct": lambda f: Count(f, distinct=True),
        "sum": lambda f: Sum(f),
        "avg": lambda f: Avg(f),
        "min": lambda f: Min(f),
        "max": lambda f: Max(f),
    }

    aggs = {}
    for agg in aggregations:
        field = agg.get("field", "").strip()
        func = agg.get("func", "").strip().lower()
        alias = agg.get("alias", "").strip() or f"{func}_{field}"

        if not field or func not in AGGREGATE_FUNCS:
            return {"error": f"Invalid aggregation: field='{field}', func='{func}'"}

        if not _ALIAS_RE.match(alias):
            return {"error": f"Invalid alias '{alias}' — use letters, digits, underscores only"}

        if _is_sensitive_field(field):
            return {"error": f"Aggregation on '{field}' is not allowed"}

        if field not in valid_fields:
            return {"error": f"Unknown field '{field}' on {model_label}"}

        # Validate numeric requirement for sum/avg
        if func in ("sum", "avg"):
            field_type = _get_field_type(model, field)
            if field_type and field_type not in NUMERIC_FIELD_TYPES:
                return {"error": f"Cannot compute {func} on non-numeric field '{field}'"}

        aggs[alias] = agg_map[func](field)

    # Validate group_by
    group_by = params.get("group_by") or []
    for gb_field in group_by:
        if _is_sensitive_field(gb_field):
            return {"error": f"Cannot group by sensitive field '{gb_field}'"}
        if gb_field not in valid_fields:
            return {"error": f"Unknown group_by field '{gb_field}' on {model_label}"}

    # Build queryset
    queryset = model.objects.all()
    queryset = _apply_owner_group_filter(model, request, queryset)

    if filters:
        try:
            queryset = queryset.filter(**filters)
        except Exception as e:
            logger.warning("aggregate_model filter error", model_label, str(e))
            return {"error": "Invalid filter parameters"}

    # Execute
    if group_by:
        limit = min(params.get("limit", DEFAULT_GROUP_LIMIT), MAX_GROUP_ROWS)
        ordering = params.get("ordering", "").strip()

        # Validate ordering
        if ordering:
            order_field = ordering.lstrip("-")
            if "__" in order_field:
                return {"error": "Relational ordering is not supported"}
            if _is_sensitive_field(order_field):
                return {"error": f"Ordering on '{order_field}' is not allowed"}

        qs = queryset.values(*group_by).annotate(**aggs)
        if ordering:
            qs = qs.order_by(ordering)

        rows = list(qs[:limit])
        total_groups = qs.count()
        truncated = total_groups > limit

        # Convert any non-serializable values
        for row in rows:
            for key, val in row.items():
                if hasattr(val, "total_seconds"):
                    row[key] = val.total_seconds()
                elif val is not None and not isinstance(val, (str, int, float, bool)):
                    row[key] = str(val)

        logger.info("aggregate_model", model_label, f"group_by={group_by}",
                     f"groups={len(rows)}", f"user={user.id}")
        result = {
            "model": model_label,
            "group_by": group_by,
            "results": rows,
            "count": len(rows),
        }
        if truncated:
            result["truncated"] = True
            result["total_groups"] = total_groups
        return result
    else:
        # Flat aggregate — single result dict
        result = queryset.aggregate(**aggs)

        # Convert non-serializable values
        for key, val in result.items():
            if hasattr(val, "total_seconds"):
                result[key] = val.total_seconds()
            elif val is not None and not isinstance(val, (str, int, float, bool)):
                result[key] = str(val)

        logger.info("aggregate_model", model_label, f"flat aggs={list(result.keys())}",
                     f"user={user.id}")
        return {
            "model": model_label,
            "results": result,
        }


# ---------------------------------------------------------------------------
# Export data tool — CSV to file storage
# ---------------------------------------------------------------------------

DEFAULT_EXPORT_LIMIT = 5000
MAX_EXPORT_LIMIT = 50000


@tool(
    name="export_data",
    domain="models",
    permission="view_admin",
    core=True,
    description=(
        "Export query results to a downloadable CSV file stored in file storage (S3). "
        "Data is written directly to a file — NOT returned inline. "
        "Returns a download URL for the user. Use for any export request. "
        "For summaries (counts, sums, averages) use aggregate_model instead."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "app_name": {
                "type": "string",
                "description": "Django app label (e.g. 'account', 'incident', 'jobs')",
            },
            "model_name": {
                "type": "string",
                "description": "Model class name (e.g. 'User', 'Incident', 'Job')",
            },
            "filters": {
                "type": "object",
                "description": "ORM filter dict (e.g. {'status': 'active', 'created__gte': '2026-01-01'})",
            },
            "search": {
                "type": "string",
                "description": "Free-text search (uses model's SEARCH_FIELDS)",
            },
            "ordering": {
                "type": "string",
                "description": "Order by field, prefix with - for descending (e.g. '-created')",
            },
            "limit": {
                "type": "integer",
                "description": "Max rows to export (default 5000, max 50000)",
            },
            "fields": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Specific fields to include (optional, defaults to model's graph config)",
            },
            "graph": {
                "type": "string",
                "description": "Serialization graph name for field config (default 'default')",
            },
        },
        "required": ["app_name", "model_name"],
    },
    mutates=True,
)
def _tool_export_data(params, user):
    """Export query results to a CSV file in storage, return download URL."""
    import io
    from datetime import timedelta
    from django.utils import timezone
    from mojo.helpers.settings import settings

    app_name = params.get("app_name", "").strip()
    model_name = params.get("model_name", "").strip()

    if not app_name or not model_name:
        return {"error": "Both 'app_name' and 'model_name' are required"}

    model, err = _resolve_model(app_name, model_name)
    if err:
        return err

    model_label = f"{app_name}.{model_name}"

    # Permission check
    filters = params.get("filters") or {}
    request = _build_request(user, filters)

    err = _check_ai_access(model, "view", user, request=request)
    if err:
        return err

    if not model.rest_check_permission(request, "VIEW_PERMS"):
        details = f"Permission denied: export_data on {model_label} by user {user.id}"
        logger.warning(details)
        _report_security_event("assistant_permission_denied", 5, details, user, model_name=model_label)
        return {"error": f"Permission denied for {model_label}"}

    # Validate filters
    valid_fields = _get_valid_field_names(model)
    if filters:
        filter_err = _validate_filter_keys(filters, valid_fields, user, model_label)
        if filter_err:
            return filter_err

    # Validate ordering
    ordering = params.get("ordering", "").strip()
    if ordering:
        order_field = ordering.lstrip("-")
        if "__" in order_field:
            return {"error": "Relational ordering is not supported"}
        if _is_sensitive_field(order_field):
            return {"error": f"Ordering on '{order_field}' is not allowed"}
        if order_field not in valid_fields:
            return {"error": f"Unknown ordering field '{order_field}' on {model_label}"}

    # Build queryset
    queryset = model.objects.all()
    queryset = _apply_owner_group_filter(model, request, queryset)

    # Search
    search = params.get("search", "").strip()
    if search:
        request.DATA["search"] = search
        queryset = model.on_rest_list_search(request, queryset)

    # Filters
    if filters:
        try:
            queryset = queryset.filter(**filters)
        except Exception as e:
            logger.warning("export_data filter error", model_label, str(e))
            return {"error": "Invalid filter parameters"}

    # Ordering
    if ordering:
        queryset = queryset.order_by(ordering)
    elif hasattr(model._meta, "ordering") and model._meta.ordering:
        pass
    else:
        queryset = queryset.order_by("-pk")

    # Limit
    limit = min(params.get("limit", DEFAULT_EXPORT_LIMIT), MAX_EXPORT_LIMIT)
    export_qs = queryset[:limit]

    # Resolve FileManager
    from mojo.apps.fileman.models import FileManager
    group = getattr(user, "group", None)
    if not group:
        membership = getattr(user, "membership", None)
        if membership:
            group = getattr(membership, "group", None)

    try:
        fm = FileManager.get_for_user_group(user=user, group=group)
    except Exception:
        fm = None

    if not fm:
        return {"error": "No file storage configured. Contact your administrator."}

    # Generate CSV
    custom_fields = params.get("fields")
    if custom_fields:
        for f in custom_fields:
            if _is_sensitive_field(f):
                return {"error": f"Field '{f}' is not allowed in exports"}
    try:
        if custom_fields:
            # Use CsvFormatter directly with custom fields
            from mojo.serializers.core.manager import get_serializer_manager
            manager = get_serializer_manager()
            serializer = manager.get_format_serializer("csv")
            csv_data = serializer.serialize_queryset(
                export_qs, fields=custom_fields, raw_data=True,
            )
        else:
            csv_data = model.to_csv(export_qs, format="csv")
    except Exception as e:
        logger.warning("export_data csv error", model_label, str(e))
        return {"error": "CSV generation failed"}

    # Count rows (header line excluded)
    row_count = csv_data.count("\n") - 1 if csv_data.strip() else 0
    if row_count < 0:
        row_count = 0

    # Build file-like object
    date_str = timezone.now().strftime("%Y-%m-%d")
    filename = f"export_{app_name}_{model_name}_{date_str}.csv"
    csv_bytes = csv_data.encode("utf-8")

    file_obj = io.BytesIO(csv_bytes)
    file_obj.name = filename
    file_obj.size = len(csv_bytes)
    file_obj.content_type = "text/csv"

    # Create File record
    from mojo.apps.fileman.models import File
    try:
        file_instance = File.create_from_file(
            file_obj, filename, user=user, group=group, file_manager=fm,
        )
    except Exception as e:
        logger.error("export_data file creation error", model_label, str(e))
        return {"error": "Failed to save export file"}

    # Set metadata with expiration
    expire_days = settings.get("FILEMAN_EXPORT_EXPIRES_DAYS", 14)
    expires_at = timezone.now() + timedelta(days=expire_days)
    file_instance.metadata = {
        "source": "assistant_export",
        "model": model_label,
        "row_count": row_count,
        "expires_at": expires_at.isoformat(),
    }
    file_instance.save()

    # Generate download URL — use shortlink if available
    url = _get_export_url(file_instance, user, group, expire_days)

    logger.info("export_data", model_label, f"rows={row_count}", f"size={len(csv_bytes)}",
                f"file_id={file_instance.pk}", f"user={user.id}")

    return {
        "url": url,
        "filename": filename,
        "size": len(csv_bytes),
        "row_count": row_count,
        "model": model_label,
        "expires_in": f"{expire_days} days",
    }


def _get_export_url(file_instance, user, group, expire_days):
    """Generate a download URL, wrapping in a shortlink if available."""
    from django.apps import apps

    if apps.is_installed("mojo.apps.shortlink"):
        try:
            from mojo.apps.shortlink import shorten
            return shorten(
                file=file_instance,
                source="assistant_export",
                expire_days=expire_days,
                resolve_file=True,
                user=user,
                group=group,
                bot_passthrough=True,
            )
        except Exception as e:
            logger.warning("export_data shortlink error, falling back to direct URL", str(e))

    return file_instance.generate_download_url()
