"""Versioned read and write authority for Admin Security.

Discovery responses stay bounded. Authorized detail responses preserve the
operational evidence administrators need, with only authentication secrets
removed at the final serialization boundary.
"""

from datetime import timedelta
import re
from types import MappingProxyType

from django.db import transaction
from django.db.models import Count, Prefetch
from django.utils import timezone

from mojo import errors as merrors
from mojo.helpers.request import (
    credential_kind, restricted_identity, safe_actor_context,
    validated_user_api_key)

from . import admin_security_transport as transport
from . import mojosec_actions, rule_validation


SCHEMA_VERSION = 3
DEFAULT_LIMIT = 50
MAX_LIMIT = 100
DEFAULT_WINDOW_HOURS = 24
MAX_WINDOW_HOURS = 24 * 90
MAX_ACTION_TARGETS = 1024
MAX_RECEIPT_HOSTS = 128
MAX_RECEIPT_MEMBER_COUNT = 1000000
MAX_OBJECT_ID = 2147483647
SECTIONS = (
    "overview", "cases", "incidents", "events", "rules", "ipsets",
    "recommendations", "schemas",
)


class SecurityAuthority:
    """Immutable, server-derived authority for one Admin Security request."""

    __slots__ = (
        "scope", "group_id", "credential", "actor", "actor_context",
        "freshness_window")

    def __init__(self, scope, credential, actor, group_id=None,
                 actor_context=None, freshness_window=0):
        if scope not in ("global", "group"):
            raise ValueError("Admin Security authority scope is invalid")
        if scope == "group" and (
                isinstance(group_id, bool) or not isinstance(group_id, int) or
                group_id < 1):
            raise ValueError("Admin Security group authority requires a group id")
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "group_id", group_id)
        object.__setattr__(self, "credential", credential)
        object.__setattr__(self, "actor", actor)
        object.__setattr__(self, "actor_context", MappingProxyType(
            dict(actor_context or {})))
        object.__setattr__(self, "freshness_window", max(0, int(freshness_window)))

    def __setattr__(self, name, value):
        raise AttributeError("SecurityAuthority is immutable")

    @property
    def is_global(self):
        return self.scope == "global"

    @property
    def cursor_scope(self):
        return "global" if self.is_global else f"group:{self.group_id}"


def _global_internal_authority(actor=None):
    """Trusted compatibility scope for internal service callers."""
    return SecurityAuthority(
        "global", "internal", actor,
        actor_context={"credential_kind": "internal",
                       "user_id": getattr(actor, "pk", None)})


def build_authority(request, write=False):
    """Resolve one request to global authority or an exact group read scope.

    Client-supplied group parameters never choose the group. Restricted
    credentials carry an authenticated group; ordinary users use their
    server-owned default organization. Group authority is read-only.
    """
    actor = getattr(request, "user", None)
    if actor is None or not getattr(actor, "is_authenticated", False):
        raise merrors.PermissionDeniedException()
    context = safe_actor_context(request)
    from mojo.apps.account.services import fresh_auth
    freshness_window = fresh_auth.resolve_window(request)
    kind = credential_kind(request)
    global_perms = (["manage_security", "security"] if write else
                    ["view_security", "manage_security", "security"])
    restricted = restricted_identity(request)

    # A validated UserAPIKey is a User credential, not an account.ApiKey. Its
    # positive record provenance was stamped only after full JWT validation.
    if validated_user_api_key(request) is not None:
        if actor.has_permission(global_perms):
            return SecurityAuthority(
                "global", kind, actor, actor_context=context,
                freshness_window=freshness_window)
        if write:
            raise merrors.PermissionDeniedException()
        group = getattr(actor, "org", None)
        if (group is None or not group.is_effectively_active() or
                not group.user_has_permission(
                    actor, global_perms, check_user=False)):
            raise merrors.PermissionDeniedException()
        return SecurityAuthority(
            "group", kind, actor, group_id=group.pk, actor_context=context,
            freshness_window=freshness_window)

    # Ordinary interactive/OAuth user sessions retain their existing global
    # permission behavior. Unknown custom bearer identities fail closed.
    if restricted is None and kind in ("user", "oauth"):
        if actor.has_permission(global_perms):
            return SecurityAuthority(
                "global", kind, actor, actor_context=context,
                freshness_window=freshness_window)
        if write:
            raise merrors.PermissionDeniedException()
        group = getattr(actor, "org", None)
        if (group is None or not group.is_effectively_active() or
                not group.user_has_permission(
                    actor, global_perms, check_user=False)):
            raise merrors.PermissionDeniedException()
        return SecurityAuthority(
            "group", kind, actor, group_id=group.pk, actor_context=context,
            freshness_window=freshness_window)

    if restricted is None or write:
        raise merrors.PermissionDeniedException()
    group = getattr(restricted, "group", None)
    if group is None:
        group = getattr(request, "group", None)
    if (group is None or getattr(restricted, "group_id", None) != group.pk or
            not group.is_effectively_active()):
        raise merrors.PermissionDeniedException()
    if getattr(restricted, "override_user", False):
        allowed = group.user_has_permission(
            actor, global_perms, check_user=False)
    else:
        allowed = restricted.has_permission(global_perms)
    if not allowed:
        raise merrors.PermissionDeniedException()
    return SecurityAuthority(
        "group", kind, actor, group_id=group.pk, actor_context=context,
        freshness_window=freshness_window)


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


