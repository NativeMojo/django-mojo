"""Deterministic, redacted Admin Security preview authority."""

from copy import deepcopy
from urllib.parse import parse_qs


NAME = "security"
SCHEMA_VERSION = 2
NOW = "2026-08-10T17:10:00Z"
WINDOW = {"hours": 24, "start": "2026-08-09T17:10:00Z", "end": NOW}


def describe(capabilities):
    values = {
        "view": capabilities["view_security"],
        "manage": capabilities["manage_security"],
    }
    return {"id": NAME, "enabled": values["view"], "capabilities": values}


def reset(handler, fixtures, *, security_state="full", **options):
    handler.security_state = security_state
    handler.security_reads = 0
    handler.security_actions = 0


def _owner(handler):
    return handler if isinstance(handler, type) else type(handler)


def _envelope(data, status="available", reason=None):
    value = {
        "status": status, "observed_at": NOW, "cutoff": NOW,
        "window": WINDOW, "truncated": False, "data": data,
    }
    if reason:
        value["reason"] = reason
    return value


CASES = [
    {"id": 701, "created": "2026-08-10T15:00:00Z",
     "first_seen": "2026-08-10T15:00:00Z", "last_seen": NOW,
     "sensor_kind": "auth", "resource_id": "login", "family": "credential",
     "state": "learning", "urgency": "high", "occurrence_count": 12,
     "receipt_count": 12, "projected_event_count": 3, "distinct_count": 4,
     "sample_count": 3, "overflow_count": 9, "distinct_source_count": 4,
     "policy_version": 1, "evaluator_version": 1,
     "accuracy": "sampled_learning_projection"},
]
INCIDENTS = [
    {"id": 301, "created": "2026-08-10T16:20:00Z", "priority": 9,
     "state": "open", "status": "open", "scope": "global",
     "category": "auth:failures", "group_id": 9, "rule_set_id": 801},
]
EVENTS = [
    {"id": 401, "created": "2026-08-10T16:19:00Z", "level": 9,
     "scope": "global", "category": "invalid_password", "country_code": "US",
     "incident_id": 301, "group_id": 9},
]
RULES = [
    {"id": 801, "created": "2026-08-01T10:00:00Z", "modified": NOW,
     "name": "Repeated authentication failures", "category": "auth:*",
     "priority": 10, "is_active": True, "bundle_minutes": 30,
     "bundle_by": 1, "bundle_by_rule_set": True, "match_by": 1,
     "trigger_count": 5, "trigger_window": 10, "retrigger_every": 5,
     "rule_count": 2, "validation": {"status": "valid", "legacy": False,
       "handlers": [{"type": "notify", "permission": "manage_security"}]}},
]
IPSETS = [
    {"id": 901, "created": "2026-08-01T11:00:00Z", "modified": NOW,
     "name": "hostile_sources", "kind": "block", "description": "Reviewed sources",
     "is_enabled": True, "cidr_count": 37, "last_synced": NOW,
     "has_sync_error": False, "enforcement_status": "verified", "enforcement_ok": True,
     "enforcement": {"status": "verified", "desired": {"present": True, "count": 37,
       "digest": "0" * 64}, "observed": "verified", "generation": 14,
       "observation_cutoff": NOW, "expected_host_ids": ["edge-a", "edge-b"],
       "responded_host_ids": ["edge-a", "edge-b"],
       "succeeded_host_ids": ["edge-a", "edge-b"], "failed_host_ids": [],
       "missing_host_ids": []}},
]
RECOMMENDATIONS = [
    {"id": 1001, "created": "2026-08-10T16:30:00Z", "modified": NOW,
     "case_id": 701, "action": "block", "state": "proposed",
     "reason_code": "repeated_auth_failures", "confidence": "high", "urgency": "high",
     "requested_scope": "temporary", "requested_ttl_seconds": 3600,
     "expires_at": "2026-08-10T18:10:00Z", "approved_at": None,
     "target_count": 2, "validated_count": 2, "protected_count": 0,
     "executed_count": 0, "failed_count": 0, "reversed_count": 0,
     "policy_version": 1, "evaluator_version": 1},
]


