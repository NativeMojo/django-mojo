"""Typed policy facts derived exclusively from durable MojoSec receipts.

Event metadata is presentation evidence, never an authority for these fields.
Imports of the ingestion service and models stay lazy because the public rule
schema imports this module during application setup.
"""

import datetime
import re


FIELD_SCHEMAS = {
    "mojosec_kind": "str",
    "mojosec_user": "str",
    "mojosec_auth_method": "str",
    "mojosec_path": "str",
    "mojosec_status": "int",
    "mojosec_response_class": "str",
    "mojosec_command_path": "str",
    "mojosec_proof_status": "str",
    "mojosec_expected_change": "bool",
    "mojosec_routine": "bool",
    "mojosec_scanner_confirmed": "bool",
}

WINDOW_SECONDS = 600
MAX_RECENT_RECEIPTS = 256
_CACHE_ATTRIBUTE = "_mojosec_rule_facts_cache"
_DIGEST = re.compile(r"^[a-f0-9]{64}$")


def _validated_receipt(receipt, event):
    from mojo.apps.incident.services import mojosec, mojosec_evidence
    from mojo.mojosec import protocol

    replay = receipt.replay_features
    if (not isinstance(replay, dict) or
            replay.get("feature_schema") != "replay_features_v1" or
            replay.get("schema") != protocol.BATCH_SCHEMA or
            type(replay.get("version")) is not int or
            replay["version"] != protocol.PROTOCOL_VERSION or
            replay["version"] != receipt.protocol_version or
            not isinstance(receipt.sensor_id, str) or not receipt.sensor_id or
            replay.get("sensor_id") != receipt.sensor_id or
            replay.get("policy_revision") != receipt.sensor_policy_revision):
        return None
    wire = replay.get("event")
    try:
        protocol.validate_event(wire)
        if (protocol.has_unstorable_text(wire) or
                wire["id"] != receipt.wire_event_id or
                mojosec._payload_digest(wire) != receipt.payload_digest):
            return None
        projection = mojosec_evidence.project(
            wire["kind"], wire["attributes"], wire["count"], wire["last_seen"])
    except (TypeError, ValueError, OverflowError):
        return None
    if (receipt.event_id != event.pk or event.scope != "mojosec" or
            event.category != mojosec._category_for_kind(wire["kind"]) or
            event.source_ip != projection["source_ip"] or
            (event.model_id is not None and event.model_id != receipt.api_key_id)):
        return None
    return {"receipt": receipt, "wire": wire, "projection": projection}


def _load(event):
    from mojo.apps.incident.models import MojoSecReceipt
    from mojo.helpers import dates

    if not getattr(event, "pk", None) or getattr(event, "scope", None) != "mojosec":
        return None
    receipts = list(MojoSecReceipt.objects.filter(event_id=event.pk).only(
        "id", "created", "api_key_id", "event_id", "sensor_id", "wire_event_id",
        "payload_digest", "protocol_version", "sensor_policy_revision", "replay_features",
    ).order_by("pk")[:2])
    if len(receipts) != 1:
        return None
    context = _validated_receipt(receipts[0], event)
    if context is None:
        return None
    evidence = context["projection"]["evidence"]
    attributes = context["wire"]["attributes"]
    values = {"mojosec_kind": context["wire"]["kind"]}
    for name in ("auth_method", "path", "status", "response_class",
                 "command_path", "proof_status"):
        if name in evidence:
            values["mojosec_" + name] = evidence[name]
    # The display projector deliberately tolerates some scalar coercions.
    # Authentication identity must instead start as a literal sensor string.
    for name in ("user", "actor", "target_user"):
        if isinstance(attributes.get(name), str) and name in evidence:
            values["mojosec_user"] = evidence[name]
            break
    context.update({"values": values, "now": dates.utcnow()})
    return context


def _recent(context, categories):
    from django.db.models import Exists, OuterRef
    from mojo.apps.incident.models import MojoSecReceipt

    source_ip = context["projection"]["source_ip"]
    if not source_ip:
        return []
    other_receipt = MojoSecReceipt.objects.filter(
        event_id=OuterRef("event_id")).exclude(pk=OuterRef("pk"))
    candidates = MojoSecReceipt.objects.filter(
        event__source_ip=source_ip, event__category__in=categories,
        created__gte=context["now"] - datetime.timedelta(seconds=WINDOW_SECONDS),
        created__lte=context["now"],
    ).select_related("event").annotate(
        ambiguous_event=Exists(other_receipt),
    ).only(
        "id", "created", "api_key_id", "event_id", "sensor_id", "wire_event_id",
        "payload_digest", "protocol_version", "sensor_policy_revision", "replay_features",
        "event__id", "event__scope", "event__category", "event__source_ip", "event__model_id",
    ).order_by("-created", "-pk")[:MAX_RECENT_RECEIPTS]
    valid = []
    for receipt in candidates:
        if receipt.ambiguous_event:
            continue
        candidate = _validated_receipt(receipt, receipt.event)
        if candidate is not None:
            valid.append(candidate)
    return valid