def _scope_queryset(queryset, authority, field="group_id"):
    if authority.is_global:
        return queryset
    return queryset.filter(**{field: authority.group_id})


def _capabilities(authority):
    return {
        "scope": authority.scope,
        "group_id": authority.group_id,
        "credential_kind": authority.credential,
        "view": True,
        "manage": authority.is_global and bool(
            authority.actor is None or authority.actor.has_permission(
                ["manage_security", "security"])),
        "fresh_auth": {
            "enabled": authority.freshness_window > 0,
            "window_seconds": authority.freshness_window,
            "applies_to_credential": (
                authority.credential not in (
                    "user_api_key", "api_key", "group_token", "internal")),
        },
    }


def _safe_rule_set(row, detail=False):
    validation = rule_validation.validation_summary(row)
    prefetched = getattr(row, "_admin_security_rules", None)
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
        "rule_count": (
            len(prefetched) if prefetched is not None else row.rules.count()),
    }
    if detail and not validation.get("legacy"):
        payload = rule_validation.ruleset_payload(row)
        value["handlers"] = payload["handlers"]
        value["handler"] = row.handler or ""
        value["rules"] = payload["rules"]
        value["metadata"] = row.metadata or {}
        value["delete_on_resolution"] = payload["delete_on_resolution"]
    elif detail:
        rules = prefetched if prefetched is not None else list(
            row.rules.order_by("index", "id")[:MAX_ACTION_TARGETS])
        value.update(
            handler=row.handler or "", metadata=row.metadata or {},
            rules=[{
                "id": item.pk, "name": item.name or "", "index": item.index,
                "field_name": item.field_name, "comparator": item.comparator,
                "value": item.value, "value_type": item.value_type,
                "is_required": item.is_required,
            } for item in rules],
            delete_on_resolution=bool(
                isinstance(row.metadata, dict) and
                row.metadata.get("delete_on_resolution") is True))
    return value


def _safe_recommendation(row, detail=False):
    value = {
        "id": row.pk, "created": _iso(row.created),
        "modified": _iso(row.modified), "case_id": row.case_id,
        "group_id": row.group_id,
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
            "last_error": target.last_error,
            "applied_at": _iso(target.applied_at),
            "expires_at": _iso(target.expires_at),
            "reversed_at": _iso(target.reversed_at),
            "prior_blocked_until": _iso(target.prior_blocked_until),
            "prior_reason": target.prior_reason,
        } for target in targets[:MAX_ACTION_TARGETS]]
        value["targets_truncated"] = len(targets) > MAX_ACTION_TARGETS
    return value


def _bounded_rule_set(row, authority):
    values = _safe_rule_set(row, detail=True)
    return transport.bounded_detail(
        values, authority, "ruleset", row.pk, _revision(row),
        ("handler", "metadata", "rules"))


def _bounded_case(row, authority):
    values = {
        "id": row.pk, "created": _iso(row.created),
        "modified": _iso(row.modified), "first_seen": _iso(row.first_seen),
        "last_seen": _iso(row.last_seen), "window_start": _iso(row.window_start),
        "window_end": _iso(row.window_end), "group_id": row.group_id,
        "installation_key_id": row.installation_key_id,
        "sensor_id": row.sensor_id, "sensor_kind": row.sensor_kind,
        "resource_id": row.resource_id, "family": row.family,
        "network": row.network, "deployment_id": row.deployment_id,
        "campaign_id": row.campaign_id, "correlation_key": row.correlation_key,
        "window_key": row.window_key, "state": row.state,
        "state_reason": row.state_reason, "urgency": row.urgency,
        "urgency_reason": row.urgency_reason, "settled_at": _iso(row.settled_at),
        "projected_urgency": row.projected_urgency,
        "projection_dispatched_at": _iso(row.projection_dispatched_at),
        "occurrence_count": row.occurrence_count,
        "receipt_count": row.receipt_count,
        "projected_event_count": row.projected_event_count,
        "distinct_count": row.distinct_count, "sample_count": row.sample_count,
        "overflow_count": row.overflow_count,
        "distinct_source_count": row.distinct_source_count,
        "policy_version": row.policy_version,
        "evaluator_version": row.evaluator_version,
        "accuracy": "sampled_learning_projection",
        "samples": row.samples, "observed_sources": row.observed_sources,
        "breakdown": row.breakdown,
    }
    return transport.bounded_detail(
        values, authority, "case", row.pk, _revision(row),
        ("samples", "observed_sources", "breakdown"))


