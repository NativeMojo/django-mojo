"""Normalization and lifecycle for GuardDuty findings delivered over SNS.

GuardDuty reaches django-mojo as ``GuardDuty -> EventBridge -> SNS -> POST``,
so the SNS ``Message`` is always an EventBridge envelope wrapping the finding
in ``detail``. The receiver is the sibling of ``cloudwatch_alarms`` and follows
the same posture: bounded persisted metadata, a durable per-finding row, exact
dedupe, and out-of-band handler dispatch with a per-occurrence idempotency key.

Severity policy (approved, deliberate): NOTHING here opens an incident on its
own. Every band maps below ``INCIDENT_LEVEL_THRESHOLD`` (7), so enabling the
receiver only records events until an operator installs an explicit RuleSet —
exactly the choice already made for CloudWatch alarms. Medium and above still
map to level 6 so ``prune_events`` (which deletes ``level__lt=6``) keeps them
for the full retention window.
"""

import hashlib
import json
import math
import re

from django.db import IntegrityError, transaction
from django.utils.dateparse import parse_datetime

from mojo.apps.aws.models import GuardDutyFinding
from mojo.apps.incident.reporter import record_event
from mojo.helpers import logit


logger = logit.get_logger(__name__, "aws.log")

SCOPE = "aws:guardduty"
CATEGORY_PREFIX = "aws:guardduty:"
MODEL_NAME = "aws.guardduty.finding"
EVENTBRIDGE_SOURCE = "aws.guardduty"
EVENTBRIDGE_DETAIL_TYPE = "GuardDuty Finding"

# An incident in one of these states is no longer the finding's live
# occurrence; the next delivery must open a fresh one.
TERMINAL_INCIDENT_STATUS = ("resolved", "closed", "ignored")

_ID_RE = re.compile(r"^[A-Za-z0-9]+$")
# "aws:guardduty:" (14) + a 100-char type = 114, inside Event.category's 124.
_TYPE_RE = re.compile(r"^[A-Za-z0-9:/._-]+$")

# The ONLY leaf names copied out of a finding's ``resource`` block. The raw
# dict is never persisted: it is unbounded provider-shaped data, and
# record_event's sanitize_dict is not GuardDuty-aware, so this allowlist IS
# the control.
IDENTIFIER_NAMES = frozenset({
    "instanceId", "accessKeyId", "userName", "userType", "principalId",
    "name", "functionName", "dbInstanceIdentifier", "username", "arn",
})
MAX_IDENTIFIERS = 10
MAX_IDENTIFIER_LENGTH = 256
MAX_IDENTIFIER_DEPTH = 3


class GuardDutyPayloadError(ValueError):
    pass


class GuardDutyDispatchError(RuntimeError):
    pass


def _text(payload, name, limit, required=True):
    value = payload.get(name)
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value) or len(value) > limit:
        raise GuardDutyPayloadError(f"invalid {name}")
    return value


def severity_to_level(severity):
    """Map a GuardDuty severity score to an incident Event level.

    Critical (>=9), High (>=7) and Medium (>=4) all map to 6 — the floor that
    survives ``prune_events`` — and NOT to 7+, because level 7 is the
    auto-incident threshold. A High finding must not open an incident, a
    triage job, or an IP threat stamp on its own; a RuleSet is how an
    operator opts into that.

    ``bool`` is excluded explicitly: ``isinstance(True, int)`` is True in
    Python, and ``True`` is not a severity.
    """
    if isinstance(severity, bool) or not isinstance(severity, (int, float)):
        raise GuardDutyPayloadError("invalid finding severity")
    value = float(severity)
    if not math.isfinite(value) or value < 0.0 or value > 10.0:
        raise GuardDutyPayloadError("invalid finding severity")
    if value >= 4.0:
        return 6
    if value >= 1.0:
        return 4
    return 2


def severity_label(severity):
    """The AWS console label for a severity score, for titles and metadata."""
    value = float(severity)
    if value >= 9.0:
        return "Critical"
    if value >= 7.0:
        return "High"
    if value >= 4.0:
        return "Medium"
    if value >= 1.0:
        return "Low"
    return "Informational"


