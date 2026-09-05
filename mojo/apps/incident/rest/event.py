import json

from django.db import transaction

from mojo import decorators as md
from mojo import errors as merrors
from mojo import JsonResponse
from mojo.apps.incident.models import Incident, IncidentHistory, Event, RuleSet, Rule


def _compatibility_audit(request, action, object_type, object_id):
    """Emit bounded provenance authored from the authenticated request."""
    from mojo.apps.incident.services import admin_security
    from mojo.helpers.request import safe_actor_context

    admin_security._audit(
        request.user, action, object_type, object_id,
        actor_context=safe_actor_context(request))


def _method_not_allowed():
    return JsonResponse({
        "status": False, "error": "Method not allowed", "code": 405,
    }, status=405)


def _create_compatibility_ruleset(request):
    """Create one explicitly marked compatibility policy."""
    from mojo.apps.incident.services import rule_validation

    metadata = request.DATA.get("metadata", {})
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (TypeError, ValueError):
            metadata = None
    if not isinstance(metadata, dict):
        raise merrors.ValueException("RuleSet metadata must be an object")
    if rule_validation.has_reserved_marker(metadata):
        raise merrors.ValueException("the policy mode marker is server-owned")
    request.DATA["metadata"] = rule_validation.mark_compatible(metadata)
    RuleSet.rest_check_permission_or_raise(
        request, ["CREATE_PERMS", "SAVE_PERMS", "VIEW_PERMS"])
    RuleSet.rest_resolve_request_graph(request, "default")
    row = RuleSet.create_from_request(request)
    _compatibility_audit(
        request, "ruleset.compatibility.create", "ruleset", row.pk)
    return row.on_rest_get(request)


def _compatibility_ruleset_request(request, pk=None):
    """Generic compatibility CRUD with a locked server-owned mode boundary."""
    if request.method == "GET":
        return RuleSet.on_rest_request(request, pk)
    if ((pk is None and request.method != "POST") or
            (pk is not None and request.method not in ("PUT", "PATCH", "DELETE"))):
        return _method_not_allowed()
    from mojo.apps.account.services import fresh_auth

    fresh_auth.require_fresh(request)
    with transaction.atomic():
        if pk is None:
            return _create_compatibility_ruleset(request)
        row = RuleSet.objects.select_for_update().filter(pk=pk).first()
        if row is None:
            raise merrors.ValueException("RuleSet not found", code=404, status=404)
        if request.method == "DELETE":
            response = RuleSet.on_rest_handle_delete(request, row)
            if response.status_code < 400:
                _compatibility_audit(
                    request, "ruleset.compatibility.delete", "ruleset", pk)
            return response
        response = RuleSet.on_rest_handle_save(request, row)
        _compatibility_audit(
            request, "ruleset.compatibility.update", "ruleset", pk)
        return response


def _rule_parent_id(request, fallback=None):
    value = request.DATA.get("parent", fallback)
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise merrors.ValueException("Rule parent must be a positive integer")
    return value


def _compatibility_rule_request(request, pk=None):
    """Child CRUD with old/new parents locked in the same REST transaction."""
    if request.method == "GET":
        return Rule.on_rest_request(request, pk)
    if ((pk is None and request.method != "POST") or
            (pk is not None and request.method not in ("PUT", "PATCH", "DELETE"))):
        return _method_not_allowed()
    from mojo.apps.account.services import fresh_auth

    fresh_auth.require_fresh(request)
    with transaction.atomic():
        if pk is None:
            parent_id = _rule_parent_id(request)
            # The model hook re-checks the mode; this early lock fixes lock
            # ordering across concurrent creates/reparents.
            parents = list(RuleSet.objects.select_for_update().filter(
                pk=parent_id).values_list("pk", flat=True))
            if parents != [parent_id]:
                raise merrors.ValueException("RuleSet parent not found")
            Rule.rest_check_permission_or_raise(
                request, ["CREATE_PERMS", "SAVE_PERMS", "VIEW_PERMS"])
            Rule.rest_resolve_request_graph(request, "default")
            row = Rule.create_from_request(request)
            _compatibility_audit(
                request, "rule.compatibility.create", "rule", row.pk)
            return row.on_rest_get(request)

        observed_parent_id = Rule.objects.filter(pk=pk).values_list(
            "parent_id", flat=True).first()
        if observed_parent_id is None:
            raise merrors.ValueException("Rule not found", code=404, status=404)
        new_parent_id = _rule_parent_id(
            request, fallback=observed_parent_id)
        # Parent-first lock order matches governed aggregate replacement. The
        # child is then locked and its observed parent rechecked, turning a
        # concurrent reparent into a retryable conflict rather than allowing a
        # write under an unlocked aggregate (or deadlocking parent vs child).
        parent_ids = sorted({observed_parent_id, new_parent_id})
        parents = list(RuleSet.objects.select_for_update().filter(
            pk__in=parent_ids).order_by(
                "pk").values_list("pk", flat=True))
        if parents != parent_ids:
            raise merrors.ValueException("RuleSet parent not found")
        row = Rule.objects.select_for_update().filter(pk=pk).first()
        if row is None:
            raise merrors.ValueException("Rule not found", code=404, status=404)
        if row.parent_id != observed_parent_id:
            raise merrors.ValueException(
                "Rule parent changed; reload and retry", code=409, status=409)
        if request.method == "DELETE":
            response = Rule.on_rest_handle_delete(request, row)
            if response.status_code < 400:
                _compatibility_audit(
                    request, "rule.compatibility.delete", "rule", pk)
            return response

        response = Rule.on_rest_handle_save(request, row)
        _compatibility_audit(
            request, "rule.compatibility.update", "rule", pk)
        return response