def _bounded_incident(row, authority):
    values = {
        "id": row.pk, "created": _iso(row.created), "priority": row.priority,
        "state": row.state, "status": row.status, "scope": row.scope,
        "category": row.category, "country_code": row.country_code,
        "group_id": row.group_id, "rule_set_id": row.rule_set_id,
        "source_ip": row.source_ip, "hostname": row.hostname,
        "model_name": row.model_name, "model_id": row.model_id,
        "title": row.title, "details": row.details, "metadata": row.metadata,
    }
    return transport.bounded_detail(
        values, authority, "incident", row.pk, _iso(row.created),
        ("title", "details", "metadata"))


def _bounded_event(row, authority):
    values = row.admin_security_projection(detail=True)
    return transport.bounded_detail(
        values, authority, "event", row.pk, _iso(row.created),
        ("title", "details", "metadata"))


def _bounded_recommendation(row, authority):
    values = _safe_recommendation(row, detail=True)
    values.update(
        explanation=row.explanation, approval_note=row.approval_note,
        collateral=row.collateral,
        installation_key_id=row.installation_key_id)
    return transport.bounded_detail(
        values, authority, "recommendation", row.pk, _revision(row),
        ("explanation", "approval_note", "collateral", "targets"))


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


def _cases(cutoff, window, end, limit, authority, case_id=None):
    from mojo.apps.incident.models import MojoSecCase
    qs = _scope_queryset(MojoSecCase.objects.all(), authority)
    if case_id is not None:
        qs = qs.filter(pk=_id(case_id, "case_id"))
    else:
        qs = qs.filter(last_seen__gte=cutoff, last_seen__lte=end)
    qs = qs.order_by("-last_seen", "-id")
    rows = list(qs[:limit + 1])
    if case_id is not None:
        data = [_bounded_case(row, authority) for row in rows[:limit]]
    else:
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
        "accuracy": "sampled_learning_projection", "group_id": row.group_id,
    } for row in rows[:limit]]
    return _envelope(data, cutoff, window, len(rows) > limit)


def _incidents(cutoff, window, end, limit, authority, incident_id=None):
    from mojo.apps.incident.models import Incident
    queryset = _scope_queryset(Incident.objects.all(), authority)
    if incident_id is not None:
        queryset = queryset.filter(pk=_id(incident_id, "incident_id"))
    else:
        queryset = queryset.filter(created__gte=cutoff, created__lte=end)
    rows = list(queryset.order_by("-created", "-id")[:limit + 1])
    if incident_id is not None:
        data = [_bounded_incident(row, authority) for row in rows[:limit]]
    else:
        data = [{
        "id": row.pk, "created": _iso(row.created), "priority": row.priority,
        "state": row.state, "status": row.status, "scope": row.scope,
        "category": row.category, "group_id": row.group_id,
        "rule_set_id": row.rule_set_id, "source_ip": row.source_ip,
        "hostname": row.hostname, "title": (row.title or "")[:512],
    } for row in rows[:limit]]
    return _envelope(data, cutoff, window, len(rows) > limit)


def _events(cutoff, window, end, limit, authority, event_id=None):
    from mojo.apps.incident.models import Event
    queryset = _scope_queryset(Event.objects.all(), authority)
    if event_id is not None:
        queryset = queryset.filter(pk=_id(event_id, "event_id"))
    else:
        queryset = queryset.filter(created__gte=cutoff, created__lte=end)
    rows = list(queryset.order_by("-created", "-id")[:limit + 1])
    data = ([_bounded_event(row, authority) for row in rows[:limit]]
            if event_id is not None else
            [transport.scrub(row.admin_security_projection(detail=False))
             for row in rows[:limit]])
    return _envelope(data, cutoff, window, len(rows) > limit)


def _rules(cutoff, window, limit, authority, ruleset_id=None):
    from mojo.apps.incident.models import Rule, RuleSet
    queryset = RuleSet.objects.prefetch_related(Prefetch(
            "rules", queryset=Rule.objects.order_by("index", "id"),
            to_attr="_admin_security_rules"))
    detail = ruleset_id is not None
    if detail:
        queryset = queryset.filter(pk=_id(ruleset_id, "ruleset_id"))
    rows = list(queryset.order_by("priority", "id")[:limit + 1])
    return _envelope([
        (_bounded_rule_set(row, authority) if detail else
         transport.scrub(_safe_rule_set(row, detail=False)))
        for row in rows[:limit]], cutoff,
                     window, len(rows) > limit)


