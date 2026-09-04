"""Versioned read and write authority for Admin Security.

Every response is bounded and JSON-safe.  In particular this module never
returns event metadata/evidence, rule handler strings, IPSet CIDRs/source
credentials, Python paths, commands, or exception text.
"""

from datetime import timedelta

from django.db import transaction
from django.db.models import Count, Prefetch
from django.utils import timezone

from mojo import errors as merrors

from . import mojosec_actions, rule_validation


SCHEMA_VERSION = 1
DEFAULT_LIMIT = 50
MAX_LIMIT = 100
DEFAULT_WINDOW_HOURS = 24
MAX_WINDOW_HOURS = 24 * 90
MAX_ACTION_TARGETS = 1024
SECTIONS = (
    "overview", "cases", "incidents", "events", "rules", "ipsets",
    "recommendations", "schemas",
)


class SecurityActionError(ValueError):
    def __init__(self, message, code="invalid_action", status=400):
        self.code = code
        self.status = status
        super().__init__(message)


def _iso(value):
    return value.isoformat() if value is not None else None


def _positive(value, default, maximum, name):
    if value in (None, ""):
        return default
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SecurityActionError(f"{name} must be a positive integer")
    if value > maximum:
        raise SecurityActionError(f"{name} must be at most {maximum}")
    return value


def _envelope(data, cutoff, window, truncated=False, status="available",
              reason=None):
    row = {
        "status": status,
        "observed_at": _iso(timezone.now()),
        "cutoff": window.get("end") if isinstance(window, dict) else _iso(cutoff),
        "window": window,
        "truncated": bool(truncated),
        "data": data,
    }
    if reason:
        row["reason"] = reason
    return row


def _unavailable(cutoff, window, reason):
    return _envelope({}, cutoff, window, status="unavailable", reason=reason)


def _safe_rule_set(row, detail=False):
    validation = rule_validation.validation_summary(row)
    value = {
        "id": row.pk, "created": _iso(row.created),
        "modified": _iso(row.modified), "name": row.name or "",
        "category": row.category, "priority": row.priority,
        "is_active": row.is_active, "bundle_minutes": row.bundle_minutes,
        "bundle_by": row.bundle_by, "bundle_by_rule_set": row.bundle_by_rule_set,
        "match_by": row.match_by, "trigger_count": row.trigger_count,
        "trigger_window": row.trigger_window,
        "retrigger_every": row.retrigger_every,
        "validation": validation,
    }
    if detail and not validation.get("legacy"):
        payload = rule_validation.ruleset_payload(row)
        value["handlers"] = payload["handlers"]
        value["rules"] = payload["rules"]
        value["delete_on_resolution"] = payload["delete_on_resolution"]
    else:
        prefetched = getattr(row, "_admin_security_rules", None)
        value["rule_count"] = (
            len(prefetched) if prefetched is not None else row.rules.count())
    return value


def _safe_recommendation(row, detail=False):
    value = {
        "id": row.pk, "created": _iso(row.created),
        "modified": _iso(row.modified), "case_id": row.case_id,
        "action": row.action, "state": row.state,
        "reason_code": row.reason_code, "confidence": row.confidence,
        "urgency": row.urgency, "requested_scope": row.requested_scope,
        "requested_ttl_seconds": row.requested_ttl_seconds,
        "expires_at": _iso(row.expires_at), "approved_at": _iso(row.approved_at),
        "target_count": row.target_count,
        "validated_count": row.validated_count,
        "protected_count": row.protected_count,
        "executed_count": row.executed_count, "failed_count": row.failed_count,
        "reversed_count": row.reversed_count,
        "policy_version": row.policy_version,
        "evaluator_version": row.evaluator_version,
    }
    if detail:
        targets = list(row.targets.order_by("id")[:MAX_ACTION_TARGETS + 1])
        value["targets"] = [{
            "id": target.pk, "ip": target.ip, "kind": target.kind,
            "validation_state": target.validation_state,
            "validation_reason": target.validation_reason,
            "outcome": target.outcome, "attempts": target.attempts,
            "applied_at": _iso(target.applied_at),
            "expires_at": _iso(target.expires_at),
            "reversed_at": _iso(target.reversed_at),
        } for target in targets[:MAX_ACTION_TARGETS]]
        value["targets_truncated"] = len(targets) > MAX_ACTION_TARGETS
    return value


