"""Human feedback and deterministic offline evaluation for MojoSec detectors."""

import hashlib
import json

from django.db import transaction

from mojo.helpers import dates
from mojo.helpers.settings import settings
from mojo.apps.incident.services.mojosec import KIND_POLICY


POLICY_SCHEMA = "mojosec.policy-proposal.v1"
REPLAY_SCHEMA = "replay_features_v1"
MAX_POLICY_BYTES = 16 * 1024
MAX_DETECTORS = 24
MAX_REPLAY_ROWS = 100
MAX_METRIC_ROWS = 1000
MAX_NOTE = 1000
MAX_SUMMARY = 500
ALLOWED_KINDS = frozenset(KIND_POLICY)
ALLOWED_SEVERITIES = ("low", "medium", "high", "critical")
SEVERITY_RANK = {value: index for index, value in enumerate(ALLOWED_SEVERITIES)}
ALLOWED_DECISIONS = ("flag", "ignore")


class MojoSecLearningError(ValueError):
    pass


def _canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest(value):
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _bounded_text(value, field, limit, required=False):
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise MojoSecLearningError(f"{field} must be a string")
    value = value.strip()
    if required and not value:
        raise MojoSecLearningError(f"{field} is required")
    if len(value) > limit:
        raise MojoSecLearningError(f"{field} exceeds {limit} characters")
    return value


def _bounded_int(value, field, minimum, maximum):
    if not isinstance(value, int) or isinstance(value, bool):
        raise MojoSecLearningError(f"{field} must be an integer")
    if value < minimum or value > maximum:
        raise MojoSecLearningError(f"{field} must be between {minimum} and {maximum}")
    return value


def _assert_human_security_author(author, permission="manage_security"):
    if (author is None or not hasattr(author, "is_request_user")
            or not getattr(author, "is_authenticated", False)
            or not author.has_permission([permission, "security"])):
        raise MojoSecLearningError("global human security authorization is required")


def validate_policy_content(content):
    """Return a canonical bounded policy using only fixed scalar predicates."""
    if not isinstance(content, dict):
        raise MojoSecLearningError("content must be an object")
    if set(content) != {"schema", "detectors"}:
        raise MojoSecLearningError("content accepts only schema and detectors")
    if content.get("schema") != POLICY_SCHEMA:
        raise MojoSecLearningError(f"schema must be {POLICY_SCHEMA}")
    detectors = content.get("detectors")
    if not isinstance(detectors, list) or not detectors:
        raise MojoSecLearningError("detectors must be a non-empty list")
    if len(detectors) > MAX_DETECTORS:
        raise MojoSecLearningError(f"detectors is limited to {MAX_DETECTORS} entries")

    normalized = []
    seen = set()
    allowed_keys = {"kind", "decision", "minimum_count", "minimum_severity"}
    for index, detector in enumerate(detectors):
        if not isinstance(detector, dict) or set(detector) - allowed_keys:
            raise MojoSecLearningError(f"detectors[{index}] contains unsupported fields")
        if set(detector) < {"kind", "decision"}:
            raise MojoSecLearningError(f"detectors[{index}] requires kind and decision")
        kind = detector.get("kind")
        if kind not in ALLOWED_KINDS:
            raise MojoSecLearningError(f"detectors[{index}].kind is not allowlisted")
        if kind in seen:
            raise MojoSecLearningError(f"detector kind {kind} appears more than once")
        seen.add(kind)
        decision = detector.get("decision")
        if decision not in ALLOWED_DECISIONS:
            raise MojoSecLearningError(
                f"detectors[{index}].decision must be flag or ignore")
        row = {"kind": kind, "decision": decision}
        if "minimum_count" in detector:
            row["minimum_count"] = _bounded_int(
                detector["minimum_count"], f"detectors[{index}].minimum_count", 1, 10000)
        if "minimum_severity" in detector:
            severity = detector["minimum_severity"]
            if severity not in ALLOWED_SEVERITIES:
                raise MojoSecLearningError(
                    f"detectors[{index}].minimum_severity is not allowlisted")
            row["minimum_severity"] = severity
        normalized.append(row)
    normalized.sort(key=lambda row: row["kind"])
    result = {"schema": POLICY_SCHEMA, "detectors": normalized}
    if len(_canonical_json(result).encode("utf-8")) > MAX_POLICY_BYTES:
        raise MojoSecLearningError("content exceeds the bounded policy size")
    return result


