"""Human feedback and deterministic offline evaluation for MojoSec detectors."""

import hashlib
import json
import secrets

from django.db import transaction

from mojo.helpers import dates
from mojo.helpers.settings import settings
from mojo.apps.incident.services.mojosec import KIND_POLICY, UNKNOWN_KIND_POLICY


POLICY_SCHEMA = "mojosec.policy-proposal.v1"
REPLAY_SCHEMA = "replay_features_v1"
EVALUATOR_SCHEMA = "mojosec.offline-evaluator"
EVALUATOR_VERSION = 1
MAX_POLICY_BYTES = 16 * 1024
MAX_DETECTORS = 24
MAX_REPLAY_ROWS = 100
MAX_METRIC_ROWS = 1000
MAX_METRIC_ROWS_PER_INSTALLATION = 100
MAX_METRIC_CANDIDATES = 4000
MAX_NOTE = 1000
MAX_SUMMARY = 500
ALLOWED_KINDS = frozenset(KIND_POLICY)
ALLOWED_SEVERITIES = ("info", "warning", "high", "critical")
SEVERITY_RANK = {value: index for index, value in enumerate(ALLOWED_SEVERITIES)}
ALLOWED_DECISIONS = ("flag", "ignore")


class MojoSecLearningError(ValueError):
    pass


def _canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest(value):
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _kind_policy_registry_digest():
    return _digest({
        "kind_policy": {key: KIND_POLICY[key] for key in sorted(KIND_POLICY)},
        "unknown_kind_policy": UNKNOWN_KIND_POLICY,
    })


def _evaluator_provenance():
    return {
        "schema": EVALUATOR_SCHEMA,
        "version": EVALUATOR_VERSION,
        "kind_policy_registry_digest": _kind_policy_registry_digest(),
    }


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
    kind = str(event_features.get("kind") or "")[:64]
    if not kind:
        raise MojoSecLearningError("receipt has no retained detector kind")
    policy = KIND_POLICY.get(kind, UNKNOWN_KIND_POLICY)
    return {
        "installation_key_id_snapshot": receipt.api_key_id,
        "detector_kind": kind,
        "detector_category": str(
            policy.get("category", f"mojosec.{kind}"))[:124],
        "detector_level": policy["level"],
        "sensor_id": receipt.sensor_id,
        "sensor_policy_revision_sha256": hashlib.sha256(
            receipt.sensor_policy_revision.encode("utf-8")).hexdigest()
            if receipt.sensor_policy_revision else "",
        "observed_count": max(1, min(10000, int(event_features.get("count") or 1))),
        "receipt_wire_event_id_snapshot": receipt.wire_event_id,
        "incident_id_snapshot": receipt.incident_id,
    }


def _subject_key(receipt, incident, manual):
    if receipt is not None:
        return f"receipt:{receipt.pk}"
    return f"manual:{_digest(manual)}"


def _subject_snapshot(receipt, incident, manual):
    if receipt is not None:
        return "receipt", str(receipt.pk)
    digest = _digest(manual)
    return "manual", digest


def _locked_feedback_head(subject_key):
    from mojo.apps.incident.models import MojoSecDetectorFeedbackHead

    # Django's get_or_create retries a unique-key insert race in its own
    # savepoint. Lock the resulting stable row before inspecting its pointer.
    MojoSecDetectorFeedbackHead.objects.get_or_create(subject_key=subject_key)
    return MojoSecDetectorFeedbackHead.objects.select_for_update().get(
        subject_key=subject_key)