def _overview(cutoff, window, end):
    from mojo.apps.incident.models import (
        Incident, MojoSecRecommendation, MojoSecRecommendationTransition,
        RuleSet)

    terminal = ("ignored", "resolved", "closed")
    transitions = dict(
        MojoSecRecommendationTransition.objects.filter(
            created__gte=cutoff, created__lte=end)
        .values_list("transition").annotate(total=Count("id")))
    data = {
        "current": {
            "open_incidents": Incident.objects.exclude(status__in=terminal).count(),
            "active_rule_sets": RuleSet.objects.filter(is_active=True).count(),
            "pending_recommendations": MojoSecRecommendation.objects.filter(
                state__in=("proposed", "approved", "auto_approved", "executing")
            ).count(),
        },
        "recommendation_transitions": transitions,
        "accuracy": {
            "current": "exact_current_rows",
            "recommendation_transitions": "exact_append_only_transitions",
            "resolution_rate": "unavailable",
        },
        "unavailable": {
            "resolution_rate": "incident rows do not preserve every deletion as an immutable transition"
        },
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
                "accuracy": "exact", "window": window},
            "case_learning": {"source": "bounded MojoSecCase projections",
                              "accuracy": "sampled", "window": window},
            "resolution_rate": {"source": None, "accuracy": "unavailable",
                                "window": window},
        },
    }
    return _envelope(data, cutoff, window)


def _cases(cutoff, window, end, limit):
    from mojo.apps.incident.models import MojoSecCase
    qs = MojoSecCase.objects.filter(
        last_seen__gte=cutoff, last_seen__lte=end).order_by("-last_seen", "-id")
    rows = list(qs[:limit + 1])
    data = [{
        "id": row.pk, "created": _iso(row.created),
        "first_seen": _iso(row.first_seen), "last_seen": _iso(row.last_seen),
        "sensor_kind": row.sensor_kind, "resource_id": row.resource_id,
        "family": row.family, "state": row.state, "urgency": row.urgency,
        "occurrence_count": row.occurrence_count,
        "receipt_count": row.receipt_count,
        "projected_event_count": row.projected_event_count,
        "distinct_count": row.distinct_count, "sample_count": row.sample_count,
        "overflow_count": row.overflow_count,
        "distinct_source_count": row.distinct_source_count,
        "policy_version": row.policy_version,
        "evaluator_version": row.evaluator_version,
        "accuracy": "sampled_learning_projection",
    } for row in rows[:limit]]
    return _envelope(data, cutoff, window, len(rows) > limit)


def _incidents(cutoff, window, end, limit):
    from mojo.apps.incident.models import Incident
    rows = list(Incident.objects.filter(created__gte=cutoff, created__lte=end).order_by(
        "-created", "-id")[:limit + 1])
    data = [{
        "id": row.pk, "created": _iso(row.created), "priority": row.priority,
        "state": row.state, "status": row.status, "scope": row.scope,
        "category": row.category, "group_id": row.group_id,
        "rule_set_id": row.rule_set_id,
    } for row in rows[:limit]]
    return _envelope(data, cutoff, window, len(rows) > limit)


def _events(cutoff, window, end, limit):
    from mojo.apps.incident.models import Event
    rows = list(Event.objects.filter(created__gte=cutoff, created__lte=end).order_by(
        "-created", "-id")[:limit + 1])
    data = [row.admin_security_projection() for row in rows[:limit]]
    return _envelope(data, cutoff, window, len(rows) > limit)


def _rules(cutoff, window, limit):
    from mojo.apps.incident.models import Rule, RuleSet
    rows = list(
        RuleSet.objects.prefetch_related(Prefetch(
            "rules", queryset=Rule.objects.order_by("index", "id"),
            to_attr="_admin_security_rules"))
        .order_by("priority", "id")[:limit + 1])
    return _envelope([_safe_rule_set(row) for row in rows[:limit]], cutoff,
                     window, len(rows) > limit)


