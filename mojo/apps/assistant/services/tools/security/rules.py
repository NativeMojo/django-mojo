"""Governed, redacted RuleSet tools for the Assistant."""

from mojo.apps.assistant import CONFIGURED_FRESH_AUTH, tool


# Resolve the deployment's FRESH_AUTH_WINDOW when an approval is proposed and
# again when it executes. Default 0 keeps the gate explicitly disabled.
FRESH_AUTH = CONFIGURED_FRESH_AUTH
WRITE_PERMS = ["manage_security", "security"]
MAX_RESULTS = 50


def _service():
    from mojo.apps.incident.services import admin_security
    return admin_security


def _ruleset(pk):
    from django.db.models import Prefetch
    from mojo.apps.incident.models import Rule, RuleSet
    return RuleSet.objects.prefetch_related(Prefetch(
        "rules", queryset=Rule.objects.order_by("index", "id"),
        to_attr="_admin_security_rules")).filter(pk=pk).first()


def _revision(row):
    return row.modified.isoformat()


def _refuse(message, code="action_refused"):
    return {"error": message, "error_code": code}


def _actor_context(user, request_meta=None, **_ignored):
    """Bounded action attribution from server-built Assistant metadata."""
    kinds = {"user", "user_api_key", "oauth", "api_key", "group_token",
             "internal", "unknown"}
    kind = request_meta.get("credential_kind") if request_meta is not None else None
    if kind not in kinds:
        kind = "unknown"
    value = {"credential_kind": kind, "user_id": getattr(user, "pk", None)}
    if kind == "user_api_key":
        key_id = request_meta.get("user_api_key_id")
        label = request_meta.get("user_api_key_label")
        if isinstance(key_id, int) and not isinstance(key_id, bool) and key_id > 0:
            value["user_api_key_id"] = key_id
        if isinstance(label, str):
            value["user_api_key_label"] = label[:160]
    return value


def _invoke(payload, user, request_meta=None):
    try:
        return _service().apply_action(
            payload, user, actor_context=_actor_context(user, request_meta))
    except Exception as error:
        from mojo.apps.incident.services.admin_security import SecurityActionError
        if isinstance(error, SecurityActionError):
            return _refuse(str(error), error.code)
        raise


@tool(
    name="query_rulesets", domain="security", permission="view_security",
    description="List bounded, redacted rule policy summaries.",
    input_schema={"type": "object", "properties": {
        "category": {"type": "string"}, "is_active": {"type": "boolean"},
        "limit": {"type": "integer", "minimum": 1, "maximum": MAX_RESULTS}}})
def _tool_query_rulesets(params, user):
    from django.db.models import Count
    from mojo.apps.incident.models import RuleSet
    criteria = {}
    if params.get("category"):
        criteria["category"] = params["category"]
    if params.get("is_active") is not None:
        criteria["is_active"] = params["is_active"]
    limit = params.get("limit", MAX_RESULTS)
    if isinstance(limit, bool) or not isinstance(limit, int):
        return _refuse("limit must be an integer", "invalid_input")
    limit = max(1, min(limit, MAX_RESULTS))
    # The discovery shape only needs a count. Loading every condition here
    # makes one old, large compatibility RuleSet dominate the list request.
    queryset = RuleSet.objects.filter(**criteria).annotate(
        _admin_security_rule_count=Count("rules"))
    return [_service()._safe_rule_set(row) for row in
            queryset.order_by("priority", "id")[:limit]]


@tool(
    name="get_ruleset", domain="security", permission="view_security",
    description="Get one redacted rule policy and its typed governed fields.",
    input_schema={"type": "object", "properties": {
        "ruleset_id": {"type": "integer"}}, "required": ["ruleset_id"]})
def _tool_get_ruleset(params, user):
    row = _ruleset(params.get("ruleset_id"))
    if row is None:
        return _refuse("RuleSet not found", "not_found")
    return _service()._safe_rule_set(row, detail=True)


