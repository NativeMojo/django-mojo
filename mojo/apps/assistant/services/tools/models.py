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


def _report_security_event(category, level, details, user, model_name=None, **extra):
    """Report a security event through the incident system."""
    from mojo.apps.incident import reporter
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


def _build_request(user, filters=None, method="GET", path="/assistant/query_model"):
    """Build a synthetic request object for permission checking."""
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
        "Query any MojoModel with filters, search, ordering, and format options. "
        "Respects RestMeta permissions and owner/group filtering. "
        "Supports JSON and CSV output, count-only mode, and configurable limits (max 200). "
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
            "format": {
                "type": "string",
                "enum": ["json", "csv"],
                "description": "Output format (default 'json')",
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

    # Format
    fmt = params.get("format", "json").strip().lower()
    graph = params.get("graph", "default").strip()

    if fmt == "csv":
        try:
            csv_data = model.to_csv(queryset[:limit], format="csv")
            logger.info("query_model", model_label, f"csv rows={limit}", f"user={user.id}")
            return {
                "model": model_label,
                "format": "csv",
                "content": csv_data,
                "count": queryset.count(),
            }
        except Exception as e:
            logger.warning("query_model csv error", model_label, str(e))
            return {"error": "CSV export failed"}

    # JSON (default)
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
def _tool_delete_model_instance(params, user):
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

    # Gate 1: CAN_DELETE must be True
    if not model.get_rest_meta_prop("CAN_DELETE", False):
        return {"error": f"Deletion is not allowed on {model_label}"}

    # Look up instance
    instance = model.objects.filter(pk=pk).first()
    if instance is None:
        return {"error": f"{model_label} with pk={pk} not found"}

    # Gate 2: full REST permission chain (DELETE_PERMS > SAVE_PERMS > VIEW_PERMS)
    request = _build_request(user, method="DELETE", path=f"/assistant/delete_model/{model_label}/{pk}")
    if not model.rest_check_permission(request, ["DELETE_PERMS", "SAVE_PERMS", "VIEW_PERMS"], instance):
        details = f"Permission denied: delete_model_instance on {model_label} pk={pk} by user {user.id}"
        logger.warning(details)
        _report_security_event(
            "assistant_permission_denied",
            6,
            details,
            user,
            model_name=model_label,
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
        logger.warning("delete_model_instance error", model_label, f"pk={pk}", f"status={response.status_code}")
        return {"error": result.get("error", f"Delete failed for {model_label} pk={pk}")}

    logger.info("delete_model_instance", model_label, f"pk={pk}", f"user={user.id}")
    return {"ok": True, "model": model_label, "pk": pk, "status": result.get("status", "deleted")}