def _ipsets(cutoff, window, limit):
    from mojo.apps.incident.models import IPSet
    rows = list(IPSet.objects.order_by("name", "id")[:limit + 1])
    data = [{
        "id": row.pk, "created": _iso(row.created),
        "modified": _iso(row.modified), "name": row.name, "kind": row.kind,
        "description": row.description, "source": row.source,
        "is_enabled": row.is_enabled, "cidr_count": row.cidr_count,
        "last_synced": _iso(row.last_synced),
        "enforcement_status": ("verified" if not row.sync_error and
                               row.last_synced else "pending_or_unknown"),
        "sync_error": row.sync_error or "",
    } for row in rows[:limit]]
    return _envelope(data, cutoff, window, len(rows) > limit)


def _recommendations(cutoff, window, end, limit, recommendation_id=None):
    from mojo.apps.incident.models import MojoSecRecommendation
    queryset = MojoSecRecommendation.objects.filter(
        created__gte=cutoff, created__lte=end)
    detail = recommendation_id is not None
    if detail:
        queryset = queryset.filter(
            pk=_id(recommendation_id, "recommendation_id"))
    rows = list(queryset.order_by("-created", "-id")[:limit + 1])
    return _envelope([
        _safe_recommendation(row, detail=detail) for row in rows[:limit]],
                     cutoff, window, len(rows) > limit)


def overview(params=None):
    params = params or {}
    limit = _positive(params.get("limit"), DEFAULT_LIMIT, MAX_LIMIT, "limit")
    hours = _positive(params.get("window_hours"), DEFAULT_WINDOW_HOURS,
                      MAX_WINDOW_HOURS, "window_hours")
    now = timezone.now()
    cutoff = now - timedelta(hours=hours)
    window = {"hours": hours, "start": _iso(cutoff), "end": _iso(now)}
    recommendation_id = params.get("recommendation_id")
    if recommendation_id not in (None, ""):
        recommendation_id = _id(recommendation_id, "recommendation_id")
    else:
        recommendation_id = None
    requested = params.get("sections") or params.get("section") or SECTIONS
    if isinstance(requested, str):
        requested = [part.strip() for part in requested.split(",") if part.strip()]
    if not isinstance(requested, (list, tuple)) or not requested:
        raise SecurityActionError("sections must be a non-empty array or comma-separated string")
    unknown = sorted(set(requested) - set(SECTIONS))
    if unknown:
        raise SecurityActionError(f"unknown security section: {', '.join(unknown)}")
    collectors = {
        "overview": lambda: _overview(cutoff, window, now),
        "cases": lambda: _cases(cutoff, window, now, limit),
        "incidents": lambda: _incidents(cutoff, window, now, limit),
        "events": lambda: _events(cutoff, window, now, limit),
        "rules": lambda: _rules(cutoff, window, limit),
        "ipsets": lambda: _ipsets(cutoff, window, limit),
        "recommendations": lambda: _recommendations(
            cutoff, window, now, limit, recommendation_id),
        "schemas": lambda: _envelope({
            "rule_policy": rule_validation.public_schema(),
            "actions": list(ACTIONS),
        }, cutoff, window),
    }
    sections = {}
    for name in requested:
        try:
            sections[name] = collectors[name]()
        except Exception:
            sections[name] = _unavailable(cutoff, window, "collector_unavailable")
    return {"schema_version": SCHEMA_VERSION, "sections": sections}


def _id(value, name):
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SecurityActionError(f"{name} must be a positive integer")
    return value


def _confirm(payload, expected):
    if payload.get("confirm") != expected:
        raise SecurityActionError(f"confirm must exactly equal {expected!r}",
                                  code="confirmation_required")


def _revision(row):
    return _iso(row.modified)


def _expect_revision(payload, row):
    expected = payload.get("expected_modified")
    if not isinstance(expected, str) or expected != _revision(row):
        raise SecurityActionError(
            "The security object changed. Reload it and confirm again.",
            code="stale_revision", status=409)