RULESET_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "maxLength": 160},
        "category": {"type": "string", "maxLength": 124},
        "priority": {"type": "integer", "minimum": 0, "maximum": 10000},
        "bundle_minutes": {"type": ["integer", "null"]},
        "bundle_by": {"type": "integer", "minimum": 0, "maximum": 13},
        "bundle_by_rule_set": {"type": "boolean"},
        "match_by": {"type": "integer", "enum": [0, 1]},
        "trigger_count": {"type": ["integer", "null"]},
        "trigger_window": {"type": ["integer", "null"]},
        "retrigger_every": {"type": ["integer", "null"]},
        "delete_on_resolution": {"type": "boolean"},
        "is_active": {"type": "boolean"},
        "handlers": {"type": "array", "maxItems": 8, "items": {
            "type": "object", "description": (
                "A server-allowlisted typed handler. Read the security schema "
                "for allowed types and arguments; job/Python/LLM handlers are forbidden.")}},
        "rules": {"type": "array", "maxItems": 32, "items": {
            "type": "object", "properties": {
                "name": {"type": "string"}, "field": {"type": "string"},
                "operator": {"type": "string"}, "value": {},
                "value_type": {"type": "string"},
                "is_required": {"type": "boolean"}},
            "required": ["field", "operator", "value"]}},
    },
    "required": ["name", "category", "handlers", "rules"],
}


def _preview_create(params, user):
    from mojo.apps.incident.services import rule_validation
    normalized = rule_validation.normalize_ruleset(dict(params.get("ruleset") or {}))
    return {"summary": "Create an inactive governed RuleSet.",
            "details": {"name": normalized["name"],
                        "category": normalized["category"],
                        "rule_count": len(normalized["rules"])},
            "revision": "new-ruleset-v1"}


@tool(
    name="create_rule", domain="security", permission=WRITE_PERMS,
    description=("Create a validated inactive RuleSet proposal. Requires an "
                 "operator approval card and fresh authentication."),
    input_schema={"type": "object", "properties": {
        "ruleset": RULESET_SCHEMA,
        "confirm": {"type": "string", "enum": ["CREATE RULESET"]}},
        "required": ["ruleset", "confirm"]},
    mutates=True, fresh_auth_seconds=FRESH_AUTH, preview=_preview_create)
def _tool_create_rule(params, user, approval=None, *, request_meta=None):
    payload = dict(params)
    payload["action"] = "ruleset.create"
    return _invoke(payload, user, request_meta=request_meta)


def _preview_existing(params, user):
    from mojo.apps.incident.services import rule_validation

    row = _ruleset(params.get("ruleset_id"))
    if row is None:
        raise ValueError("RuleSet not found")
    if params.get("expected_modified") != _revision(row):
        raise ValueError("RuleSet changed; reload it before approval")
    if params.get("action") == "replace":
        replacement = dict(params.get("ruleset") or {})
        if replacement.get("is_active") is not False:
            raise ValueError("Replacement RuleSet must be inactive")
        rule_validation.normalize_ruleset(replacement)
    elif params.get("action") == "activate":
        rule_validation.validate_existing(row)
    return {"summary": f"Apply {params.get('action', 'delete')} to RuleSet {row.pk}.",
            "details": _service()._safe_rule_set(row),
            "revision": _revision(row)}


@tool(
    name="update_ruleset", domain="security", permission=WRITE_PERMS,
    description=("Fully replace, activate, or deactivate a RuleSet through the "
                 "versioned Admin Security authority. Partial updates are refused."),
    input_schema={"type": "object", "properties": {
        "ruleset_id": {"type": "integer"},
        "action": {"type": "string", "enum": ["replace", "activate", "deactivate"]},
        "expected_modified": {"type": "string"}, "confirm": {"type": "string"},
        "confirm_catch_all": {"type": "string"}, "ruleset": RULESET_SCHEMA},
        "required": ["ruleset_id", "action", "expected_modified", "confirm"]},
    mutates=True, fresh_auth_seconds=FRESH_AUTH, preview=_preview_existing)