def _finding_key(account, region, detector_id, finding_id):
    raw = f"{account}:{region}:{detector_id}:{finding_id}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _identifiers(resource):
    """Bounded projection of a finding's resource block.

    Walks at most ``MAX_IDENTIFIER_DEPTH`` levels, keeps only allowlisted leaf
    names, at most ``MAX_IDENTIFIERS`` entries, each truncated. Anything else
    in the resource — tags, arbitrary blobs, unexpected credential-shaped keys
    — is dropped rather than persisted.
    """
    found = []
    seen = set()

    def walk(node, depth):
        if len(found) >= MAX_IDENTIFIERS or depth > MAX_IDENTIFIER_DEPTH:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                if len(found) >= MAX_IDENTIFIERS:
                    return
                if key in IDENTIFIER_NAMES and not isinstance(value, bool) \
                        and isinstance(value, (str, int, float)):
                    text = str(value)[:MAX_IDENTIFIER_LENGTH]
                    if (key, text) in seen:
                        continue
                    seen.add((key, text))
                    found.append({"name": key, "value": text})
                elif isinstance(value, (dict, list)):
                    walk(value, depth + 1)
        elif isinstance(node, list):
            for item in node:
                if len(found) >= MAX_IDENTIFIERS:
                    return
                if isinstance(item, (dict, list)):
                    walk(item, depth + 1)

    if isinstance(resource, dict):
        walk(resource, 1)
    return found


def _scalar(value, limit):
    if isinstance(value, str):
        return value[:limit]
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return value
    return None


def _ip(details):
    if not isinstance(details, dict):
        return None
    value = details.get("ipAddressV4") or details.get("ipAddressV6")
    if isinstance(value, str) and 0 < len(value) <= 45:
        return value
    return None


def _port(details):
    if not isinstance(details, dict):
        return None
    port = details.get("port")
    if isinstance(port, bool) or not isinstance(port, int):
        return None
    if port < 0 or port > 65535:
        return None
    return port


def _action_summary(action):
    """Return ``(summary, remote_ip, remote_ip_is_origin)`` for a finding action.

    Only fixed, per-type key sets are copied — never the raw action.

    ``remote_ip_is_origin`` decides whether the address may become the Event's
    ``source_ip``. For an OUTBOUND network connection the remote address is a
    destination OUR host chose to contact, not an actor: feeding it to inbound
    threat scoring would stamp a C2 destination (or an innocent third party)
    with a threat level and could route it into a firewall block.
    """
    summary = {}
    remote_ip = None
    is_origin = False
    if not isinstance(action, dict):
        return summary, remote_ip, is_origin

    network = action.get("networkConnectionAction")
    api_call = action.get("awsApiCallAction")
    dns = action.get("dnsRequestAction")
    probe = action.get("portProbeAction")
    kube = action.get("kubernetesApiCallAction")

    if isinstance(network, dict):
        direction = _scalar(network.get("connectionDirection"), 32)
        summary["action_type"] = "networkConnection"
        summary["connection_direction"] = direction
        summary["protocol"] = _scalar(network.get("protocol"), 32)
        summary["blocked"] = bool(network.get("blocked"))
        summary["local_port"] = _port(network.get("localPortDetails"))
        summary["remote_port"] = _port(network.get("remotePortDetails"))
        remote_ip = _ip(network.get("remoteIpDetails"))
        is_origin = bool(remote_ip) and str(direction or "").upper() == "INBOUND"
    elif isinstance(api_call, dict):
        summary["action_type"] = "awsApiCall"
        summary["api"] = _scalar(api_call.get("api"), 128)
        summary["service_name"] = _scalar(api_call.get("serviceName"), 128)
        summary["caller_type"] = _scalar(api_call.get("callerType"), 64)
        summary["error_code"] = _scalar(api_call.get("errorCode"), 128)
        remote_ip = _ip(api_call.get("remoteIpDetails"))
        is_origin = bool(remote_ip)
    elif isinstance(dns, dict):
        summary["action_type"] = "dnsRequest"
        summary["domain"] = _scalar(dns.get("domain"), 253)
        summary["protocol"] = _scalar(dns.get("protocol"), 32)
        summary["blocked"] = bool(dns.get("blocked"))
    elif isinstance(probe, dict):
        summary["action_type"] = "portProbe"
        summary["blocked"] = bool(probe.get("blocked"))
        rows = probe.get("portProbeDetails")
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            remote_ip = _ip(rows[0].get("remoteIpDetails"))
        is_origin = bool(remote_ip)
    elif isinstance(kube, dict):
        summary["action_type"] = "kubernetesApiCall"
        summary["verb"] = _scalar(kube.get("verb"), 32)
        summary["request_uri"] = _scalar(kube.get("requestUri"), 256)
        remote_ip = _ip(kube.get("remoteIpDetails"))
        is_origin = bool(remote_ip)

    return {k: v for k, v in summary.items() if v is not None}, remote_ip, is_origin


