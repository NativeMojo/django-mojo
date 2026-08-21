import json

from django.core.exceptions import ObjectDoesNotExist
from django.http import HttpResponse
from django.db.models import Count, Sum

from mojo import decorators as md
from mojo.apps.account.models import ApiKey
from mojo.apps.incident.services import mojosec
from mojo.apps.incident.services import mojosec_learning
from mojo import errors as merrors


def _json_response(payload, status=200):
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return HttpResponse(body, status=status, content_type="application/json")


@md.POST("mojosec/batch")
def on_mojosec_batch(request):
    api_key = getattr(request, "api_key", None)
    if (request.bearer != "apikey" or not isinstance(api_key, ApiKey) or
            not api_key.group.is_effectively_active() or
            not api_key.has_permission("mojosec_ingest")):
        return _json_response({"error": "unauthorized"}, status=403)
    try:
        batch = mojosec.parse_request_batch(request)
        mojosec.sensor_profile(api_key, batch)
    except mojosec.MojoSecIngestError as err:
        return _json_response({"error": err.reason}, status=err.status)
    return _json_response(mojosec.ingest_batch(api_key, batch))


def _learning_error(func):
    try:
        return func()
    except (mojosec_learning.MojoSecLearningError, ObjectDoesNotExist, ValueError) as err:
        raise merrors.ValueException(str(err)) from err


def _int_param(value, default):
    if value in (None, ""):
        return default
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return value


def _proposal_data(proposal):
    return {
        "id": proposal.pk,
        "created": proposal.created,
        "lineage_id": str(proposal.lineage_id),
        "revision": proposal.revision,
        "status": proposal.status,
        "summary": proposal.summary,
        "content": proposal.content,
        "content_digest": proposal.content_digest,
        "supersedes_id": proposal.supersedes_id,
    }


@md.POST("mojosec/feedback")
@md.requires_global_perms("manage_security", "security")
def on_mojosec_feedback(request):
    feedback = _learning_error(lambda: mojosec_learning.create_feedback(
        request.user,
        request.DATA.get("disposition"),
        receipt_id=request.DATA.get("receipt_id"),
        incident_id=request.DATA.get("incident_id"),
        manual_exemplar=request.DATA.get("manual_exemplar"),
        note=request.DATA.get("note", ""),
        reverses_id=request.DATA.get("reverses_id"),
    ))
    return {
        "status": True,
        "data": {
            "id": feedback.pk,
            "created": feedback.created,
            "disposition": feedback.disposition,
            "subject_key": feedback.subject_key,
            "reverses_id": feedback.reverses_id,
            "detector_kind": feedback.detector_kind,
        },
    }


@md.POST("mojosec/proposal")
@md.requires_global_perms("manage_security", "security")
def on_mojosec_proposal(request):
    proposal = _learning_error(lambda: mojosec_learning.create_policy_proposal(
        request.user,
        request.DATA.get("content"),
        summary=request.DATA.get("summary", ""),
        status=request.DATA.get("status", "draft"),
        supersedes=request.DATA.get("supersedes_id"),
    ))
    return {"status": True, "data": _proposal_data(proposal)}


def _evaluate(request, mode):
    evaluation = _learning_error(lambda: mojosec_learning.evaluate_proposal(
        request.user,
        request.DATA.get("proposal_id"),
        mode=mode,
        receipt_ids=request.DATA.get("receipt_ids"),
        limit=_int_param(request.DATA.get("limit"), 100),
    ))
    return {
        "status": True,
        "data": {
            "id": evaluation.pk,
            "created": evaluation.created,
            "proposal_id": evaluation.proposal_id,
            "mode": evaluation.mode,
            "sample_count": evaluation.sample_count,
            "sample_digest": evaluation.sample_digest,
            "result_digest": evaluation.result_digest,
            "metrics": evaluation.metrics,
        },
    }


@md.POST("mojosec/replay")
@md.requires_global_perms("manage_security", "security")
def on_mojosec_replay(request):
    return _evaluate(request, "replay")


@md.POST("mojosec/shadow")
@md.requires_global_perms("manage_security", "security")
def on_mojosec_shadow(request):
    return _evaluate(request, "shadow")


@md.GET("mojosec/metrics")
@md.requires_global_perms("view_security", "security")
def on_mojosec_metrics(request):
    metrics = _learning_error(lambda: mojosec_learning.detector_metrics(
        request.user,
        days=_int_param(request.DATA.get("days"), 30),
        limit=_int_param(request.DATA.get("limit"), 1000),
    ))
    return {"status": True, "data": metrics}