def _expected_change(context):
    from mojo.apps.incident.services import mojosec, mojosec_correlation

    wire = context["wire"]
    annotation = mojosec._expected_change_projection(
        wire["kind"], wire["attributes"], wire["last_seen"])
    if annotation is None:
        return False
    return mojosec_correlation._deployment_registered(
        context["receipt"].api_key_id, annotation["deployment_id"], context["now"])


def _routine(context):
    from mojo.mojosec.disposition import classify_local_only, JOBMAN_FIREWALL_CLASSIFIER

    wire = context["wire"]
    kind = wire["kind"]
    attributes = wire["attributes"]
    evidence = context["projection"]["evidence"]
    if kind == "auth.ssh_login":
        if (not context["projection"]["source_ip"] or
                not isinstance(attributes.get("user"), str) or
                not evidence.get("user") or evidence.get("auth_method") != "PUBLICKEY"):
            return False
        # A valid credential immediately following failures is precisely the
        # success that must remain visible, including a different target user.
        return not _recent(context, ("mojosec.auth.ssh_failure",))
    if kind == "fim.change":
        return _value(context, "mojosec_expected_change")
    classifier = classify_local_only(wire, wire=True)
    if classifier == JOBMAN_FIREWALL_CLASSIFIER:
        # This classifier's envelope is shared with the sensor. The explicit
        # provenance requirements prevent a marker or executable name alone
        # from granting generic command suppression.
        return bool(
            attributes.get("proof_status") == "proven" and
            attributes.get("origin_kind") == "cron_jobman" and
            isinstance(attributes.get("lineage_sha256"), str) and
            _DIGEST.fullmatch(attributes["lineage_sha256"]) and
            attributes.get("command_path") == "/usr/local/sbin/mojo-firewall-broker" and
            not any(name in attributes for name in (
                "command_truncated", "command_path_truncated", "cwd_truncated")))
    return bool(classifier)


def _sensitive_path(path):
    if not isinstance(path, str) or not path.startswith("/"):
        return False
    pieces = path.lower().strip("/").split("/")
    if any(piece == ".env" or piece.startswith(".env.") for piece in pieces):
        return True
    if ".git" in pieces:
        index = pieces.index(".git")
        return index + 1 < len(pieces) and pieces[index + 1] in (
            "config", "head", "index", "packed-refs", "refs", "objects", "logs")
    return bool(len(pieces) >= 3 and pieces[:2] == ["vendor", "phpunit"] and
                pieces[-1] == "eval-stdin.php")


def _scanner_candidate(context):
    evidence = context["projection"]["evidence"]
    status = evidence.get("status")
    return bool(
        context["wire"]["kind"] == "web.probe" and
        type(status) is int and 300 <= status < 500 and
        context["projection"]["source_ip"] and _sensitive_path(evidence.get("path")))


def _scanner_confirmed(context):
    if not _scanner_candidate(context):
        return False
    paths = set()
    receipts = set()
    for candidate in _recent(context, ("mojosec.web.probe",)):
        if _scanner_candidate(candidate):
            paths.add(candidate["projection"]["evidence"]["path"].lower())
            receipts.add(candidate["receipt"].pk)
            if len(paths) >= 3 and len(receipts) >= 3:
                return True
    return False


def _value(context, name):
    values = context["values"]
    if name not in values:
        if name == "mojosec_expected_change":
            values[name] = _expected_change(context)
        elif name == "mojosec_routine":
            values[name] = _routine(context)
        elif name == "mojosec_scanner_confirmed":
            values[name] = _scanner_confirmed(context)
    return values.get(name)


def field_value(event, name):
    """Resolve a reserved field, returning None when durable proof is absent.

    Cached validation is private to this Event instance and invalidates if its
    authoritative columns change. Expensive context is computed lazily once.
    """
    if name not in FIELD_SCHEMAS:
        return None
    from django.db import Error

    signature = tuple(getattr(event, field, None) for field in (
        "pk", "scope", "category", "source_ip", "model_id"))
    cached = getattr(event, _CACHE_ATTRIBUTE, None)
    try:
        if not isinstance(cached, dict) or cached.get("signature") != signature:
            cached = {"signature": signature, "context": _load(event)}
            setattr(event, _CACHE_ATTRIBUTE, cached)
        context = cached["context"]
        return _value(context, name) if context is not None else None
    except (Error, TypeError, ValueError, OverflowError, AttributeError):
        # Unavailable or malformed evidence must never fall back to arbitrary
        # metadata, nor buy a routine exemption or an automatic block. Retain
        # the failed lookup too, so an outage cannot repeat work per rule.
        setattr(event, _CACHE_ATTRIBUTE, {"signature": signature, "context": None})
        return None