def _persist_ruleset(normalized, existing=None):
    from mojo.apps.incident.models import Rule, RuleSet
    metadata = dict(normalized["metadata"])
    if existing is not None and isinstance(existing.metadata, dict):
        # These values are internal LLM workflow state, not public policy
        # input. A complete policy replacement must not silently discard them.
        for key in (
                "agent_memory", "agent_prompt", "assistant_proposed",
                "llm_proposed", "llm_reasoning", "occurrence_count"):
            if key in existing.metadata:
                metadata[key] = existing.metadata[key]
    values = {key: normalized[key] for key in (
        "name", "category", "priority", "bundle_minutes", "bundle_by",
        "bundle_by_rule_set", "match_by", "trigger_count", "trigger_window",
        "retrigger_every", "handler", "is_active")}
    values["metadata"] = metadata
    if existing is None:
        row = RuleSet.objects.create(**values)
    else:
        row = existing
        for key, value in values.items():
            setattr(row, key, value)
        row.save()
        row.rules.all().delete()
    Rule.objects.bulk_create([Rule(parent=row, **rule) for rule in normalized["rules"]])
    # bulk_create deliberately bypasses Rule.save; touch the aggregate once.
    row.save(update_fields=["modified"])
    return row


def _validated_ruleset(value):
    try:
        return rule_validation.normalize_ruleset(value)
    except rule_validation.RuleValidationError as error:
        raise SecurityActionError(
            str(error), code=error.code, status=400) from error


def _validated_existing(row):
    try:
        return rule_validation.validate_existing(row)
    except rule_validation.RuleValidationError as error:
        raise SecurityActionError(
            "RuleSet requires a complete inactive replacement before activation",
            code=error.code, status=409) from error


def _audit(actor, action, object_type, object_id):
    from mojo.apps.incident.models import Event
    Event.objects.create(
        level=5, scope="global", category="security:admin_action",
        title="Admin Security policy action",
        details=f"{action} {object_type} {object_id}",
        uid=getattr(actor, "pk", None),
        metadata={"schema_version": SCHEMA_VERSION, "action": action,
                  "object_type": object_type, "object_id": object_id})


ACTIONS = (
    "ruleset.create", "ruleset.replace", "ruleset.activate",
    "ruleset.deactivate", "ruleset.delete", "recommendation.approve",
    "recommendation.reject", "recommendation.cancel", "recommendation.reverse",
    "ipset.sync", "ipset.enable", "ipset.disable",
)

_ACTION_FIELDS = {
    "ruleset.create": {"action", "confirm", "ruleset"},
    "ruleset.replace": {"action", "ruleset_id", "expected_modified",
                        "confirm", "ruleset"},
    "ruleset.activate": {"action", "ruleset_id", "expected_modified",
                         "confirm", "confirm_catch_all"},
    "ruleset.deactivate": {"action", "ruleset_id", "expected_modified", "confirm"},
    "ruleset.delete": {"action", "ruleset_id", "expected_modified", "confirm"},
    "recommendation.approve": {"action", "recommendation_id",
                               "expected_modified", "confirm", "note"},
    "recommendation.reject": {"action", "recommendation_id",
                              "expected_modified", "confirm", "note"},
    "recommendation.cancel": {"action", "recommendation_id",
                              "expected_modified", "confirm", "note"},
    "recommendation.reverse": {"action", "recommendation_id",
                               "expected_modified", "confirm", "note"},
    "ipset.sync": {"action", "ipset_id", "expected_modified", "confirm"},
    "ipset.enable": {"action", "ipset_id", "expected_modified", "confirm"},
    "ipset.disable": {"action", "ipset_id", "expected_modified", "confirm"},
}