def create_feedback(author, disposition, receipt_id=None, incident_id=None,
                    manual_exemplar=None, note="", reverses_id=None):
    from mojo.apps.incident.models import (
        Incident, MojoSecDetectorFeedback, MojoSecReceipt)

    _assert_human_security_author(author)
    choices = {item[0] for item in MojoSecDetectorFeedback.DISPOSITIONS}
    if disposition not in choices:
        raise MojoSecLearningError("disposition is not allowlisted")
    note = _bounded_text(note, "note", MAX_NOTE)
    subjects = sum(value is not None for value in (receipt_id, manual_exemplar))
    if incident_id is not None and receipt_id is None:
        raise MojoSecLearningError(
            "incident context requires an explicit MojoSec receipt exemplar")
    if subjects != 1 and not (reverses_id is not None and subjects == 0):
        raise MojoSecLearningError(
            "exactly one subject is required, unless a reversal inherits its prior subject")

    with transaction.atomic():
        reverses = None
        if reverses_id is not None:
            reverses = MojoSecDetectorFeedback.objects.select_for_update().get(
                pk=_bounded_int(reverses_id, "reverses_id", 1, 2 ** 63 - 1))
            reverses.assert_integrity()
            if not note:
                raise MojoSecLearningError("a reversal requires a bounded note")
        receipt = None
        incident = None
        manual = {}
        if subjects == 0:
            receipt = reverses.receipt
            incident = reverses.incident
            manual = dict(reverses.manual_exemplar or {})
            snapshot = {
                "installation_key_id_snapshot": reverses.installation_key_id_snapshot,
                "detector_kind": reverses.detector_kind,
                "detector_category": reverses.detector_category,
                "detector_level": reverses.detector_level,
                "sensor_id": reverses.sensor_id,
                "sensor_policy_revision_sha256": reverses.sensor_policy_revision_sha256,
                "observed_count": reverses.observed_count,
                "receipt_wire_event_id_snapshot": reverses.receipt_wire_event_id_snapshot,
                "incident_id_snapshot": reverses.incident_id_snapshot,
            }
            subject_key = reverses.subject_key
            subject_type = reverses.subject_type
            subject_id_snapshot = reverses.subject_id_snapshot
        elif receipt_id is not None:
            # Event is nullable; PostgreSQL refuses FOR UPDATE on that outer
            # joined side, so lock the receipt alone and resolve Event after.
            receipt = MojoSecReceipt.objects.select_for_update().get(
                pk=_bounded_int(receipt_id, "receipt_id", 1, 2 ** 63 - 1))
            if receipt.publish_state != MojoSecReceipt.PUBLISH_PUBLISHED:
                raise MojoSecLearningError("receipt exemplar must be published")
            if incident_id is not None:
                context_id = _bounded_int(incident_id, "incident_id", 1, 2 ** 63 - 1)
                if receipt.incident_id != context_id:
                    raise MojoSecLearningError(
                        "incident context must match the explicit receipt exemplar")
                incident = Incident.objects.select_for_update().get(pk=context_id)
            snapshot = _receipt_snapshot(receipt)
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
                "receipt_wire_event_id_snapshot": "",
                "incident_id_snapshot": None,
            }
        if subjects:
            subject_key = _subject_key(receipt, incident, manual)
            subject_type, subject_id_snapshot = _subject_snapshot(receipt, incident, manual)
        if reverses is not None:
            if reverses.subject_key != subject_key:
                raise MojoSecLearningError("a reversal must keep the same subject")
            snapshot = {
                "installation_key_id_snapshot": reverses.installation_key_id_snapshot,
                "detector_kind": reverses.detector_kind,
                "detector_category": reverses.detector_category,
                "detector_level": reverses.detector_level,
                "sensor_id": reverses.sensor_id,
                "sensor_policy_revision_sha256": reverses.sensor_policy_revision_sha256,
                "observed_count": reverses.observed_count,
                "receipt_wire_event_id_snapshot": reverses.receipt_wire_event_id_snapshot,
                "incident_id_snapshot": reverses.incident_id_snapshot,
            }
            manual = dict(reverses.manual_exemplar or {})
            subject_type = reverses.subject_type
            subject_id_snapshot = reverses.subject_id_snapshot
        head = _locked_feedback_head(subject_key)
        if reverses is not None and head.current_id != reverses.pk:
            raise MojoSecLearningError("only the current feedback may be reversed")
        if reverses is None and head.current_id is not None:
            raise MojoSecLearningError(
                "the subject already has current feedback; reverse it explicitly")
        feedback = MojoSecDetectorFeedback.objects.create(
            author=author, receipt=receipt, incident=incident, reverses=reverses,
            disposition=disposition, note=note, subject_key=subject_key,
            subject_type=subject_type, subject_id_snapshot=subject_id_snapshot,
            author_id_snapshot=author.pk,
            author_identity_snapshot=str(
                getattr(author, "username", None) or getattr(author, "email", author.pk)
            )[:254],
            manual_exemplar=manual, **snapshot)
        try:
            head.advance(feedback)
        except ValueError as exc:
            raise MojoSecLearningError(str(exc)) from exc
        return feedback


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
            previous.assert_integrity()
            if previous.content_digest != _digest(validate_policy_content(previous.content)):
                raise MojoSecLearningError("superseded proposal content digest does not match")
            if previous.status == MojoSecPolicyProposal.REJECTED:
                raise MojoSecLearningError("a rejected proposal cannot be revised")
            if hasattr(previous, "superseded_by"):
                raise MojoSecLearningError("proposal revision has already been superseded")
        values = {
            "created_by": author,
            "revision": previous.revision + 1 if previous else 1,
            "supersedes": previous,
            "status": status,
            "summary": summary,
            "content": normalized,
            "content_digest": _digest(normalized),
        }
        if previous:
            values["lineage_id"] = previous.lineage_id
        return MojoSecPolicyProposal.objects.create(**values)