def normalize(message):
    """Validate the EventBridge envelope wrapping one GuardDuty finding.

    An UNKNOWN finding type is accepted on purpose: the type is data, not an
    enum, and a new AWS detector family must never make the receiver return
    400. Only charset and length bound it.
    """
    if not isinstance(message, dict):
        raise GuardDutyPayloadError("invalid GuardDuty message")
    # EventBridge is the only route GuardDuty can take to SNS, so the wrapper
    # is always present — a cheap provenance check on a public endpoint.
    if message.get("source") != EVENTBRIDGE_SOURCE:
        raise GuardDutyPayloadError("invalid EventBridge source")
    if message.get("detail-type") != EVENTBRIDGE_DETAIL_TYPE:
        raise GuardDutyPayloadError("invalid EventBridge detail-type")
    detail = message.get("detail")
    if not isinstance(detail, dict):
        raise GuardDutyPayloadError("invalid GuardDuty detail")

    finding_id = _text(detail, "id", 128)
    if not _ID_RE.match(finding_id):
        raise GuardDutyPayloadError("invalid finding id")
    finding_type = _text(detail, "type", 100)
    if not _TYPE_RE.match(finding_type):
        raise GuardDutyPayloadError("invalid finding type")

    severity = detail.get("severity")
    level = severity_to_level(severity)

    title = _text(detail, "title", 512)
    description = _text(detail, "description", 4096, required=False)

    account = _text(detail, "accountId", 12)
    if not account.isdigit() or len(account) != 12:
        raise GuardDutyPayloadError("invalid accountId")
    region = _text(detail, "region", 64)

    service = detail.get("service")
    if service is not None and not isinstance(service, dict):
        raise GuardDutyPayloadError("invalid service block")
    service = service or {}
    detector_id = _text(service, "detectorId", 64, required=False)
    if detector_id and not _ID_RE.match(detector_id):
        raise GuardDutyPayloadError("invalid detectorId")
    count = service.get("count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        count = None

    updated_text = _text(detail, "updatedAt", 64)
    updated_at = parse_datetime(updated_text)
    if updated_at is None or updated_at.tzinfo is None:
        raise GuardDutyPayloadError("invalid updatedAt")
    created_text = _text(detail, "createdAt", 64, required=False)
    if created_text:
        created_at = parse_datetime(created_text)
        if created_at is None or created_at.tzinfo is None:
            raise GuardDutyPayloadError("invalid createdAt")

    resource = detail.get("resource")
    resource_type = ""
    if isinstance(resource, dict):
        resource_type = _text(resource, "resourceType", 64, required=False)
    action_summary, remote_ip, remote_ip_is_origin = _action_summary(
        service.get("action"),
    )

    return {
        "finding_id": finding_id,
        "finding_type": finding_type,
        "severity": float(severity),
        "severity_label": severity_label(severity),
        "level": level,
        "title": title,
        "description": description,
        "account": account,
        "region": region,
        "detector_id": detector_id,
        "guardduty_count": count,
        "updated_at": updated_at,
        "updated_at_text": updated_text,
        "created_at_text": created_text,
        "resource_type": resource_type,
        "identifiers": _identifiers(resource),
        "action": action_summary,
        "remote_ip": remote_ip,
        "remote_ip_is_origin": remote_ip_is_origin,
    }


def _locked_finding(data):
    key = _finding_key(
        data["account"], data["region"], data["detector_id"], data["finding_id"],
    )
    finding = GuardDutyFinding.objects.select_for_update().filter(
        finding_key=key,
    ).first()
    if finding is None:
        try:
            with transaction.atomic():
                finding = GuardDutyFinding.objects.create(
                    finding_key=key,
                    finding_id=data["finding_id"],
                    detector_id=data["detector_id"],
                    account=data["account"],
                    region=data["region"],
                )
        except IntegrityError:
            finding = GuardDutyFinding.objects.select_for_update().get(
                finding_key=key,
            )
    # Re-assert identity after the lock: a hash collision, or a row created by
    # a racing worker, must never be treated as this finding.
    if finding.finding_id != data["finding_id"]:
        raise GuardDutyPayloadError("finding identity collision")
    return finding


def _resolve_active_incident(finding):
    """Return the finding's live incident, clearing the occurrence when terminal.

    Clearing ``opening_event`` is what rotates ``Event.model_id`` for the next
    occurrence. determine_bundle_criteria has NO status filter and (with
    bundle_minutes=None) no time filter, so without rotation a finding that
    recurs after its incident was resolved would bundle straight back into the
    resolved incident.
    """
    if finding.active_incident_id is None:
        # Either none was ever opened, or SET_NULL cleared a pruned incident.
        # The occurrence handle must not outlive the incident it named.
        finding.opening_event = None
        return None
    from mojo.apps.incident.models import Incident
    incident = Incident.objects.select_for_update().filter(
        pk=finding.active_incident_id,
    ).first()
    if incident is None or incident.status in TERMINAL_INCIDENT_STATUS:
        finding.active_incident = None
        finding.opening_event = None
        return None
    return incident


def _record_finding_event(finding, data):
    metadata = {
        "finding_id": data["finding_id"],
        "finding_type": data["finding_type"],
        "aws_account_id": data["account"],
        # NOT "region": Event.sync_metadata writes a geo-lookup "region" when
        # the event carries a source_ip, which would silently overwrite the
        # AWS region with a city/state name.
        "aws_region": data["region"],
        "detector_id": data["detector_id"],
        "severity": data["severity"],
        "severity_label": data["severity_label"],
        "guardduty_count": data["guardduty_count"],
        "updated_at": data["updated_at_text"],
        "created_at": data["created_at_text"],
        "resource_type": data["resource_type"],
        "identifiers": data["identifiers"],
        "action": data["action"],
        "occurrence": finding.occurrence_count + 1,
    }
    if data["remote_ip"]:
        metadata["remote_ip"] = data["remote_ip"]
        metadata["remote_ip_is_origin"] = data["remote_ip_is_origin"]

    event = record_event(
        data["description"] or data["title"],
        title=f"GuardDuty {data['severity_label']}: {data['title']}",
        category=CATEGORY_PREFIX + data["finding_type"],
        scope=SCOPE,
        level=data["level"],
        group=None,
        model_name=MODEL_NAME,
        model_id=finding.opening_event_id,
        source_ip=data["remote_ip"] if data["remote_ip_is_origin"] else None,
        **metadata,
    )
    if event.model_id is None:
        # This event opens the occurrence, so it names it. Every later event
        # of the same occurrence reuses this id (see _resolve_active_incident).
        event.model_id = event.pk
        event.metadata["model_id"] = event.pk
        event.save(update_fields=["model_id", "metadata"])
    return event


def _dispatch(finding_pk):
    finding = GuardDutyFinding.objects.select_related(
        "pending_event", "active_incident__rule_set",
    ).get(pk=finding_pk)
    if finding.dispatch_status != GuardDutyFinding.DISPATCH_PENDING:
        return
    if (
        not finding.pending_event_id
        or not finding.active_incident_id
        or not finding.active_incident.rule_set_id
    ):
        finding.dispatch_status = GuardDutyFinding.DISPATCH_COMPLETE
        finding.save(update_fields=["dispatch_status", "modified"])
        return
    try:
        finding.active_incident.rule_set.run_handler(
            finding.pending_event,
            finding.active_incident,
            # PER-OCCURRENCE, not per-finding: Job.idempotency_key is globally
            # unique and jobs.publish returns the pre-existing job on
            # collision. A finding-pk-only prefix would make occurrence #2..#N
            # publish nothing at all, silently.
            idempotency_prefix=f"aws-gd:{finding.pk}:{finding.pending_event_id}",
            strict=True,
        )
    except Exception as exc:
        raise GuardDutyDispatchError("GuardDuty handler dispatch failed") from exc
    finding.dispatch_status = GuardDutyFinding.DISPATCH_COMPLETE
    finding.save(update_fields=["dispatch_status", "modified"])


def _resume_stranded_dispatch(finding_key):
    """Retry a dispatch stranded by an earlier crash, before the new occurrence.

    Bounded on purpose: a failure here is logged and swallowed so the caller
    still records the new occurrence. Left unbounded, one permanently failing
    dispatch would drop every later delivery for that finding at the door.
    """
    finding = GuardDutyFinding.objects.filter(finding_key=finding_key).first()
    if finding is None or finding.dispatch_status != GuardDutyFinding.DISPATCH_PENDING:
        return
    try:
        _dispatch(finding.pk)
    except Exception:
        logger.exception(
            "GuardDuty stranded dispatch resume failed (finding=%s)", finding.pk,
        )


def process_notification(envelope):
    topic_arn = envelope.get("TopicArn")
    message_id = envelope.get("MessageId")
    if (
        not isinstance(topic_arn, str)
        or not isinstance(message_id, str)
        or len(message_id) > 100
    ):
        raise GuardDutyPayloadError("invalid SNS notification identity")
    try:
        message = json.loads(envelope.get("Message", ""))
    except Exception:
        raise GuardDutyPayloadError("invalid GuardDuty message")
    data = normalize(message)

    _resume_stranded_dispatch(_finding_key(
        data["account"], data["region"], data["detector_id"], data["finding_id"],
    ))

    duplicate = False
    with transaction.atomic():
        finding = _locked_finding(data)
        if (
            finding.last_updated_at is not None
            and data["updated_at"] <= finding.last_updated_at
        ):
            # A replay, or an out-of-order delivery. No Event, no state change.
            duplicate = True
        else:
            incident = _resolve_active_incident(finding)
            event = _record_finding_event(finding, data)
            finding.occurrence_count += 1
            if incident is None:
                result = event.publish(
                    use_catchall=False, dispatch_handlers=False,
                )
                incident = result["incident"]
                finding.active_incident = incident
                if incident is not None:
                    finding.opening_event = event
                if result["should_dispatch"]:
                    finding.pending_event = event
                    finding.dispatch_status = GuardDutyFinding.DISPATCH_PENDING
            else:
                event.link_to_incident(incident)
                if data["level"] > incident.priority:
                    old_priority = incident.priority
                    incident.priority = data["level"]
                    incident.save(update_fields=["priority"])
                    incident.add_history(
                        "priority_escalated",
                        note=(
                            f"Priority escalated from {old_priority} to "
                            f"{data['level']} by GuardDuty severity "
                            f"{data['severity']}"
                        ),
                    )
            finding.finding_type = data["finding_type"]
            finding.severity = data["severity"]
            finding.level = data["level"]
            finding.last_updated_at = data["updated_at"]
            finding.save(update_fields=[
                "finding_type", "severity", "level", "last_updated_at",
                "occurrence_count", "active_incident", "opening_event",
                "pending_event", "dispatch_status", "modified",
            ])

    _dispatch(finding.pk)
    return {
        "duplicate": duplicate,
        "finding": finding.pk,
        "level": finding.level,
        "occurrence_count": finding.occurrence_count,
    }