def validate_manual_exemplar(value):
    if not isinstance(value, dict):
        raise MojoSecLearningError("manual_exemplar must be an object")
    allowed = {"kind", "count", "severity"}
    if set(value) - allowed or set(value) < {"kind", "count", "severity"}:
        raise MojoSecLearningError(
            "manual_exemplar requires only kind, count, and severity")
    if value["kind"] not in ALLOWED_KINDS:
        raise MojoSecLearningError("manual_exemplar.kind is not allowlisted")
    if value["severity"] not in ALLOWED_SEVERITIES:
        raise MojoSecLearningError("manual_exemplar.severity is not allowlisted")
    return {
        "kind": value["kind"],
        "count": _bounded_int(value["count"], "manual_exemplar.count", 1, 10000),
        "severity": value["severity"],
    }


def _receipt_snapshot(receipt):
    features = receipt.replay_features if isinstance(receipt.replay_features, dict) else {}
    event_features = features.get("event") if isinstance(features.get("event"), dict) else {}
    event = receipt.event
    return {
        "installation_key_id_snapshot": receipt.api_key_id,
        "detector_kind": str(event_features.get("kind") or "")[:64],
        "detector_category": str(getattr(event, "category", "") or "")[:124],
        "detector_level": max(0, min(15, int(getattr(event, "level", 0) or 0))),
        "sensor_id": receipt.sensor_id,
        "sensor_policy_revision_sha256": hashlib.sha256(
            receipt.sensor_policy_revision.encode("utf-8")).hexdigest()
            if receipt.sensor_policy_revision else "",
        "observed_count": max(1, min(10000, int(event_features.get("count") or 1))),
    }


def _incident_snapshot(incident):
    from mojo.apps.incident.models import MojoSecReceipt

    receipt = MojoSecReceipt.objects.filter(incident=incident).select_related("event").order_by(
        "id").first()
    if receipt is None:
        raise MojoSecLearningError("incident is not backed by MojoSec receipt evidence")
    return _receipt_snapshot(receipt)


def _subject_key(receipt, incident, manual):
    if receipt is not None:
        return f"receipt:{receipt.pk}"
    if incident is not None:
        return f"incident:{incident.pk}"
    return f"manual:{_digest(manual)}"


def _subject_snapshot(receipt, incident, manual):
    if receipt is not None:
        return "receipt", str(receipt.pk)
    if incident is not None:
        return "incident", str(incident.pk)
    digest = _digest(manual)
    return "manual", digest


def create_feedback(author, disposition, receipt_id=None, incident_id=None,
                    manual_exemplar=None, note="", reverses_id=None):
    from mojo.apps.incident.models import (
        Incident, MojoSecDetectorFeedback, MojoSecReceipt)

    _assert_human_security_author(author)
    choices = {item[0] for item in MojoSecDetectorFeedback.DISPOSITIONS}
    if disposition not in choices:
        raise MojoSecLearningError("disposition is not allowlisted")
    note = _bounded_text(note, "note", MAX_NOTE)
    subjects = sum(value is not None for value in (receipt_id, incident_id, manual_exemplar))
    if subjects != 1:
        raise MojoSecLearningError(
            "exactly one of receipt_id, incident_id, or manual_exemplar is required")

    with transaction.atomic():
        receipt = None
        incident = None
        manual = {}
        if receipt_id is not None:
            receipt = MojoSecReceipt.objects.select_for_update().select_related("event").get(
                pk=_bounded_int(receipt_id, "receipt_id", 1, 2 ** 63 - 1))
            snapshot = _receipt_snapshot(receipt)
        elif incident_id is not None:
            incident = Incident.objects.select_for_update().get(
                pk=_bounded_int(incident_id, "incident_id", 1, 2 ** 63 - 1))
            snapshot = _incident_snapshot(incident)
        else:
            manual = validate_manual_exemplar(manual_exemplar)
            policy = KIND_POLICY[manual["kind"]]
            snapshot = {
                "installation_key_id_snapshot": None,
                "detector_kind": manual["kind"],
                "detector_category": policy.get("category", f"mojosec.{manual['kind']}"),
                "detector_level": policy["level"],
                "sensor_id": "",
                "sensor_policy_revision_sha256": "",
                "observed_count": manual["count"],
            }
        subject_key = _subject_key(receipt, incident, manual)
        subject_type, subject_id_snapshot = _subject_snapshot(receipt, incident, manual)
        reverses = None
        if reverses_id is not None:
            reverses = MojoSecDetectorFeedback.objects.select_for_update().get(
                pk=_bounded_int(reverses_id, "reverses_id", 1, 2 ** 63 - 1))
            if reverses.subject_key != subject_key:
                raise MojoSecLearningError("a reversal must keep the same subject")
            if hasattr(reverses, "reversed_by"):
                raise MojoSecLearningError("feedback has already been reversed")
            if not note:
                raise MojoSecLearningError("a reversal requires a bounded note")
            snapshot = {
                "installation_key_id_snapshot": reverses.installation_key_id_snapshot,
                "detector_kind": reverses.detector_kind,
                "detector_category": reverses.detector_category,
                "detector_level": reverses.detector_level,
                "sensor_id": reverses.sensor_id,
                "sensor_policy_revision_sha256": reverses.sensor_policy_revision_sha256,
                "observed_count": reverses.observed_count,
            }
            manual = dict(reverses.manual_exemplar or {})
            subject_type = reverses.subject_type
            subject_id_snapshot = reverses.subject_id_snapshot
        elif MojoSecDetectorFeedback.objects.filter(
                subject_key=subject_key, reversed_by__isnull=True).exists():
            raise MojoSecLearningError(
                "the subject already has current feedback; reverse it explicitly")
        return MojoSecDetectorFeedback.objects.create(
            author=author, receipt=receipt, incident=incident, reverses=reverses,
            disposition=disposition, note=note, subject_key=subject_key,
            subject_type=subject_type, subject_id_snapshot=subject_id_snapshot,
            author_id_snapshot=author.pk, manual_exemplar=manual, **snapshot)


