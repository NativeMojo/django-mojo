import hashlib
import json

from django.db import IntegrityError, transaction
from django.utils.dateparse import parse_datetime

from mojo.apps.aws.models import CloudWatchAlarm, CloudWatchAlarmTransition
from mojo.apps.incident.reporter import record_event
from mojo.apps.incident.services.lifecycle import resolve_incident
from mojo.helpers import logit


logger = logit.get_logger(__name__, "aws.log")

CATEGORY = "aws:cloudwatch:alarm"
SCOPE = "aws:cloudwatch"
MODEL_NAME = "aws.cloudwatch.alarm"
STATES = {"ALARM", "OK", "INSUFFICIENT_DATA"}


class CloudWatchPayloadError(ValueError):
    pass


class CloudWatchDispatchError(RuntimeError):
    pass


def _text(payload, name, limit, required=True):
    value = payload.get(name)
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value) or len(value) > limit:
        raise CloudWatchPayloadError(f"invalid {name}")
    return value


def _dimensions(trigger):
    raw = trigger.get("Dimensions") or []
    if not isinstance(raw, list) or len(raw) > 30:
        raise CloudWatchPayloadError("invalid metric dimensions")
    result = []
    for row in raw:
        if not isinstance(row, dict):
            raise CloudWatchPayloadError("invalid metric dimensions")
        name = row.get("name", row.get("Name"))
        value = row.get("value", row.get("Value"))
        if (
            not isinstance(name, str) or not name or len(name) > 255
            or not isinstance(value, str) or len(value) > 1024
        ):
            raise CloudWatchPayloadError("invalid metric dimensions")
        result.append({"name": name, "value": value})
    return sorted(result, key=lambda row: (row["name"], row["value"]))


def normalize(payload):
    if not isinstance(payload, dict):
        raise CloudWatchPayloadError("invalid CloudWatch payload")
    alarm_name = _text(payload, "AlarmName", 255)
    alarm_arn = _text(payload, "AlarmArn", 1600)
    arn_parts = alarm_arn.split(":", 5)
    if len(arn_parts) != 6 or arn_parts[0] != "arn" or arn_parts[2] != "cloudwatch":
        raise CloudWatchPayloadError("invalid AlarmArn")
    account = _text(payload, "AWSAccountId", 12)
    if not account.isdigit() or len(account) != 12 or arn_parts[4] != account:
        raise CloudWatchPayloadError("invalid AWSAccountId")
    region = arn_parts[3]
    if not region or len(region) > 64:
        raise CloudWatchPayloadError("invalid alarm region")

    old_state = _text(payload, "OldStateValue", 32)
    new_state = _text(payload, "NewStateValue", 32)
    if old_state not in STATES or new_state not in STATES:
        raise CloudWatchPayloadError("invalid alarm state")
    state_time_text = _text(payload, "StateChangeTime", 64)
    state_changed_at = parse_datetime(state_time_text)
    if state_changed_at is None or state_changed_at.tzinfo is None:
        raise CloudWatchPayloadError("invalid StateChangeTime")

    trigger = payload.get("Trigger")
    metric_namespace = ""
    metric_name = ""
    dimensions = []
    if isinstance(trigger, dict):
        direct_namespace = trigger.get("Namespace")
        direct_name = trigger.get("MetricName")
        if isinstance(direct_namespace, str) and isinstance(direct_name, str):
            alarm_type = "metric"
            metric_namespace = _text(trigger, "Namespace", 255)
            metric_name = _text(trigger, "MetricName", 255)
            dimensions = _dimensions(trigger)
        elif isinstance(trigger.get("Metrics"), list):
            alarm_type = "metric_math"
            if len(trigger["Metrics"]) > 20:
                raise CloudWatchPayloadError("invalid metric math alarm")
            for query in trigger["Metrics"]:
                if not isinstance(query, dict):
                    raise CloudWatchPayloadError("invalid metric math alarm")
                metric = ((query.get("MetricStat") or {}).get("Metric") or {})
                if isinstance(metric, dict) and metric.get("Namespace") and metric.get("MetricName"):
                    metric_namespace = _text(metric, "Namespace", 255)
                    metric_name = _text(metric, "MetricName", 255)
                    dimensions = _dimensions(metric)
                    break
        else:
            alarm_type = "metric"
    elif isinstance(payload.get("AlarmRule"), str):
        alarm_type = "composite"
    elif "QueryString" in payload or "LogGroupNames" in payload:
        alarm_type = "logs"
    else:
        alarm_type = "unknown"

    reason = _text(payload, "NewStateReason", 1024, required=False)
    return {
        "alarm_name": alarm_name,
        "alarm_arn": alarm_arn,
        "account": account,
        "region": region,
        "old_state": old_state,
        "new_state": new_state,
        "reason": reason,
        "state_change_time": state_time_text,
        "state_changed_at": state_changed_at,
        "alarm_type": alarm_type,
        "metric_namespace": metric_namespace,
        "metric_name": metric_name,
        "dimensions": dimensions,
    }