def _ipsets(cutoff, window, limit, authority, ipset_id=None):
    from mojo.apps.incident.models import IPSet
    from mojo.apps.incident.services import firewall_truth
    queryset = IPSet.objects.all()
    if ipset_id is not None:
        queryset = queryset.filter(pk=_id(ipset_id, "ipset_id"))
    rows = list(queryset.order_by("name", "id")[:limit + 1])
    try:
        roster = firewall_truth.exact_compatible_roster()
        roster_available = True
    except firewall_truth.FirewallTruthError:
        roster = []
        roster_available = False
    data = []
    for row in rows[:limit]:
        value = _safe_ipset(
            row, roster=roster, observation_cutoff=window["end"])
        value.update(created=_iso(row.created))
        if ipset_id is not None:
            value.update(
                source=row.source, source_url=row.source_url,
                source_key=row.source_key, data=row.data,
                sync_error=row.sync_error)
            value = transport.bounded_detail(
                value, authority, "ipset", row.pk, _revision(row),
                ("data", "sync_error"))
        else:
            value = transport.scrub(value)
        data.append(value)
    roster_stable = True
    if roster_available:
        try:
            roster_stable = firewall_truth.exact_compatible_roster() == roster
        except firewall_truth.FirewallTruthError:
            roster_stable = False
    if not roster_stable:
        for value in data:
            value.update(
                enforcement_status="stale", enforcement_ok=False,
                error_code="runner_roster_changed")
            value["enforcement"].update(status="stale", observed="stale")
    return _envelope(data, cutoff, window, len(rows) > limit)


def _recommendations(cutoff, window, end, limit, authority,
                     recommendation_id=None):
    from mojo.apps.incident.models import MojoSecRecommendation
    queryset = _scope_queryset(MojoSecRecommendation.objects.all(), authority)
    detail = recommendation_id is not None
    if detail:
        queryset = queryset.filter(
            pk=_id(recommendation_id, "recommendation_id"))
    else:
        queryset = queryset.filter(created__gte=cutoff, created__lte=end)
    rows = list(queryset.order_by("-created", "-id")[:limit + 1])
    return _envelope([
        (_bounded_recommendation(row, authority) if detail else
         transport.scrub(_safe_recommendation(row, detail=False)))
        for row in rows[:limit]],
                     cutoff, window, len(rows) > limit)


def _chunk_value(authority, cursor):
    """Resume one authorized detail field after validating its signed cursor."""
    token = transport.read_cursor(cursor)
    if token["scope"] != authority.cursor_scope:
        raise transport.TransportError("Admin Security cursor is invalid")
    kind = token["kind"]
    pk = _id(token["id"], "cursor id")
    field = token["field"]
    value = revision = None
    allowed = set()

    if kind == "event":
        from mojo.apps.incident.models import Event
        row = _scope_queryset(Event.objects.all(), authority).filter(pk=pk).first()
        allowed = {"title", "details", "metadata"}
        revision = _iso(row.created) if row else None
        value = getattr(row, field, None) if row and field in allowed else None
    elif kind == "incident":
        from mojo.apps.incident.models import Incident
        row = _scope_queryset(Incident.objects.all(), authority).filter(pk=pk).first()
        allowed = {"title", "details", "metadata"}
        revision = _iso(row.created) if row else None
        value = getattr(row, field, None) if row and field in allowed else None
    elif kind == "case":
        from mojo.apps.incident.models import MojoSecCase
        row = _scope_queryset(MojoSecCase.objects.all(), authority).filter(pk=pk).first()
        allowed = {"samples", "observed_sources", "breakdown"}
        revision = _revision(row) if row else None
        value = getattr(row, field, None) if row and field in allowed else None
    elif kind == "recommendation":
        from mojo.apps.incident.models import MojoSecRecommendation
        row = _scope_queryset(
            MojoSecRecommendation.objects.all(), authority).filter(pk=pk).first()
        allowed = {"explanation", "approval_note", "collateral", "targets"}
        revision = _revision(row) if row else None
        if row and field in allowed:
            if field == "targets":
                value = _safe_recommendation(row, detail=True)["targets"]
            else:
                value = getattr(row, field)
    elif authority.is_global and kind == "ruleset":
        from django.db.models import Prefetch
        from mojo.apps.incident.models import Rule, RuleSet
        row = RuleSet.objects.prefetch_related(Prefetch(
            "rules", queryset=Rule.objects.order_by("index", "id"),
            to_attr="_admin_security_rules")).filter(pk=pk).first()
        allowed = {"handler", "metadata", "rules"}
        revision = _revision(row) if row else None
        value = (_safe_rule_set(row, detail=True).get(field)
                 if row and field in allowed else None)
    elif authority.is_global and kind == "ipset":
        from mojo.apps.incident.models import IPSet
        row = IPSet.objects.filter(pk=pk).first()
        allowed = {"data", "sync_error"}
        revision = _revision(row) if row else None
        value = getattr(row, field, None) if row and field in allowed else None
    if revision is None or field not in allowed:
        # The same response covers absent and out-of-scope objects, so a group
        # credential cannot use cursor replay as an existence oracle.
        raise transport.TransportError(
            "Admin Security evidence is unavailable", code="not_found", status=404)
    return transport.chunk(
        value, authority, kind, pk, field, revision, cursor=cursor)