def create_policy_proposal(author, content, summary="", status="draft", supersedes=None):
    from mojo.apps.incident.models import MojoSecPolicyProposal

    _assert_human_security_author(author)
    normalized = validate_policy_content(content)
    summary = _bounded_text(summary, "summary", MAX_SUMMARY)
    statuses = {item[0] for item in MojoSecPolicyProposal.STATUSES}
    if status not in statuses:
        raise MojoSecLearningError("status must be draft, shadow, or rejected")

    with transaction.atomic():
        previous = None
        if supersedes is not None:
            previous = MojoSecPolicyProposal.objects.select_for_update().get(pk=supersedes)
            if previous.status == MojoSecPolicyProposal.REJECTED:
                raise MojoSecLearningError("a rejected proposal cannot be revised")
            if hasattr(previous, "superseded_by"):
                raise MojoSecLearningError("proposal revision has already been superseded")
        return MojoSecPolicyProposal.objects.create(
            created_by=author,
            lineage_id=previous.lineage_id if previous else None,
            revision=previous.revision + 1 if previous else 1,
            supersedes=previous,
            status=status,
            summary=summary,
            content=normalized,
            content_digest=_digest(normalized),
        ) if previous else MojoSecPolicyProposal.objects.create(
            created_by=author, revision=1, status=status, summary=summary,
            content=normalized, content_digest=_digest(normalized))


def _feature_projection(replay_features):
    if not isinstance(replay_features, dict):
        return None
    if replay_features.get("feature_schema") != REPLAY_SCHEMA:
        return None
    event = replay_features.get("event")
    if not isinstance(event, dict):
        return None
    kind = event.get("kind")
    severity = event.get("severity")
    count = event.get("count")
    if kind not in ALLOWED_KINDS or severity not in ALLOWED_SEVERITIES:
        return None
    if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 10000:
        return None
    return {"kind": kind, "severity": severity, "count": count}


def evaluate_features(content, rows):
    """Pure evaluator; rows contain only receipt identity/digest/replay_features."""
    policy = validate_policy_content(content)
    rules = {row["kind"]: row for row in policy["detectors"]}
    totals = {"evaluated": 0, "flagged": 0, "ignored": 0, "unmatched": 0}
    kinds = {}
    decisions = []
    sample = []
    for row in rows[:MAX_REPLAY_ROWS]:
        receipt_id = row["id"]
        payload_digest = row["payload_digest"]
        feature = _feature_projection(row["replay_features"])
        if feature is None:
            continue
        totals["evaluated"] += 1
        rule = rules.get(feature["kind"])
        outcome = "unmatched"
        if rule is not None:
            eligible = feature["count"] >= rule.get("minimum_count", 1)
            minimum = rule.get("minimum_severity")
            if minimum is not None:
                eligible = eligible and SEVERITY_RANK[feature["severity"]] >= SEVERITY_RANK[minimum]
            if eligible:
                outcome = "flagged" if rule["decision"] == "flag" else "ignored"
        totals[outcome] += 1
        kind_metrics = kinds.setdefault(
            feature["kind"], {"evaluated": 0, "flagged": 0, "ignored": 0, "unmatched": 0})
        kind_metrics["evaluated"] += 1
        kind_metrics[outcome] += 1
        sample.append({"id": receipt_id, "payload_digest": payload_digest})
        decisions.append({"id": receipt_id, "outcome": outcome})
    metrics = dict(totals)
    metrics["by_kind"] = {key: kinds[key] for key in sorted(kinds)}
    return {
        "sample_count": len(sample),
        "sample_digest": _digest(sample),
        "result_digest": _digest({"policy": policy, "decisions": decisions}),
        "metrics": metrics,
    }