def _alarm_key(alarm_arn):
    return hashlib.sha256(alarm_arn.encode("utf-8")).hexdigest()


def _locked_alarm(alarm_arn):
    key = _alarm_key(alarm_arn)
    alarm = CloudWatchAlarm.objects.select_for_update().filter(alarm_key=key).first()
    if alarm is None:
        try:
            with transaction.atomic():
                alarm = CloudWatchAlarm.objects.create(
                    alarm_key=key, alarm_arn=alarm_arn,
                )
        except IntegrityError:
            alarm = CloudWatchAlarm.objects.select_for_update().get(alarm_key=key)
    if alarm.alarm_arn != alarm_arn:
        raise CloudWatchPayloadError("alarm identity collision")
    return alarm


def _existing_transition(alarm, topic_arn, message_id, data):
    by_message = CloudWatchAlarmTransition.objects.filter(
        topic_arn=topic_arn, sns_message_id=message_id,
    ).first()
    if by_message is not None:
        if (
            by_message.alarm_id != alarm.pk
            or by_message.old_state != data["old_state"]
            or by_message.new_state != data["new_state"]
            or by_message.state_changed_at != data["state_changed_at"]
        ):
            raise CloudWatchPayloadError("SNS message identity reused")
        return by_message
    return CloudWatchAlarmTransition.objects.filter(
        alarm=alarm,
        state_changed_at=data["state_changed_at"],
        old_state=data["old_state"],
        new_state=data["new_state"],
    ).first()


def _record_transition_event(transition, alarm, data, stale=False):
    occurrence_id = alarm.opening_transition_id or transition.pk
    metadata = {
        "alarm_name": data["alarm_name"],
        "alarm_arn": data["alarm_arn"],
        "aws_account_id": data["account"],
        "region": data["region"],
        "old_state": data["old_state"],
        "new_state": data["new_state"],
        "reason": data["reason"],
        "state_change_time": data["state_change_time"],
        "alarm_type": data["alarm_type"],
        "metric_namespace": data["metric_namespace"],
        "metric_name": data["metric_name"],
        "dimensions": data["dimensions"],
        "stale": stale,
    }
    return record_event(
        data["reason"] or f"CloudWatch alarm changed to {data['new_state']}",
        title=f"CloudWatch {data['new_state']}: {data['alarm_name']}",
        category=CATEGORY,
        scope=SCOPE,
        level=4 if data["new_state"] == "ALARM" else 2,
        group=None,
        model_name=MODEL_NAME,
        model_id=occurrence_id,
        **metadata,
    )


def _add_recovery_notes(incident, data):
    from mojo.apps.incident.services import maestro_sync
    note_text = f"CloudWatch recovered at {data['state_change_time']}: {data['reason']}"
    for ticket in incident.tickets.all():
        link_ids = list(ticket.maestro_links.values_list("id", flat=True))
        note = ticket.add_note(
            note_text,
            None,
            metadata={
                "type": "cloudwatch_recovery",
                "alarm_arn": data["alarm_arn"],
            },
        )
        for link_id in link_ids:
            maestro_sync.enqueue_note(link_id, "ticket", note.pk)


