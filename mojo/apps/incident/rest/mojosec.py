import json

from django.core.exceptions import ObjectDoesNotExist
from django.http import HttpResponse

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