def overview(params=None, authority=None):
    params = params or {}
    authority = authority or _global_internal_authority()
    cursor = params.get("chunk_cursor")
    if cursor not in (None, ""):
        try:
            data = _chunk_value(authority, cursor)
        except transport.TransportError as error:
            raise SecurityActionError(
                str(error), code=error.code, status=error.status) from error
        return transport.scrub({
            "schema_version": SCHEMA_VERSION,
            "capabilities": _capabilities(authority), "chunk": data})
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
    ruleset_id = params.get("ruleset_id")
    if ruleset_id not in (None, ""):
        ruleset_id = _id(ruleset_id, "ruleset_id")
    else:
        ruleset_id = None
    detail_ids = {}
    for name in ("case", "incident", "event", "ipset"):
        value = params.get(f"{name}_id")
        detail_ids[name] = (
            _id(value, f"{name}_id") if value not in (None, "") else None)
    default_sections = (SECTIONS if authority.is_global else
                        ("cases", "incidents", "events", "recommendations"))
    requested = params.get("sections") or params.get("section") or default_sections
    if isinstance(requested, str):
        requested = [part.strip() for part in requested.split(",") if part.strip()]
    if not isinstance(requested, (list, tuple)) or not requested:
        raise SecurityActionError("sections must be a non-empty array or comma-separated string")
    if len(requested) > len(SECTIONS) or any(
            not isinstance(name, str) or len(name) > 32 for name in requested):
        raise SecurityActionError("sections contains an invalid security section")
    unknown = sorted(set(requested) - set(SECTIONS))
    if unknown:
        raise SecurityActionError("request contains an unknown security section")
    group_allowed = {"cases", "incidents", "events", "recommendations"}
    if not authority.is_global and set(requested) - group_allowed:
        raise SecurityActionError(
            "group-scoped Admin Security access is limited to group evidence",
            code="permission_denied", status=403)
    collectors = {
        "overview": lambda: _overview(cutoff, window, now),
        "cases": lambda: _cases(
            cutoff, window, now, limit, authority, detail_ids["case"]),
        "incidents": lambda: _incidents(
            cutoff, window, now, limit, authority, detail_ids["incident"]),
        "events": lambda: _events(
            cutoff, window, now, limit, authority, detail_ids["event"]),
        "rules": lambda: _rules(
            cutoff, window, limit, authority, ruleset_id),
        "ipsets": lambda: _ipsets(
            cutoff, window, limit, authority, detail_ids["ipset"]),
        "recommendations": lambda: _recommendations(
            cutoff, window, now, limit, authority, recommendation_id),
        "schemas": lambda: _envelope({
            "rule_policy": rule_validation.public_schema(),
            "actions": _action_schemas(),
            "action_names": list(ACTIONS),
        }, cutoff, window),
    }
    sections = {}
    for name in requested:
        try:
            sections[name] = collectors[name]()
        except Exception:
            sections[name] = _unavailable(cutoff, window, "collector_unavailable")
    return transport.scrub({
        "schema_version": SCHEMA_VERSION,
        "capabilities": _capabilities(authority), "sections": sections})


def _id(value, name):
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SecurityActionError(f"{name} must be a positive integer")
    if value > MAX_OBJECT_ID:
        raise SecurityActionError(f"{name} must be at most {MAX_OBJECT_ID}")
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
    metadata = rule_validation.mark_governed(normalized["metadata"])
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
            "RuleSet policy is invalid.", code=error.code, status=400) from error


def _validated_existing(row):
    if not rule_validation.is_governed(row):
        return {"legacy": True}
    try:
        return rule_validation.validate_existing(row)
    except rule_validation.RuleValidationError as error:
        raise SecurityActionError(
            "RuleSet requires a complete inactive replacement before activation",
            code=error.code, status=409) from error


def _audit(actor, action, object_type, object_id, actor_context=None):
    from mojo.apps.incident.models import Event
    provenance = dict(actor_context or {
        "credential_kind": "internal", "user_id": getattr(actor, "pk", None)})
    Event.objects.create(
        level=5, scope="global", category="security:admin_action",
        title="Admin Security policy action",
        details=f"{action} {object_type} {object_id}",
        uid=getattr(actor, "pk", None),
        metadata={"schema_version": SCHEMA_VERSION, "action": action,
                  "object_type": object_type, "object_id": object_id,
                  "actor": provenance})


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


def _field_schema(kind, **values):
    schema = {"type": kind}
    schema.update(values)
    return schema


def _action_schema(action, required, properties, confirmation):
    return {
        "type": "object",
        "additional_properties": False,
        "required": list(required),
        "properties": {
            "action": _field_schema("string", const=action),
            **properties,
        },
        "confirmation": confirmation,
    }