def _safe_ipset(row, result=None):
    value = {
        "id": row.pk, "modified": _iso(row.modified), "name": row.name,
        "kind": row.kind, "description": row.description,
        "is_enabled": row.is_enabled, "cidr_count": row.cidr_count,
        "last_synced": _iso(row.last_synced),
        "sync_error": row.sync_error or "",
    }
    if isinstance(result, dict):
        value["enforcement_status"] = result.get("status", "unknown")
        value["enforcement_ok"] = result.get("ok") is True
        if result.get("ok") is not True:
            error = result.get("error") or {}
            value["error_code"] = str(error.get("code") or "fleet_unverified")[:64]
    return value


def _claim_ipset_action(action, payload):
    from mojo.apps.incident.models import IPSet
    pk = _id(payload.get("ipset_id"), "ipset_id")
    with transaction.atomic():
        row = IPSet.objects.select_for_update().filter(pk=pk).first()
        if row is None:
            raise SecurityActionError("IPSet does not exist", status=404,
                                      code="not_found")
        _expect_revision(payload, row)
        verb = action.split(".", 1)[1].upper()
        _confirm(payload, f"{verb} IPSET {row.pk}")
        if action == "ipset.enable":
            row.set_enabled_desired(True)
        elif action == "ipset.disable":
            row.set_enabled_desired(False)
        elif action != "ipset.sync":
            raise SecurityActionError("unsupported IPSet action")
    return row


def _rule_action(action, payload, actor):
    from mojo.apps.incident.models import RuleSet
    if action == "ruleset.create":
        _confirm(payload, "CREATE RULESET")
        proposed = dict(payload.get("ruleset") or {})
        proposed["is_active"] = False
        row = _persist_ruleset(_validated_ruleset(proposed))
    else:
        pk = _id(payload.get("ruleset_id"), "ruleset_id")
        row = RuleSet.objects.select_for_update().filter(pk=pk).first()
        if row is None:
            raise SecurityActionError("RuleSet does not exist", status=404,
                                      code="not_found")
        _expect_revision(payload, row)
        verb = action.split(".", 1)[1].upper()
        _confirm(payload, f"{verb} RULESET {row.pk}")
        if action == "ruleset.replace":
            proposed = dict(payload.get("ruleset") or {})
            if proposed.get("is_active") is not False:
                raise SecurityActionError("replacement ruleset must be inactive")
            row = _persist_ruleset(_validated_ruleset(proposed), row)
        elif action == "ruleset.activate":
            _validated_existing(row)
            if rule_validation.is_catch_all(row):
                required = f"ACTIVATE CATCH-ALL RULESET {row.pk}"
                if payload.get("confirm_catch_all") != required:
                    raise SecurityActionError(
                        f"confirm_catch_all must exactly equal {required!r}",
                        code="catch_all_confirmation_required")
            row.is_active = True
            row.save(update_fields=["is_active", "modified"])
        elif action == "ruleset.deactivate":
            row.is_active = False
            row.save(update_fields=["is_active", "modified"])
        elif action == "ruleset.delete":
            result = {"id": row.pk, "deleted": True}
            _audit(actor, action, "ruleset", row.pk)
            row.delete()
            return result
        else:
            raise SecurityActionError("unsupported ruleset action")
    _audit(actor, action, "ruleset", row.pk)
    return _safe_rule_set(row, detail=True)