def _tool_update_ruleset(params, user, approval=None, *, request_meta=None):
    payload = dict(params)
    payload["action"] = f"ruleset.{params.get('action')}"
    return _invoke(payload, user, request_meta=request_meta)


@tool(
    name="delete_ruleset", domain="security", permission=WRITE_PERMS,
    description="Delete a version-bound RuleSet after fresh operator approval.",
    input_schema={"type": "object", "properties": {
        "ruleset_id": {"type": "integer"}, "expected_modified": {"type": "string"},
        "confirm": {"type": "string"}},
        "required": ["ruleset_id", "expected_modified", "confirm"]},
    mutates=True, fresh_auth_seconds=FRESH_AUTH, preview=_preview_existing)
def _tool_delete_ruleset(params, user, approval=None, *, request_meta=None):
    payload = dict(params)
    payload["action"] = "ruleset.delete"
    return _invoke(payload, user, request_meta=request_meta)


def _retired_partial(params, user, approval=None):
    return _refuse(
        "Partial rule mutations are retired. Read the current RuleSet and use "
        "update_ruleset with a complete inactive replacement and its revision.",
        "full_replacement_required")


@tool(
    name="add_rule_condition", domain="security", permission=WRITE_PERMS,
    description="Retired: use update_ruleset with a complete inactive replacement.",
    input_schema={"type": "object", "properties": {
        "ruleset_id": {"type": "integer"}}, "required": ["ruleset_id"]},
    mutates=True, fresh_auth_seconds=FRESH_AUTH)
def _tool_add_rule_condition(params, user, approval=None):
    return _retired_partial(params, user, approval)


@tool(
    name="delete_rule", domain="security", permission=WRITE_PERMS,
    description="Retired: use update_ruleset with a complete inactive replacement.",
    input_schema={"type": "object", "properties": {
        "rule_id": {"type": "integer"}}, "required": ["rule_id"]},
    mutates=True, fresh_auth_seconds=FRESH_AUTH)
def _tool_delete_rule(params, user, approval=None):
    return _retired_partial(params, user, approval)


def _recommendation(pk):
    from mojo.apps.incident.models import MojoSecRecommendation
    return MojoSecRecommendation.objects.filter(pk=pk).first()


def _preview_recommendation(params, user):
    row = _recommendation(params.get("recommendation_id"))
    if row is None:
        raise ValueError("Recommendation not found")
    if params.get("expected_modified") != row.modified.isoformat():
        raise ValueError("Recommendation changed; reload it before approval")
    actual_targets = row.targets.count()
    if (actual_targets != row.target_count or
            actual_targets > _service().MAX_ACTION_TARGETS):
        raise ValueError(
            "Recommendation scope is inconsistent or too large to review")
    return {
        "summary": f"{params.get('action')} recommendation {row.pk} exactly as proposed.",
        "details": _service()._safe_recommendation(row, detail=True),
        "revision": row.modified.isoformat(),
    }


@tool(
    name="manage_security_recommendation", domain="security",
    permission=WRITE_PERMS,
    description=("Approve, reject, cancel, or reverse exactly one bounded "
                 "MojoSec recommendation. Requires fresh operator approval."),
    input_schema={"type": "object", "properties": {
        "recommendation_id": {"type": "integer"},
        "action": {"type": "string",
                   "enum": ["approve", "reject", "cancel", "reverse"]},
        "expected_modified": {"type": "string"},
        "confirm": {"type": "string"},
        "note": {"type": "string", "maxLength": 256}},
        "required": ["recommendation_id", "action", "expected_modified", "confirm"]},
    mutates=True, fresh_auth_seconds=FRESH_AUTH,
    preview=_preview_recommendation)
def _tool_manage_security_recommendation(
        params, user, approval=None, *, request_meta=None):
    payload = dict(params)
    payload["action"] = f"recommendation.{params.get('action')}"
    return _invoke(payload, user, request_meta=request_meta)