@md.URL('incident')
@md.URL('incident/<int:pk>')
def on_incident(request, pk=None):
    return Incident.on_rest_request(request, pk)

@md.URL('incident/history')
@md.URL('incident/<int:pk>/history')
def on_incident_history(request, pk=None):
    return IncidentHistory.on_rest_request(request, pk)


@md.URL('event')
@md.URL('event/<int:pk>')
@md.strict_rate_limit('incident_event', ip_limit=240, muid_limit=120)
def on_event(request, pk=None):
    # CREATE_PERMS is ["all"] — any authenticated caller can ingest events, so
    # this heavier path (Event INSERT + rule evaluation per call) gets its own
    # hard bound on top of the global identity throttle (DM-042), including
    # ApiKey callers. Limits are generous because the same route serves
    # security-dashboard reads.
    # NOTE: OSSEC agents do NOT post here (they use incident/ossec/alert/batch,
    # which must stay unlimited — its client parks batches after 3 failed
    # retries, so 429s there mean silently lost security alerts).
    return Event.on_rest_request(request, pk)


@md.GET('health/summary')
@md.requires_global_perms("view_security", "security")
def on_health_summary(request):
    """
    Return the most recent Event per ``system:health:*`` category (or any
    other namespaced category root via the ``prefix`` query param).

    One row per distinct category — used by the portal Security Dashboard's
    Health Strip so it can render an indicator per subsystem without
    hard-coding the category list or making N round-trips.

    The ``prefix`` parameter must be a namespace prefix — non-empty and
    colon-suffixed (``foo:bar:``). This bounds the endpoint to enumerating
    a single namespace root rather than acting as an open-ended category
    discovery oracle for any caller with view_security.
    """
    from mojo.errors import ValueException
    prefix = request.DATA.get("prefix", "system:health:")
    if not isinstance(prefix, str) or not prefix or not prefix.endswith(":"):
        raise ValueException(
            "prefix must be a non-empty namespace prefix ending in ':' (e.g. 'system:health:')",
            400,
        )
    categories = (
        Event.objects
        .filter(category__startswith=prefix)
        .values_list("category", flat=True)
        .distinct()
    )
    data = []
    for category in categories:
        latest = Event.objects.filter(category=category).order_by("-created").first()
        if latest is None:
            continue
        data.append({
            "category": category,
            "level": latest.level,
            "last_seen": latest.created.isoformat() if latest.created else None,
            "title": latest.title,
            "details": latest.details,
            "hostname": latest.hostname,
            "source_ip": latest.source_ip,
            "incident_id": latest.incident_id,
        })
    # Stable ordering for the UI — by category name.
    data.sort(key=lambda row: row["category"])
    return JsonResponse(dict(status=True, data=data))

@md.URL('event/ruleset')
@md.URL('event/ruleset/<int:pk>')
@md.uses_model_security(RuleSet)
def on_event_ruleset(request, pk=None):
    # Compatibility CRUD remains available for established policies. The
    # governed Admin Security aggregate is an additional opt-in lifecycle.
    return _compatibility_ruleset_request(request, pk)

@md.URL('event/ruleset/rule')
@md.URL('event/ruleset/rule/<int:pk>')
@md.uses_model_security(Rule)
def on_event_ruleset_rule(request, pk=None):
    return _compatibility_rule_request(request, pk)