def _action_schemas():
    """Return the complete public input contract for governed mutations.

    These schemas are deliberately descriptive JSON rather than Python or
    model metadata.  A browser can render bounded controls from them without
    learning handler strings, target addresses, source keys, or any other
    internal enforcement material.
    """
    identity = _field_schema("integer", minimum=1, maximum=MAX_OBJECT_ID)
    revision = _field_schema("string", format="date-time", max_length=64)
    confirm = _field_schema("string", min_length=1, max_length=128)
    note = _field_schema("string", max_length=256, default="")
    ruleset = {"$ref": "rule_policy.aggregate"}

    return {
        "ruleset.create": _action_schema(
            "ruleset.create", ("action", "confirm", "ruleset"),
            {"confirm": confirm, "ruleset": ruleset},
            {"kind": "exact", "value": "CREATE RULESET"}),
        "ruleset.replace": _action_schema(
            "ruleset.replace",
            ("action", "ruleset_id", "expected_modified", "confirm", "ruleset"),
            {"ruleset_id": identity, "expected_modified": revision,
             "confirm": confirm, "ruleset": ruleset},
            {"kind": "template", "value": "REPLACE RULESET {id}"}),
        "ruleset.activate": _action_schema(
            "ruleset.activate",
            ("action", "ruleset_id", "expected_modified", "confirm"),
            {"ruleset_id": identity, "expected_modified": revision,
             "confirm": confirm,
             "confirm_catch_all": _field_schema(
                 "string", max_length=128, required_when="ruleset.rules is empty")},
            {"kind": "template", "value": "ACTIVATE RULESET {id}",
             "catch_all_value": "ACTIVATE CATCH-ALL RULESET {id}"}),
        "ruleset.deactivate": _action_schema(
            "ruleset.deactivate",
            ("action", "ruleset_id", "expected_modified", "confirm"),
            {"ruleset_id": identity, "expected_modified": revision,
             "confirm": confirm},
            {"kind": "template", "value": "DEACTIVATE RULESET {id}"}),
        "ruleset.delete": _action_schema(
            "ruleset.delete",
            ("action", "ruleset_id", "expected_modified", "confirm"),
            {"ruleset_id": identity, "expected_modified": revision,
             "confirm": confirm},
            {"kind": "template", "value": "DELETE RULESET {id}"}),
        **{
            f"recommendation.{verb}": _action_schema(
                f"recommendation.{verb}",
                ("action", "recommendation_id", "expected_modified", "confirm"),
                {"recommendation_id": identity,
                 "expected_modified": revision, "confirm": confirm,
                 "note": note},
                {"kind": "template",
                 "value": f"{verb.upper()} RECOMMENDATION {{id}}"})
            for verb in ("approve", "reject", "cancel", "reverse")
        },
        **{
            f"ipset.{verb}": _action_schema(
                f"ipset.{verb}",
                ("action", "ipset_id", "expected_modified", "confirm"),
                {"ipset_id": identity, "expected_modified": revision,
                 "confirm": confirm},
                {"kind": "template",
                 "value": f"{verb.upper()} IPSET {{id}}"})
            for verb in ("sync", "enable", "disable")
        },
    }


def _safe_host_list(value):
    if not isinstance(value, list):
        return []
    hosts = []
    for host in value[:MAX_RECEIPT_HOSTS]:
        if (isinstance(host, str) and
                re.fullmatch(r"(?=.*[a-z])[a-z0-9][a-z0-9.\-]{0,253}", host) and
                host not in hosts):
            hosts.append(host)
    return hosts


def _validated_host_list(value, required=False):
    if (not isinstance(value, list) or len(value) > MAX_RECEIPT_HOSTS or
            (required and not value)):
        return None
    safe = _safe_host_list(value)
    if safe != value or safe != sorted(safe):
        return None
    return safe


def _validated_roster(value, expected, required_roster=None):
    if (not isinstance(value, list) or len(value) != len(expected) or
            len(value) > MAX_RECEIPT_HOSTS):
        return None
    safe = []
    for item in value:
        if (not isinstance(item, dict) or set(item) != {"host", "started"} or
                _validated_host_list([item.get("host")], required=True) is None or
                not isinstance(item.get("started"), str) or
                not 1 <= len(item["started"]) <= 96):
            return None
        safe.append({"host": item["host"], "started": item["started"]})
    if [item["host"] for item in safe] != expected:
        return None
    if required_roster is not None and safe != required_roster:
        return None
    return safe


def _validated_set_desired(value):
    if (not isinstance(value, dict) or
            set(value) != {"name", "present", "count", "digest"} or
            not isinstance(value.get("name"), str) or
            not re.fullmatch(r"[A-Za-z0-9_-]{1,31}", value["name"]) or
            not isinstance(value.get("present"), bool) or
            isinstance(value.get("count"), bool) or
            not isinstance(value.get("count"), int) or
            not 0 <= value["count"] <= MAX_RECEIPT_MEMBER_COUNT or
            not isinstance(value.get("digest"), str) or
            not re.fullmatch(r"[0-9a-f]{64}", value["digest"])):
        return None
    return value


