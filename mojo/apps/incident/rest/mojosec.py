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


def _bounded_positive(value, default, maximum):
    value = _int_param(value, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise merrors.ValueException("pagination values must be positive integers")
    return min(value, maximum)


def _case_row(case, detail=False):
    row = {
        "id": case.pk,
        "created": case.created,
        "first_seen": case.first_seen,
        "last_seen": case.last_seen,
        "window_start": case.window_start,
        "window_end": case.window_end,
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
    }
    if detail:
        row["sensor_id"] = case.sensor_id
        row["network"] = case.network
        row["samples"] = list(case.samples)[:8] if isinstance(case.samples, list) else []
        row["transitions"] = [
            {
                "id": item.pk,
                "created": item.created,
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
        "state": ("observing", "elevated"),
        "urgency": ("info", "warning", "high", "critical"),
        "sensor_kind": ("web", "fim"),
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
                not resource_id.startswith(("vhost:", "installation:"))):
            raise merrors.ValueException("invalid MojoSec case resource_id")
        queryset = queryset.filter(resource_id=resource_id)
    page = _bounded_positive(request.DATA.get("page"), 1, 1000000)
    page_size = _bounded_positive(request.DATA.get("page_size"), 50, 100)
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
    from mojo.helpers import dates

    days = _bounded_positive(request.DATA.get("days"), 1, 90)
    queryset = MojoSecCase.objects.filter(
        last_seen__gte=dates.utcnow() - dates.timedelta(days=days))
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
            "compression_ratio": round(occurrences / cases, 2) if cases else 0,
            "by_urgency": by_urgency,
        },
    }