def evaluate_proposal(author, proposal_id, mode="replay", receipt_ids=None, limit=100):
    from mojo.apps.incident.models import (
        MojoSecPolicyEvaluation, MojoSecPolicyProposal, MojoSecReceipt)

    _assert_human_security_author(author)
    if mode not in (MojoSecPolicyEvaluation.REPLAY, MojoSecPolicyEvaluation.SHADOW):
        raise MojoSecLearningError("mode must be replay or shadow")
    limit = _bounded_int(limit, "limit", 1, MAX_REPLAY_ROWS)
    proposal_id = _bounded_int(proposal_id, "proposal_id", 1, 2 ** 63 - 1)
    proposal = MojoSecPolicyProposal.objects.get(pk=proposal_id)
    content = validate_policy_content(proposal.content)
    if proposal.status == MojoSecPolicyProposal.REJECTED:
        raise MojoSecLearningError("rejected proposals cannot be evaluated")
    if mode == MojoSecPolicyEvaluation.SHADOW and proposal.status != MojoSecPolicyProposal.SHADOW:
        raise MojoSecLearningError("shadow evaluation requires a shadow proposal revision")

    if not isinstance(receipt_ids, list) or not receipt_ids or len(receipt_ids) > limit:
        raise MojoSecLearningError("receipt_ids must be an explicit non-empty bounded list")
    normalized_ids = [
        _bounded_int(value, "receipt_ids entry", 1, 2 ** 63 - 1)
        for value in receipt_ids
    ]
    if len(set(normalized_ids)) != len(normalized_ids):
        raise MojoSecLearningError("receipt_ids must not contain duplicates")
    normalized_ids.sort()
    rows_by_id = {
        row["id"]: row
        for row in MojoSecReceipt.objects.filter(
            publish_state=MojoSecReceipt.PUBLISH_PUBLISHED,
            pk__in=normalized_ids,
        ).values("id", "payload_digest", "replay_features")
    }
    if set(rows_by_id) != set(normalized_ids):
        raise MojoSecLearningError("every receipt_id must identify retained published evidence")
    rows = [rows_by_id[value] for value in normalized_ids]
    result = evaluate_features(content, rows)
    if result["sample_count"] != len(rows):
        raise MojoSecLearningError("every receipt_id must contain replay_features_v1")
    return MojoSecPolicyEvaluation.objects.create(
        proposal=proposal, created_by=author, mode=mode,
        sample_count=result["sample_count"], sample_digest=result["sample_digest"],
        result_digest=result["result_digest"], metrics=result["metrics"])


def detector_metrics(author, days=30, limit=1000):
    from mojo.apps.incident.models import MojoSecDetectorFeedback, MojoSecReceipt

    _assert_human_security_author(author, permission="view_security")
    days = _bounded_int(days, "days", 1, 365)
    limit = _bounded_int(limit, "limit", 1, MAX_METRIC_ROWS)
    cutoff = dates.utcnow() - dates.timedelta(days=days)
    receipts = list(MojoSecReceipt.objects.filter(created__gte=cutoff).order_by("-created").values(
        "id", "replay_features")[:limit])
    feedback = list(MojoSecDetectorFeedback.objects.filter(
        created__gte=cutoff, reversed_by__isnull=True).order_by("-created").values(
            "detector_kind", "disposition")[:limit])
    kinds = {}
    for receipt in receipts:
        feature = _feature_projection(receipt["replay_features"])
        if feature is None:
            continue
        row = kinds.setdefault(feature["kind"], {"receipts": 0, "occurrences": 0, "feedback": {}})
        row["receipts"] += 1
        row["occurrences"] += feature["count"]
    for item in feedback:
        row = kinds.setdefault(item["detector_kind"], {"receipts": 0, "occurrences": 0, "feedback": {}})
        disposition = item["disposition"]
        row["feedback"][disposition] = row["feedback"].get(disposition, 0) + 1
    return {
        "window_days": days,
        "receipt_sample_limit": limit,
        "feedback_sample_limit": limit,
        "receipt_rows_scanned": len(receipts),
        "feedback_rows_scanned": len(feedback),
        "detectors": {key: kinds[key] for key in sorted(kinds)},
    }


def prune_learning_evaluations(job=None, now=None):
    from mojo.apps.incident.models import MojoSecPolicyEvaluation

    retention_days = settings.get_static(
        "MOJOSEC_LEARNING_EVALUATION_RETENTION_DAYS", 90, kind="int")
    retention_days = max(30, min(3650, retention_days))
    cutoff = (now or dates.utcnow()) - dates.timedelta(days=retention_days)
    deleted, _ = MojoSecPolicyEvaluation.objects.filter(created__lt=cutoff).delete()
    if job is not None and hasattr(job, "add_log"):
        job.add_log(f"Pruned {deleted} expired MojoSec offline evaluation rows")
    return deleted