def _validated_direct_observations(result, expected, roster, desired,
                                   fence, fingerprint):
    rows = result.get("observations")
    if not isinstance(rows, list) or len(rows) != len(expected):
        return False
    for index, row in enumerate(rows):
        if row != {
                "schema": "mojo.firewall.semantic", "version": 1,
                "kind": "set", "identity": desired["name"],
                "fence": fence, "fingerprint": fingerprint,
                "host": expected[index], "started": roster[index]["started"],
                "desired": desired}:
            return False
    return True


def _validated_checked_receipts(checked, expected, roster, desired):
    if (not isinstance(checked, dict) or
            checked.get("schema") != "mojo.jobs.execute-checked" or
            checked.get("version") != 2 or checked.get("status") != "verified" or
            _validated_host_list(checked.get("expected_hosts"), required=True) != expected or
            _validated_roster(checked.get("expected_roster"), expected) != roster or
            _validated_host_list(checked.get("responded_hosts")) != expected or
            _validated_host_list(checked.get("succeeded_hosts")) != expected or
            checked.get("failed_hosts") != [] or checked.get("missing_hosts") != [] or
            checked.get("anomalies") != []):
        return False
    rows = checked.get("results")
    if not isinstance(rows, list) or len(rows) != len(expected):
        return False
    for index, row in enumerate(rows):
        semantic = row.get("result") if isinstance(row, dict) else None
        if (not isinstance(row, dict) or
                set(row) != {"host", "runner_id", "started", "status",
                             "result", "error"} or
                row.get("host") != expected[index] or
                not isinstance(row.get("runner_id"), str) or
                not 1 <= len(row["runner_id"]) <= 128 or
                row.get("started") != roster[index]["started"] or
                row.get("status") != "success" or row.get("error") is not None or
                semantic != {"schema": "mojo.firewall.semantic", "version": 1,
                             "kind": "set", "desired": desired,
                             "observed": desired, "ok": True}):
            return False
    return True


def _verified_enforcement_proof(result, required_roster=None):
    """Return a redacted complete proof, or ``None`` for any contradiction."""
    if (result.get("status") != "verified" or result.get("ok") is not True or
            result.get("error") not in (None, {})):
        return None
    expected = _validated_host_list(result.get("expected_hosts"), required=True)
    desired = _validated_set_desired(result.get("desired"))
    fence = result.get("fence")
    fingerprint = result.get("fingerprint")
    if (expected is None or desired is None or isinstance(fence, bool) or
            not isinstance(fence, int) or not 1 <= fence <= 9223372036854775807 or
            not isinstance(fingerprint, str) or
            not re.fullmatch(r"[0-9a-f]{64}", fingerprint)):
        return None
    roster = _validated_roster(
        result.get("expected_roster"), expected, required_roster=required_roster)
    if (roster is None or not _validated_direct_observations(
            result, expected, roster, desired, fence, fingerprint)):
        return None
    if ("checked" in result and not _validated_checked_receipts(
            result.get("checked"), expected, roster, desired)):
        return None
    return {"expected": expected, "responded": list(expected),
            "succeeded": list(expected), "desired": desired,
            "fence": fence}


def _safe_enforcement(result, roster=None, observation_cutoff=None):
    """Project checked fleet truth without exposing its raw receipt plane."""
    result = result if isinstance(result, dict) else {}
    checked = result.get("checked") if isinstance(result.get("checked"), dict) else {}
    receipt = checked or result
    expected = _safe_host_list(receipt.get("expected_hosts"))
    if not expected and isinstance(roster, list):
        expected = _safe_host_list([
            item.get("host") for item in roster if isinstance(item, dict)])
    responded = _safe_host_list(receipt.get("responded_hosts"))
    succeeded = _safe_host_list(receipt.get("succeeded_hosts"))
    failed = _safe_host_list(receipt.get("failed_hosts"))
    missing = _safe_host_list(receipt.get("missing_hosts"))
    error = result.get("error") if isinstance(result.get("error"), dict) else {}
    code = error.get("code") if isinstance(error.get("code"), str) else ""
    required_roster = (roster if isinstance(roster, list) else None)
    proof = _verified_enforcement_proof(result, required_roster=required_roster)
    if proof is not None:
        status = "verified"
        expected = proof["expected"]
        responded = proof["responded"]
        succeeded = proof["succeeded"]
        failed = []
        missing = []
    elif code in {"generation_superseded", "runner_roster_changed",
                  "host_observation_mismatch"}:
        status = "stale"
    elif missing:
        status = "missing"
    elif (result.get("status") in {"partial", "verified"} or
          checked.get("status") in {"partial", "verified"} or
          result.get("ok") is True or responded or failed or
          bool(checked.get("anomalies"))):
        status = "partial"
    else:
        status = "unavailable"
    desired = proof["desired"] if proof is not None else result.get("desired")
    safe_desired = {}
    if isinstance(desired, dict):
        for key in ("present", "count", "digest"):
            value = desired.get(key)
            if ((key == "present" and isinstance(value, bool)) or
                    (key == "count" and not isinstance(value, bool) and
                     isinstance(value, int) and
                     0 <= value <= MAX_RECEIPT_MEMBER_COUNT) or
                    (key == "digest" and isinstance(value, str) and
                     re.fullmatch(r"[0-9a-f]{64}", value))):
                safe_desired[key] = value
    generation = proof["fence"] if proof is not None else result.get("fence")
    if (isinstance(generation, bool) or not isinstance(generation, int) or
            not 1 <= generation <= 9223372036854775807):
        generation = None
    return {
        "status": status,
        "desired": safe_desired,
        "observed": status,
        "generation": generation,
        "observation_cutoff": observation_cutoff or _iso(timezone.now()),
        "expected_host_ids": expected,
        "responded_host_ids": responded,
        "succeeded_host_ids": succeeded,
        "failed_host_ids": failed,
        "missing_host_ids": missing,
    }