def _recommendation_action(action, payload, actor):
    from mojo.apps.incident.models import MojoSecRecommendation
    if action == "recommendation.reverse":
        pk = _id(payload.get("recommendation_id"), "recommendation_id")
        note = payload.get("note", "")
        if not isinstance(note, str) or len(note) > 256:
            raise SecurityActionError(
                "note must be a string of at most 256 characters")
        with transaction.atomic():
            row = MojoSecRecommendation.objects.select_for_update().filter(
                pk=pk).first()
            if row is None:
                raise SecurityActionError(
                    "Recommendation does not exist", status=404,
                    code="not_found")
            actual_targets = row.targets.count()
            if (actual_targets > MAX_ACTION_TARGETS or
                    actual_targets != row.target_count):
                raise SecurityActionError(
                    "Recommendation target scope is inconsistent or exceeds the review bound",
                    code="scope_unavailable", status=409)
            _expect_revision(payload, row)
            _confirm(payload, f"REVERSE RECOMMENDATION {row.pk}")
        try:
            row = mojosec_actions.reverse(
                row, actor, note=note,
                expected_modified=payload["expected_modified"])
        except ValueError as error:
            raise SecurityActionError(
                str(error), code="invalid_state", status=409) from error
        with transaction.atomic():
            _audit(actor, action, "recommendation", row.pk)
        return _safe_recommendation(row, detail=True)
    pk = _id(payload.get("recommendation_id"), "recommendation_id")
    row = MojoSecRecommendation.objects.select_for_update().filter(pk=pk).first()
    if row is None:
        raise SecurityActionError("Recommendation does not exist", status=404,
                                  code="not_found")
    actual_targets = row.targets.count()
    if (actual_targets > MAX_ACTION_TARGETS or
            actual_targets != row.target_count):
        raise SecurityActionError(
            "Recommendation target scope is inconsistent or exceeds the review bound",
            code="scope_unavailable", status=409)
    _expect_revision(payload, row)
    verb = action.split(".", 1)[1].upper()
    _confirm(payload, f"{verb} RECOMMENDATION {row.pk}")
    note = payload.get("note", "")
    if not isinstance(note, str) or len(note) > 256:
        raise SecurityActionError("note must be a string of at most 256 characters")
    try:
        if verb == "APPROVE":
            row = mojosec_actions.approve(row, actor, note=note)
        elif verb in ("REJECT", "CANCEL"):
            row = mojosec_actions.reject(
                row, actor, note=note,
                transition="cancelled" if verb == "CANCEL" else "rejected")
        elif verb == "REVERSE":
            row = mojosec_actions.reverse(row, actor, note=note)
        else:
            raise SecurityActionError("unsupported recommendation action")
    except ValueError as error:
        raise SecurityActionError(str(error), code="invalid_state", status=409) from error
    _audit(actor, action, "recommendation", row.pk)
    return _safe_recommendation(row, detail=True)


def apply_action(payload, actor):
    """Apply one typed action under a locked, auditable transaction.

    HTTP/Assistant/ticket callers own identity and fresh-auth verification;
    this service re-checks the global grant so an internal adapter cannot
    accidentally widen authority.
    """
    if not getattr(actor, "is_authenticated", False) or not actor.has_permission(
            ["manage_security", "security"]):
        raise merrors.PermissionDeniedException()
    if not isinstance(payload, dict):
        raise SecurityActionError("action payload must be an object")
    action = payload.get("action")
    if action not in ACTIONS:
        raise SecurityActionError("unknown Admin Security action")
    unknown = sorted(set(payload) - _ACTION_FIELDS[action])
    if unknown:
        raise SecurityActionError(
            f"unknown fields for {action}: {', '.join(unknown)}")
    if action.startswith("ipset."):
        row = _claim_ipset_action(action, payload)
        # Fleet waits must never hold the row lock or a database transaction.
        result = row.sync()
        row.refresh_from_db()
        with transaction.atomic():
            _audit(actor, action, "ipset", row.pk)
        data = _safe_ipset(row, result)
    elif action == "recommendation.reverse":
        # reverse() owns short claim/finalize transactions around its network
        # wait; an outer transaction would defeat that boundary.
        data = _recommendation_action(action, payload, actor)
    else:
        with transaction.atomic():
            if action.startswith("ruleset."):
                data = _rule_action(action, payload, actor)
            else:
                data = _recommendation_action(action, payload, actor)
    return {"schema_version": SCHEMA_VERSION, "action": action, "data": data}


def create_inactive_proposal(ruleset, metadata=None):
    """Internal LLM proposal path: validation is shared, activation is absent."""
    proposed = dict(ruleset or {})
    proposed["is_active"] = False
    normalized = rule_validation.normalize_ruleset(proposed)
    safe_metadata = {"assistant_proposed": True}
    if isinstance(metadata, dict) and isinstance(metadata.get("reasoning"), str):
        safe_metadata["llm_reasoning"] = metadata["reasoning"][:512]
    normalized["metadata"].update(safe_metadata)
    with transaction.atomic():
        return _persist_ruleset(normalized)