def _feature_projection(replay_features):
    if not isinstance(replay_features, dict):
        return None
    if replay_features.get("feature_schema") != REPLAY_SCHEMA:
        return None
    event = replay_features.get("event")
    if not isinstance(event, dict):
        return None
    kind = event.get("kind")
    count = event.get("count")
    if kind not in ALLOWED_KINDS:
        return None
    if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 10000:
        return None
    level = KIND_POLICY[kind]["level"]
    if level >= 12:
        severity = "critical"
    elif level >= 8:
        severity = "high"
    elif level >= 5:
        severity = "warning"
    else:
        severity = "info"
    effective = replay_features.get("effective")
    if effective is not None:
        expected = {
            "kind": kind, "severity": severity, "level": level, "count": count,
        }
        if not isinstance(effective, dict) or effective != expected:
            return None
    return {"kind": kind, "severity": severity, "level": level, "count": count}


def _verified_receipt_feature(row):
    replay_features = row.get("replay_features")
    if (not isinstance(replay_features, dict)
            or replay_features.get("feature_schema") != REPLAY_SCHEMA):
        return None
    event = replay_features.get("event")
    payload_digest = row.get("payload_digest")
    if not isinstance(event, dict) or not isinstance(payload_digest, str):
        raise MojoSecLearningError("receipt replay evidence is incomplete")
    actual_digest = _digest(event)
    if not secrets.compare_digest(actual_digest, payload_digest):
        raise MojoSecLearningError(
            f"receipt {row.get('id')} replay evidence failed its canonical payload digest")
    return _feature_projection(replay_features)


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
        feature = _verified_receipt_feature(row)
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
    provenance = _evaluator_provenance()
    metrics = dict(totals)
    metrics["by_kind"] = {key: kinds[key] for key in sorted(kinds)}
    metrics["evaluator"] = provenance
    content_digest = _digest(policy)
    return {
        "sample_count": len(sample),
        "sample_digest": _digest({"evaluator": provenance, "sample": sample}),
        "result_digest": _digest({
            "evaluator": provenance, "content_digest": content_digest,
            "decisions": decisions,
        }),
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
    if not isinstance(receipt_ids, list) or not receipt_ids or len(receipt_ids) > limit:
        raise MojoSecLearningError("receipt_ids must be an explicit non-empty bounded list")
    normalized_ids = [
        _bounded_int(value, "receipt_ids entry", 1, 2 ** 63 - 1)
        for value in receipt_ids
    ]
    if len(set(normalized_ids)) != len(normalized_ids):
        raise MojoSecLearningError("receipt_ids must not contain duplicates")
    normalized_ids.sort()
    with transaction.atomic():
        proposal = MojoSecPolicyProposal.objects.select_for_update().get(pk=proposal_id)
        proposal.assert_integrity()
        if MojoSecPolicyProposal.objects.filter(supersedes=proposal).exists():
            raise MojoSecLearningError("only an unsuperseded proposal leaf may be evaluated")
        content = validate_policy_content(proposal.content)
        if proposal.content_digest != _digest(content):
            raise MojoSecLearningError("proposal content digest does not match")
        if proposal.status == MojoSecPolicyProposal.REJECTED:
            raise MojoSecLearningError("a rejected proposal leaf closes its lineage")
        if mode == MojoSecPolicyEvaluation.SHADOW and proposal.status != MojoSecPolicyProposal.SHADOW:
            raise MojoSecLearningError("shadow evaluation requires a shadow proposal revision")
        rows_by_id = {
            row["id"]: row
            for row in MojoSecReceipt.objects.filter(
                publish_state=MojoSecReceipt.PUBLISH_PUBLISHED,
                pk__in=normalized_ids,
            ).values("id", "payload_digest", "replay_features")
        }
        if set(rows_by_id) != set(normalized_ids):
            raise MojoSecLearningError(
                "every receipt_id must identify retained published evidence")
        rows = [rows_by_id[value] for value in normalized_ids]
        result = evaluate_features(content, rows)
        if result["sample_count"] != len(rows):
            raise MojoSecLearningError("every receipt_id must contain replay_features_v1")
        return MojoSecPolicyEvaluation.objects.create(
            proposal=proposal, created_by=author, mode=mode,
            sample_count=result["sample_count"], sample_digest=result["sample_digest"],
            result_digest=result["result_digest"], metrics=result["metrics"])


def _stratified_sample(rows, limit, per_installation, identity):
    selected = []
    counts = {}
    for row in rows:
        key = identity(row)
        if counts.get(key, 0) >= per_installation:
            continue
        selected.append(row)
        counts[key] = counts.get(key, 0) + 1
        if len(selected) >= limit:
            break
    return selected


def detector_metrics(author, days=30, limit=1000):
    from mojo.apps.incident.models import (
        MojoSecDetectorFeedbackHead, MojoSecReceipt)

    _assert_human_security_author(author, permission="view_security")
    days = _bounded_int(days, "days", 1, 365)
    limit = _bounded_int(limit, "limit", 1, MAX_METRIC_ROWS)
    cutoff = dates.utcnow() - dates.timedelta(days=days)
    per_installation = min(
        MAX_METRIC_ROWS_PER_INSTALLATION, max(1, limit // 10))
    candidate_limit = min(MAX_METRIC_CANDIDATES, max(limit, limit * 4))
    receipt_candidates = list(MojoSecReceipt.objects.filter(
        created__gte=cutoff, publish_state=MojoSecReceipt.PUBLISH_PUBLISHED,
    ).order_by("-created", "-id").values(
        "id", "api_key_id", "sensor_id", "payload_digest",
        "replay_features")[:candidate_limit])
    head_candidates = list(MojoSecDetectorFeedbackHead.objects.filter(
        modified__gte=cutoff, current__isnull=False,
    ).select_related("current").order_by("-modified", "-id")[:candidate_limit])
    for head in head_candidates:
        head.current.assert_integrity()
        if head.current.subject_key != head.subject_key:
            raise MojoSecLearningError("feedback head and current revision subjects differ")
    receipts = _stratified_sample(
        receipt_candidates, limit, per_installation,
        lambda row: (row["api_key_id"], row["sensor_id"]))
    feedback = _stratified_sample(
        [head.current for head in head_candidates], limit, per_installation,
        lambda row: (row.installation_key_id_snapshot, row.sensor_id))
    kinds = {}
    installations = {}
    for receipt in receipts:
        feature = _verified_receipt_feature(receipt)
        if feature is None:
            continue
        row = kinds.setdefault(feature["kind"], {"receipts": 0, "occurrences": 0, "feedback": {}})
        row["receipts"] += 1
        row["occurrences"] += feature["count"]
        installation = f"{receipt['api_key_id']}:{receipt['sensor_id']}"
        install_row = installations.setdefault(
            installation, {"installation_key_id": receipt["api_key_id"],
                           "sensor_id": receipt["sensor_id"], "receipts": 0,
                           "feedback": 0})
        install_row["receipts"] += 1
    for item in feedback:
        item.assert_integrity()
        row = kinds.setdefault(item.detector_kind, {"receipts": 0, "occurrences": 0, "feedback": {}})
        disposition = item.disposition
        row["feedback"][disposition] = row["feedback"].get(disposition, 0) + 1
        installation = f"{item.installation_key_id_snapshot or 'manual'}:{item.sensor_id}"
        install_row = installations.setdefault(
            installation, {"installation_key_id": item.installation_key_id_snapshot,
                           "sensor_id": item.sensor_id, "receipts": 0,
                           "feedback": 0})
        install_row["feedback"] += 1
    return {
        "window_days": days,
        "receipt_sample_limit": limit,
        "feedback_sample_limit": limit,
        "candidate_scan_limit": candidate_limit,
        "per_installation_sample_limit": per_installation,
        "receipt_rows_scanned": len(receipt_candidates),
        "feedback_rows_scanned": len(head_candidates),
        "receipt_rows_sampled": len(receipts),
        "feedback_rows_sampled": len(feedback),
        "detectors": {key: kinds[key] for key in sorted(kinds)},
        "installations": [installations[key] for key in sorted(installations)],
    }


def prune_learning_evaluations(job=None, now=None):
    from mojo.apps.incident.models.mojosec_policy_evaluation import (
        _prune_policy_evaluations_before)

    retention_days = settings.get_static(
        "MOJOSEC_LEARNING_EVALUATION_RETENTION_DAYS", 90, kind="int")
    retention_days = max(30, min(3650, retention_days))
    cutoff = (now or dates.utcnow()) - dates.timedelta(days=retention_days)
    deleted, _ = _prune_policy_evaluations_before(cutoff)
    if job is not None and hasattr(job, "add_log"):
        job.add_log(f"Pruned {deleted} expired MojoSec offline evaluation rows")
    return deleted