def _safe_ipset(row, result=None, roster=None, observation_cutoff=None):
    if result is None:
        from mojo.apps.incident.services import firewall_truth
        result = firewall_truth.current_ipset_enforcement(row, roster=roster)
    value = {
        "id": row.pk, "modified": _iso(row.modified), "name": row.name,
        "kind": row.kind, "description": row.description,
        "is_enabled": row.is_enabled, "cidr_count": row.cidr_count,
        "last_synced": _iso(row.last_synced),
        "has_sync_error": bool(row.sync_error),
    }
    if isinstance(result, dict):
        enforcement = _safe_enforcement(
            result, roster=roster, observation_cutoff=observation_cutoff)
        value["enforcement"] = enforcement
        value["enforcement_status"] = enforcement["status"]
        value["enforcement_ok"] = enforcement["status"] == "verified"
        if enforcement["status"] != "verified":
            error = result.get("error") or {}
            code = error.get("code")
            value["error_code"] = (
                code if isinstance(code, str) and
                re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code)
                else "fleet_unverified")
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
        if action not in ("ipset.enable", "ipset.disable", "ipset.sync"):
            raise SecurityActionError("unsupported IPSet action")
    return row


def _rule_action(action, payload, actor, actor_context=None):
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
            if row.is_active:
                raise SecurityActionError(
                    "deactivate the governed RuleSet before replacement",
                    code="active_ruleset", status=409)
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
            if rule_validation.is_governed(row) and row.is_active:
                raise SecurityActionError(
                    "deactivate the governed RuleSet before deletion",
                    code="active_ruleset", status=409)
            result = {"id": row.pk, "deleted": True}
            _audit(actor, action, "ruleset", row.pk, actor_context)
            row.delete()
            return result
        else:
            raise SecurityActionError("unsupported ruleset action")
    _audit(actor, action, "ruleset", row.pk, actor_context)
    return _safe_rule_set(row, detail=True)


def _recommendation_action(action, payload, actor, actor_context=None):
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
                "Recommendation state does not allow reversal.",
                code="invalid_state", status=409) from error
        with transaction.atomic():
            _audit(actor, action, "recommendation", row.pk, actor_context)
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
        raise SecurityActionError(
            "Recommendation state does not allow this action.",
            code="invalid_state", status=409) from error
    _audit(actor, action, "recommendation", row.pk, actor_context)
    return _safe_recommendation(row, detail=True)


def apply_action(payload, actor, *, reconcile_ipset=None, authority=None,
                 actor_context=None):
    """Apply one typed action under a locked, auditable transaction.

    HTTP/Assistant/ticket callers own identity and fresh-auth verification;
    this service re-checks the global grant so an internal adapter cannot
    accidentally widen authority.
    """
    if authority is not None and not authority.is_global:
        raise merrors.PermissionDeniedException()
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
        raise SecurityActionError("action payload contains unsupported fields")
    if action.startswith("ipset."):
        row = _claim_ipset_action(action, payload)
        # Fleet waits must never hold the row lock or a database transaction.
        if action == "ipset.enable":
            result = row.enable(reconciler=reconcile_ipset)
        elif action == "ipset.disable":
            result = row.disable(reconciler=reconcile_ipset)
        else:
            result = row.sync(reconciler=reconcile_ipset)
        row.refresh_from_db()
        with transaction.atomic():
            _audit(actor, action, "ipset", row.pk, actor_context)
        data = _safe_ipset(
            row, result, observation_cutoff=_iso(timezone.now()))
    elif action == "recommendation.reverse":
        # reverse() owns short claim/finalize transactions around its network
        # wait; an outer transaction would defeat that boundary.
        data = _recommendation_action(
            action, payload, actor, actor_context=actor_context)
    else:
        with transaction.atomic():
            if action.startswith("ruleset."):
                data = _rule_action(
                    action, payload, actor, actor_context=actor_context)
            else:
                data = _recommendation_action(
                    action, payload, actor, actor_context=actor_context)
    return transport.scrub(
        {"schema_version": SCHEMA_VERSION, "action": action, "data": data})


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