def _dispatch(transition_id):
    transition = CloudWatchAlarmTransition.objects.select_related(
        "event", "incident__rule_set",
    ).get(pk=transition_id)
    if transition.dispatch_status != CloudWatchAlarmTransition.DISPATCH_PENDING:
        return
    if not transition.event_id or not transition.incident_id or not transition.incident.rule_set_id:
        transition.dispatch_status = CloudWatchAlarmTransition.DISPATCH_COMPLETE
        transition.save(update_fields=["dispatch_status", "modified"])
        return
    try:
        transition.incident.rule_set.run_handler(
            transition.event,
            transition.incident,
            idempotency_prefix=f"aws-cw:{transition.pk}",
            strict=True,
        )
    except Exception as exc:
        raise CloudWatchDispatchError("CloudWatch handler dispatch failed") from exc
    transition.dispatch_status = CloudWatchAlarmTransition.DISPATCH_COMPLETE
    transition.save(update_fields=["dispatch_status", "modified"])


def process_notification(envelope):
    topic_arn = envelope.get("TopicArn")
    message_id = envelope.get("MessageId")
    if not isinstance(topic_arn, str) or not isinstance(message_id, str) or len(message_id) > 100:
        raise CloudWatchPayloadError("invalid SNS notification identity")
    try:
        payload = json.loads(envelope.get("Message", ""))
    except Exception:
        raise CloudWatchPayloadError("invalid CloudWatch message")
    data = normalize(payload)

    duplicate = False
    with transaction.atomic():
        alarm = _locked_alarm(data["alarm_arn"])
        existing = _existing_transition(alarm, topic_arn, message_id, data)
        if existing is not None:
            transition = existing
            duplicate = True
        else:
            transition = CloudWatchAlarmTransition.objects.create(
                alarm=alarm,
                topic_arn=topic_arn,
                sns_message_id=message_id,
                old_state=data["old_state"],
                new_state=data["new_state"],
                state_changed_at=data["state_changed_at"],
            )
            stale = bool(
                alarm.last_state_change
                and data["state_changed_at"] <= alarm.last_state_change
            )
            if data["new_state"] == "ALARM" and not alarm.opening_transition_id and not stale:
                alarm.opening_transition = transition
            event = _record_transition_event(transition, alarm, data, stale=stale)
            transition.event = event

            if not stale and data["new_state"] == "ALARM":
                result = event.publish(use_catchall=False, dispatch_handlers=False)
                incident = result["incident"]
                transition.incident = incident
                if incident is not None:
                    alarm.active_incident = incident
                if result["should_dispatch"]:
                    transition.dispatch_status = CloudWatchAlarmTransition.DISPATCH_PENDING
            elif (
                not stale
                and alarm.active_incident_id
                and data["new_state"] in ("OK", "INSUFFICIENT_DATA")
            ):
                from mojo.apps.incident.models import Incident
                active_incident = Incident.objects.select_for_update().get(
                    pk=alarm.active_incident_id,
                )
                event.link_to_incident(active_incident)
                transition.incident = active_incident
                if data["new_state"] == "INSUFFICIENT_DATA":
                    active_incident.add_history(
                        "cloudwatch:insufficient_data",
                        note=f"CloudWatch entered INSUFFICIENT_DATA at {data['state_change_time']}",
                    )
                else:
                    _add_recovery_notes(active_incident, data)
                    resolve_incident(
                        active_incident,
                        note=f"CloudWatch recovered at {data['state_change_time']}: {data['reason']}",
                        kind="cloudwatch:recovered",
                    )
                    if event.pk and not event.__class__.objects.filter(pk=event.pk).exists():
                        transition.event = None
                    if transition.incident.pk is None:
                        transition.incident = None
                    alarm.active_incident = None
                    alarm.opening_transition = None

            transition.save(
                update_fields=["event", "incident", "dispatch_status", "modified"]
            )
            if not stale:
                alarm.current_state = data["new_state"]
                alarm.last_state_change = data["state_changed_at"]
                alarm.save(update_fields=[
                    "current_state", "last_state_change", "active_incident",
                    "opening_transition", "modified",
                ])

    _dispatch(transition.pk)
    return {"duplicate": duplicate, "transition_id": transition.pk}