def _policy_schema():
    properties = {
        "name": {"type": "string", "min_length": 1, "max_length": 160},
        "category": {"type": "string", "min_length": 1, "max_length": 124},
        "priority": {"type": "integer", "minimum": 0, "maximum": 10000, "default": 50},
        "bundle_minutes": {"type": ["integer", "null"], "minimum": 0, "maximum": 10080, "default": 30},
        "bundle_by": {"type": "integer", "enum": [1, 2, 3], "default": 1},
        "bundle_by_rule_set": {"type": "boolean", "default": True},
        "match_by": {"type": "integer", "enum": [1, 2], "default": 1},
        "trigger_count": {"type": ["integer", "null"], "minimum": 1, "maximum": 1000000, "default": None},
        "trigger_window": {"type": ["integer", "null"], "minimum": 1, "maximum": 10080, "default": None},
        "retrigger_every": {"type": ["integer", "null"], "minimum": 1, "maximum": 1000000, "default": None},
        "handlers": {"type": "array", "max_items": 8, "default": []},
        "rules": {"type": "array", "max_items": 32, "default": []},
        "delete_on_resolution": {"type": "boolean", "default": False},
        "is_active": {"type": "boolean", "default": False},
    }
    return {"schema_version": 1, "aggregate": {"type": "object",
        "additional_properties": False, "required": ["name", "category"],
        "properties": properties}, "limits": {"rules": 32, "handlers": 8}}


def _action_schema(action):
    noun, verb = action.split(".")
    identity = {"ruleset": "ruleset_id", "recommendation": "recommendation_id",
                "ipset": "ipset_id"}.get(noun)
    if action == "ruleset.create":
        required = ["action", "confirm", "ruleset"]
        properties = {"confirm": {"type": "string", "min_length": 1,
                                  "max_length": 128},
                      "ruleset": {"$ref": "rule_policy.aggregate"}}
        value = "CREATE RULESET"
    else:
        required = ["action", identity, "expected_modified", "confirm"]
        properties = {identity: {"type": "integer", "minimum": 1,
                                 "maximum": 2147483647},
                      "expected_modified": {"type": "string", "format": "date-time", "max_length": 64},
                      "confirm": {"type": "string", "min_length": 1,
                                  "max_length": 128}}
        value = f"{verb.upper()} {noun.upper()} {{id}}"
        if action == "ruleset.replace":
            required.append("ruleset")
            properties["ruleset"] = {"$ref": "rule_policy.aggregate"}
        if action == "ruleset.activate":
            properties["confirm_catch_all"] = {
                "type": "string", "max_length": 128,
                "required_when": "ruleset.rules is empty"}
        if noun == "recommendation":
            properties["note"] = {"type": "string", "max_length": 256, "default": ""}
    confirmation = {"kind": "exact" if action == "ruleset.create" else "template",
                    "value": value}
    if action == "ruleset.activate":
        confirmation["catch_all_value"] = "ACTIVATE CATCH-ALL RULESET {id}"
    return {"type": "object", "additional_properties": False,
            "required": required, "properties": {"action": {"type": "string", "const": action},
            **properties}, "confirmation": confirmation}


ACTIONS = tuple(
    [f"ruleset.{verb}" for verb in ("create", "replace", "activate", "deactivate", "delete")]
    + [f"recommendation.{verb}" for verb in ("approve", "reject", "cancel", "reverse")]
    + [f"ipset.{verb}" for verb in ("sync", "enable", "disable")])


def _sections(state):
    empty = state == "empty"
    cases = [] if empty else [
        {**deepcopy(CASES[0]), "id": 701 + index,
         "resource_id": f"login-{index + 1}"}
        for index in range(12)]
    data = {
        "overview": {"current": {"open_incidents": 0 if empty else 1,
          "active_rule_sets": 0 if empty else 1, "pending_recommendations": 0 if empty else 1},
          "recommendation_transitions": {}, "accuracy": {"current": "exact_current_rows",
          "recommendation_transitions": "exact_append_only_transitions", "resolution_rate": "unavailable"},
          "unavailable": {"resolution_rate": "immutable_resolution_history_unavailable"},
          "metric_definitions": {
            "open_incidents": {"source": "incident.Incident current rows",
                               "accuracy": "exact", "window": "current"},
            "active_rule_sets": {"source": "incident.RuleSet current rows",
                                 "accuracy": "exact", "window": "current"},
            "pending_recommendations": {
              "source": "incident.MojoSecRecommendation current rows",
              "accuracy": "exact", "window": "current"},
            "recommendation_transitions": {
              "source": "incident.MojoSecRecommendationTransition append-only rows",
              "accuracy": "exact", "window": WINDOW},
            "case_learning": {"source": "bounded MojoSecCase projections",
                              "accuracy": "sampled", "window": WINDOW},
            "resolution_rate": {"source": None, "accuracy": "unavailable",
                                "window": WINDOW}}},
        "cases": cases,
        "incidents": [] if empty else deepcopy(INCIDENTS),
        "events": [] if empty else deepcopy(EVENTS),
        "rules": [] if empty else deepcopy(RULES),
        "ipsets": [] if empty else deepcopy(IPSETS),
        "recommendations": [] if empty else deepcopy(RECOMMENDATIONS),
        "schemas": {"rule_policy": _policy_schema(),
                    "actions": {name: _action_schema(name) for name in ACTIONS},
                    "action_names": list(ACTIONS)},
    }
    return data