def _bounded_positive(value, default, maximum, name="value"):
    value = _int_param(value, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise merrors.ValueException(f"{name} must be a positive integer")
    if value > maximum:
        raise merrors.ValueException(f"{name} must be at most {maximum}")
    return value


def _iso(value):
    # The plain-dict response path (objict.to_json) cannot serialize
    # datetimes nested inside lists — bounded ISO strings, explicitly.
    return value.isoformat() if hasattr(value, "isoformat") else value


def _case_row(case, detail=False):
    row = {
        "id": case.pk,
        "created": _iso(case.created),
        "first_seen": _iso(case.first_seen),
        "last_seen": _iso(case.last_seen),
        "window_start": _iso(case.window_start),
        "window_end": _iso(case.window_end),
        "sensor_kind": case.sensor_kind,
        "resource_id": case.resource_id,
        "family": case.family,
        "state": case.state,
        "state_reason": case.state_reason,
        "urgency": case.urgency,
        "urgency_reason": case.urgency_reason,
        "occurrence_count": case.occurrence_count,
        "receipt_count": case.receipt_count,
        "projected_event_count": case.projected_event_count,
        "distinct_count": case.distinct_count,
        "sample_count": case.sample_count,
        "overflow_count": case.overflow_count,
        "policy_version": case.policy_version,
        "evaluator_version": case.evaluator_version,
        "deployment_id": case.deployment_id,
        "campaign_id": case.campaign_id,
        "distinct_source_count": case.distinct_source_count,
    }
    if detail:
        row["sensor_id"] = case.sensor_id
        row["network"] = case.network
        row["settled_at"] = _iso(case.settled_at) if case.settled_at else None
        row["projected_urgency"] = case.projected_urgency
        row["breakdown"] = (
            case.breakdown if isinstance(case.breakdown, dict) else {})
        row["samples"] = list(case.samples)[:8] if isinstance(case.samples, list) else []
        row["transitions"] = [
            {
                "id": item.pk,
                "created": _iso(item.created),
                "transition": item.transition,
                "reason": item.reason,
                "from_state": item.from_state,
                "to_state": item.to_state,
                "from_urgency": item.from_urgency,
                "to_urgency": item.to_urgency,
                "occurrence_count": item.occurrence_count,
                "receipt_count": item.receipt_count,
            }
            for item in case.transitions.order_by("-created", "-id")[:50]
        ]
    return row


@md.GET("mojosec/case")
@md.GET("mojosec/case/<int:pk>")
@md.requires_global_perms("view_security", "security")
def on_mojosec_case(request, pk=None):
    from mojo.apps.incident.models import MojoSecCase

    queryset = MojoSecCase.objects.all()
    if pk is not None:
        case = queryset.filter(pk=pk).first()
        if case is None:
            raise merrors.RestErrorException(
                "MojoSec case does not exist", code=404, status=404)
        return {"status": True, "data": _case_row(case, detail=True)}
    filters = {
        "state": ("observing", "elevated", "settled"),
        "urgency": ("info", "warning", "high", "critical"),
        "sensor_kind": ("web", "fim", "auth", "host", "campaign"),
    }
    for field, allowed in filters.items():
        value = request.DATA.get(field)
        if value not in (None, ""):
            if value not in allowed:
                raise merrors.ValueException(f"unknown MojoSec case {field}")
            queryset = queryset.filter(**{field: value})
    resource_id = request.DATA.get("resource_id")
    if resource_id not in (None, ""):
        if (not isinstance(resource_id, str) or len(resource_id) > 96 or
                not resource_id.startswith(("vhost:", "installation:", "user:"))):
            raise merrors.ValueException("invalid MojoSec case resource_id")
        queryset = queryset.filter(resource_id=resource_id)
    campaign_id = request.DATA.get("campaign_id")
    if campaign_id not in (None, ""):
        if not (isinstance(campaign_id, int) or
                (isinstance(campaign_id, str) and campaign_id.isdigit())):
            raise merrors.ValueException("invalid MojoSec case campaign_id")
        queryset = queryset.filter(campaign_id=int(campaign_id))
    for field, limit in (("family", 64), ("deployment_id", 128)):
        value = request.DATA.get(field)
        if value not in (None, ""):
            if not isinstance(value, str) or len(value) > limit:
                raise merrors.ValueException(f"invalid MojoSec case {field}")
            queryset = queryset.filter(**{field: value})
    page = _bounded_positive(request.DATA.get("page"), 1, 100, name="page")
    page_size = _bounded_positive(
        request.DATA.get("page_size"), 50, 100, name="page_size")
    start = (page - 1) * page_size
    rows = list(queryset.order_by("-last_seen", "-id")[start:start + page_size + 1])
    return {
        "status": True,
        "data": [_case_row(case) for case in rows[:page_size]],
        "page": page,
        "page_size": page_size,
        "has_more": len(rows) > page_size,
    }


@md.GET("mojosec/case-metrics")
@md.requires_global_perms("view_security", "security")
def on_mojosec_case_metrics(request):
    from mojo.apps.incident.models import MojoSecCase
    from mojo.apps.incident.services import mojosec_correlation
    from mojo.helpers import dates

    days = _bounded_positive(request.DATA.get("days"), 1, 90, name="days")
    now = dates.utcnow()
    queryset = MojoSecCase.objects.filter(
        last_seen__gte=now - dates.timedelta(days=days),
        last_seen__lte=now + dates.timedelta(
            seconds=mojosec_correlation.future_skew_seconds()))
    resource_id = request.DATA.get("resource_id")
    if resource_id not in (None, ""):
        if not isinstance(resource_id, str) or len(resource_id) > 96:
            raise merrors.ValueException("invalid MojoSec metrics resource_id")
        queryset = queryset.filter(resource_id=resource_id)
    totals = queryset.aggregate(
        cases=Count("id"), occurrences=Sum("occurrence_count"),
        receipts=Sum("receipt_count"), projected_events=Sum("projected_event_count"),
        distinct=Sum("distinct_count"), overflows=Sum("overflow_count"))
    by_urgency = {
        row["urgency"]: row["count"]
        for row in queryset.values("urgency").annotate(count=Count("id"))
    }
    occurrences = totals["occurrences"] or 0
    cases = totals["cases"] or 0
    settled = queryset.filter(state="settled").count()
    from mojo.apps.incident.models import MojoSecReceipt

    suppressed_query = MojoSecReceipt.objects.filter(
        case_routed=True,
        publish_state=MojoSecReceipt.PUBLISH_PUBLISHED,
        case_contributed_at__gte=now - dates.timedelta(days=days))
    if resource_id not in (None, ""):
        suppressed_query = suppressed_query.filter(
            mojosec_case__resource_id=resource_id)
    from mojo.apps.incident.models import (
        MojoSecRecommendation, MojoSecRecommendationTarget)

    recommendations = MojoSecRecommendation.objects.filter(
        created__gte=now - dates.timedelta(days=days))
    target_rows = MojoSecRecommendationTarget.objects.filter(
        recommendation__created__gte=now - dates.timedelta(days=days))
    return {
        "status": True,
        "data": {
            "days": days,
            "cases": cases,
            "occurrences": occurrences,
            "receipts": totals["receipts"] or 0,
            "projected_events": totals["projected_events"] or 0,
            "distinct": totals["distinct"] or 0,
            "overflows": totals["overflows"] or 0,
            "settled": settled,
            "suppressed_events": suppressed_query.count(),
            "compression_ratio": round(occurrences / cases, 2) if cases else 0,
            "by_urgency": by_urgency,
            "recommendations": recommendations.count(),
            "targets_applied": target_rows.filter(outcome="applied").count(),
            "targets_pre_existing": target_rows.filter(
                outcome="pre_existing").count(),
            "targets_protected": target_rows.filter(
                validation_state="protected").count(),
        },
    }


def _recommendation_row(recommendation, detail=False):
    row = {
        "id": recommendation.pk,
        "created": _iso(recommendation.created),
        "modified": _iso(recommendation.modified),
        "case_id": recommendation.case_id,
        "action": recommendation.action,
        "state": recommendation.state,
        "reason_code": recommendation.reason_code,
        "confidence": recommendation.confidence,
        "urgency": recommendation.urgency,
        "requested_scope": recommendation.requested_scope,
        "requested_ttl_seconds": recommendation.requested_ttl_seconds,
        "expires_at": _iso(recommendation.expires_at),
        "approved_at": _iso(recommendation.approved_at)
        if recommendation.approved_at else None,
        "target_count": recommendation.target_count,
        "validated_count": recommendation.validated_count,
        "protected_count": recommendation.protected_count,
        "executed_count": recommendation.executed_count,
        "failed_count": recommendation.failed_count,
        "reversed_count": recommendation.reversed_count,
        "policy_version": recommendation.policy_version,
        "evaluator_version": recommendation.evaluator_version,
    }
    if detail:
        row["explanation"] = recommendation.explanation
        row["approval_note"] = recommendation.approval_note
        row["collateral"] = recommendation.collateral
        row["approved_by"] = (
            recommendation.approved_by.username
            if recommendation.approved_by_id else None)
        row["targets"] = [
            {
                "id": target.pk, "ip": target.ip, "kind": target.kind,
                "validation_state": target.validation_state,
                "validation_reason": target.validation_reason,
                "outcome": target.outcome, "attempts": target.attempts,
                "last_error": target.last_error,
                "applied_at": _iso(target.applied_at) if target.applied_at else None,
                "expires_at": _iso(target.expires_at) if target.expires_at else None,
                "reversed_at": _iso(target.reversed_at) if target.reversed_at else None,
                "prior_blocked_until": _iso(target.prior_blocked_until)
                if target.prior_blocked_until else None,
                "prior_reason": target.prior_reason,
            }
            for target in recommendation.targets.order_by("id")[:512]
        ]
        row["transitions"] = [
            {
                "id": item.pk, "created": _iso(item.created),
                "transition": item.transition, "reason": item.reason,
                "from_state": item.from_state, "to_state": item.to_state,
            }
            for item in recommendation.transitions.order_by(
                "-created", "-id")[:50]
        ]
        row["attempts"] = [
            {
                "id": item.pk, "created": _iso(item.created),
                "target_id": item.target_id,
                "attempt_number": item.attempt_number,
                "outcome": item.outcome, "detail": item.detail,
            }
            for item in recommendation.attempts.order_by(
                "-created", "-id")[:50]
        ]
    return row


@md.GET("mojosec/recommendation")
@md.GET("mojosec/recommendation/<int:pk>")
@md.requires_global_perms("view_security", "manage_security", "security")
def on_mojosec_recommendation(request, pk=None):
    from mojo.apps.incident.models import MojoSecRecommendation

    queryset = MojoSecRecommendation.objects.all()
    if pk is not None:
        recommendation = queryset.filter(pk=pk).first()
        if recommendation is None:
            raise merrors.RestErrorException(
                "MojoSec recommendation does not exist", code=404, status=404)
        return {"status": True,
                "data": _recommendation_row(recommendation, detail=True)}
    filters = {
        "state": ("proposed", "approved", "auto_approved", "executing",
                  "executed", "failed", "expired", "reversed", "rejected"),
        "action": ("temporary_block_ip", "temporary_block_ip_set"),
        "urgency": ("info", "warning", "high", "critical"),
        "confidence": ("low", "medium", "high"),
    }
    for field, allowed in filters.items():
        value = request.DATA.get(field)
        if value not in (None, ""):
            if value not in allowed:
                raise merrors.ValueException(
                    f"unknown MojoSec recommendation {field}")
            queryset = queryset.filter(**{field: value})
    case_id = request.DATA.get("case_id")
    if case_id not in (None, ""):
        if not (isinstance(case_id, int) or
                (isinstance(case_id, str) and case_id.isdigit())):
            raise merrors.ValueException(
                "invalid MojoSec recommendation case_id")
        queryset = queryset.filter(case_id=int(case_id))
    page = _bounded_positive(request.DATA.get("page"), 1, 100, name="page")
    page_size = _bounded_positive(
        request.DATA.get("page_size"), 50, 100, name="page_size")
    start = (page - 1) * page_size
    rows = list(queryset.order_by("-created", "-id")[start:start + page_size + 1])
    return {
        "status": True,
        "data": [_recommendation_row(item) for item in rows[:page_size]],
        "page": page,
        "page_size": page_size,
        "has_more": len(rows) > page_size,
    }


@md.POST("mojosec/recommendation-action")
@md.requires_global_perms("manage_security", "security")
def on_mojosec_recommendation_action(request):
    """Approve/reject/cancel/reverse exactly what was proposed.

    Static path by convention (dynamic segments go last); the target
    recommendation rides request.DATA. No parameter can add targets or
    widen scope — approval approves the proposal verbatim.
    """
    from mojo.apps.incident.models import MojoSecRecommendation
    from mojo.apps.incident.services import mojosec_actions

    recommendation_id = request.DATA.get("recommendation_id")
    if not (isinstance(recommendation_id, int) or
            (isinstance(recommendation_id, str) and
             recommendation_id.isdigit())):
        raise merrors.ValueException("recommendation_id must be an integer")
    action = request.DATA.get("action")
    note = request.DATA.get("note", "")
    if not isinstance(note, str) or len(note) > 256:
        raise merrors.ValueException("note must be a string of at most 256")
    recommendation = MojoSecRecommendation.objects.filter(
        pk=int(recommendation_id)).first()
    if recommendation is None:
        raise merrors.RestErrorException(
            "MojoSec recommendation does not exist", code=404, status=404)
    try:
        if action == "approve":
            recommendation = mojosec_actions.approve(
                recommendation, request.user, note=note)
        elif action == "reject":
            recommendation = mojosec_actions.reject(
                recommendation, request.user, note=note)
        elif action == "cancel":
            recommendation = mojosec_actions.reject(
                recommendation, request.user, note=note,
                transition="cancelled")
        elif action == "reverse":
            recommendation = mojosec_actions.reverse(
                recommendation, request.user, note=note)
        else:
            raise merrors.ValueException(
                "action must be approve, reject, cancel or reverse")
    except ValueError as err:
        raise merrors.ValueException(str(err)) from err
    return {"status": True,
            "data": _recommendation_row(recommendation, detail=True)}


@md.GET("mojosec/deployment")
@md.requires_global_perms("view_security", "manage_security", "security")
def on_mojosec_deployment_list(request):
    from mojo.apps.incident.models import MojoSecDeployment

    queryset = MojoSecDeployment.objects.all()
    installation_key_id = request.DATA.get("installation_key_id")
    if installation_key_id not in (None, ""):
        if not (isinstance(installation_key_id, int) or
                (isinstance(installation_key_id, str) and
                 installation_key_id.isdigit())):
            raise merrors.ValueException("invalid installation_key_id")
        queryset = queryset.filter(
            installation_key_id=int(installation_key_id))
    page = _bounded_positive(request.DATA.get("page"), 1, 100, name="page")
    page_size = _bounded_positive(
        request.DATA.get("page_size"), 50, 100, name="page_size")
    start = (page - 1) * page_size
    rows = list(queryset.order_by("-created")[start:start + page_size + 1])
    return {
        "status": True,
        "data": [
            {
                "id": row.pk, "created": _iso(row.created),
                "installation_key_id": row.installation_key_id,
                "deployment_id": row.deployment_id,
                "expires_at": _iso(row.expires_at),
                "registered_by": (
                    row.registered_by.username if row.registered_by_id
                    else None),
                "note": row.note,
            }
            for row in rows[:page_size]
        ],
        "page": page,
        "page_size": page_size,
        "has_more": len(rows) > page_size,
    }


@md.POST("mojosec/deployment")
@md.requires_global_perms("manage_security", "security")
def on_mojosec_deployment_register(request):
    """Driver-side pre-registration of one deployment identity.

    Deliberately an operator/driver API: the node journal has no network
    channel, and a node-originated registration would still be root-asserted
    — the point is an identity the sensor cannot mint for itself.
    """
    import datetime as _dt
    import re as _re

    from mojo.apps.account.models import ApiKey
    from mojo.apps.incident.models import MojoSecDeployment
    from mojo.helpers import dates

    installation_key_id = request.DATA.get("installation_key_id")
    if not (isinstance(installation_key_id, int) or
            (isinstance(installation_key_id, str) and
             installation_key_id.isdigit())):
        raise merrors.ValueException("installation_key_id must be an integer")
    api_key = ApiKey.objects.filter(pk=int(installation_key_id)).first()
    if api_key is None:
        raise merrors.RestErrorException(
            "installation key does not exist", code=404, status=404)
    deployment_id = request.DATA.get("deployment_id")
    if (not isinstance(deployment_id, str) or
            not _re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}",
                              deployment_id)):
        raise merrors.ValueException("invalid deployment_id")
    ttl = request.DATA.get("ttl_seconds", 86400)
    if isinstance(ttl, str) and ttl.isdigit():
        ttl = int(ttl)
    if not isinstance(ttl, int) or isinstance(ttl, bool) or \
            not 300 <= ttl <= 604800:
        raise merrors.ValueException("ttl_seconds must be 300-604800")
    note = request.DATA.get("note", "")
    if not isinstance(note, str) or len(note) > 256:
        raise merrors.ValueException("note must be a string of at most 256")
    row, created = MojoSecDeployment.objects.update_or_create(
        installation_key=api_key, deployment_id=deployment_id,
        defaults={
            "registered_by": request.user,
            "expires_at": dates.utcnow() + _dt.timedelta(seconds=ttl),
            "note": note,
        })
    return {
        "status": True,
        "data": {
            "id": row.pk, "created": created,
            "installation_key_id": row.installation_key_id,
            "deployment_id": row.deployment_id,
            "expires_at": _iso(row.expires_at),
        },
    }