def get(handler, parsed):
    if parsed.path.rstrip("/") != "/api/incident/admin/security":
        return None
    state = handler.security_state
    if state == "no-access":
        return 403, {"error": "Security access is unavailable"}
    if state == "expired-session":
        return 401, {"error": "Admin session expired", "error_code": "session_expired"}
    owner = _owner(handler)
    owner.security_reads += 1
    recovering = state == "recovery" and owner.security_reads == 1
    data = _sections("empty" if state == "empty" else "full")
    query = parse_qs(parsed.query)
    wanted = query.get("sections", ["overview"])[0].split(",")
    ruleset_id = query.get("ruleset_id", [None])[0]
    sections = {}
    for name in wanted:
        if name not in data:
            continue
        status = "available"
        reason = None
        if state in ("unavailable", "failed") or recovering:
            status = "failed" if state == "failed" else "unavailable"
            reason = "deterministic_preview_state"
        elif state == "stale":
            status = "stale"
        elif state == "partial" and name == "ipsets":
            status = "partial"
            truth = data[name][0]["enforcement"]
            truth.update(status="missing", observed="missing",
                         succeeded_host_ids=["edge-a"], missing_host_ids=["edge-b"])
            data[name][0].update(enforcement_status="missing", enforcement_ok=False,
                                 error_code="host_observation_missing")
        elif state == "malformed":
            if name == "overview":
                data[name] = {"current": {"open_incidents": "one"}}
            elif name == "schemas":
                data[name] = {"rule_policy": {}, "actions": [],
                              "action_names": list(ACTIONS)}
            else:
                data[name] = {"not": "a bounded row array"}
        if name == "rules" and ruleset_id is not None:
            data[name] = [row for row in data[name]
                          if str(row["id"]) == str(ruleset_id)]
            for row in data[name]:
                row.pop("rule_count", None)
                row.update(
                    handlers=[{"type": "notify", "permission": "manage_security"}],
                    rules=[{"name": "serious", "field": "level",
                            "operator": ">=", "value": 8,
                            "value_type": "int"}],
                    delete_on_resolution=False)
        sections[name] = _envelope({} if status in ("unavailable", "failed") else data[name],
                                   status=status, reason=reason)
    return 200, {"schema_version": SCHEMA_VERSION, "sections": sections}


def post(handler, path, payload):
    if path.rstrip("/") != "/api/incident/admin/security/action":
        return None
    owner = _owner(handler)
    owner.security_actions += 1
    state = handler.security_state
    if state == "expired-session":
        return 401, {"error": "Admin session expired", "error_code": "session_expired"}
    if state == "440" and owner.security_actions == 1:
        return 440, {"error": "Recent authentication required", "error_code": "fresh_auth_required"}
    if state == "conflict":
        return 409, {"error": "Security state changed", "error_code": "stale_revision"}
    if state == "failed":
        return 503, {"error": "Security action failed", "error_code": "action_failed"}
    action = payload.get("action")
    if action not in ACTIONS:
        return 400, {"error": "Unknown security action", "error_code": "invalid_action"}
    return 200, {"schema_version": SCHEMA_VERSION, "action": action,
                 "data": {"id": payload.get("ruleset_id") or
                          payload.get("recommendation_id") or payload.get("ipset_id") or 1101,
                          "modified": "2026-08-10T17:11:00Z", "preview": True}}
