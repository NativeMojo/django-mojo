import base64
import json
from datetime import timedelta

from testit import helpers as th


TOPIC = "arn:aws:sns:us-east-1:123456789012:guardduty"
CLOUDWATCH_TOPIC = "arn:aws:sns:us-east-1:123456789012:operations"
FINDING_ID = "abcdef0123456789abcdef0123456789"
DETECTOR_ID = "0123456789abcdef0123456789abcdef"
ACCOUNT = "123456789012"
REGION = "us-east-1"
FINDING_TYPE = "UnauthorizedAccess:EC2/SSHBruteForce"
CATEGORY = f"aws:guardduty:{FINDING_TYPE}"
SCOPE = "aws:guardduty"
REMOTE_IP = "198.51.100.7"


def _inbound_action(direction="INBOUND"):
    return {
        "actionType": "NETWORK_CONNECTION",
        "networkConnectionAction": {
            "connectionDirection": direction,
            "protocol": "TCP",
            "blocked": False,
            "localPortDetails": {"port": 22, "portName": "SSH"},
            "remotePortDetails": {"port": 41234},
            "remoteIpDetails": {
                "ipAddressV4": REMOTE_IP,
                "country": {"countryName": "Elbonia"},
            },
        },
    }


def _resource():
    return {
        "resourceType": "Instance",
        "instanceDetails": {
            "instanceId": "i-0abc1234def567890",
            "instanceType": "t3.micro",
            "tags": [{"key": "Name", "value": "web-1"}],
        },
    }


def _finding(updated, severity=8.0, finding_id=FINDING_ID, resource=None,
             action=None, extras=None, detector_id=DETECTOR_ID):
    from django.utils import timezone

    detail = {
        "id": finding_id,
        "type": FINDING_TYPE,
        "severity": severity,
        "title": "SSH brute force attack against an EC2 instance",
        "description": f"{REMOTE_IP} is performing SSH brute force attacks",
        "accountId": ACCOUNT,
        "region": REGION,
        "createdAt": timezone.now().isoformat(),
        "updatedAt": updated.isoformat(),
        "resource": _resource() if resource is None else resource,
        "service": {
            "detectorId": detector_id,
            "count": 3,
            "action": _inbound_action() if action is None else action,
        },
    }
    detail.update(extras or {})
    return detail


def _eventbridge(detail, source="aws.guardduty", detail_type="GuardDuty Finding"):
    from django.utils import timezone

    return {
        "version": "0",
        "id": "3f4c1d2e-0000-4000-8000-abcdefabcdef",
        "detail-type": detail_type,
        "source": source,
        "account": ACCOUNT,
        "time": timezone.now().isoformat(),
        "region": REGION,
        "resources": [],
        "detail": detail,
    }


def _envelope(message_id, message, topic=TOPIC):
    from django.utils import timezone

    return {
        "Type": "Notification",
        "MessageId": message_id,
        "TopicArn": topic,
        "Message": json.dumps(message),
        "Timestamp": timezone.now().isoformat(),
        "SignatureVersion": "2",
        "SigningCertURL": (
            "https://sns.us-east-1.amazonaws.com/"
            "SimpleNotificationService-test.pem"
        ),
        "Signature": base64.b64encode(b"invalid until signed").decode("ascii"),
    }


def _clear_state():
    from mojo.apps.aws.models import GuardDutyFinding
    from mojo.apps.incident.models import (
        Event, Incident, MaestroItemLink, RuleSet, Ticket,
    )
    from mojo.apps.jobs.models import Job

    Job.objects.filter(idempotency_key__startswith="aws-gd:").delete()
    MaestroItemLink.objects.filter(ticket__category="guardduty-test").delete()
    Ticket.objects.filter(category="guardduty-test").delete()
    GuardDutyFinding.objects.all().delete()
    Event.objects.filter(scope=SCOPE).delete()
    Incident.objects.filter(scope=SCOPE).delete()
    RuleSet.objects.filter(category=SCOPE).delete()


def _events():
    from mojo.apps.incident.models import Event
    return Event.objects.filter(scope=SCOPE).order_by("id")


def _incidents():
    from mojo.apps.incident.models import Incident
    return Incident.objects.filter(scope=SCOPE).order_by("id")


def _handler_jobs():
    from mojo.apps.jobs.models import Job
    return Job.objects.filter(idempotency_key__startswith="aws-gd:")


def _ticket_policy():
    """A RuleSet with no conditions, so every GuardDuty level reaches it."""
    from mojo.apps.incident.models import BundleBy, RuleSet
    return RuleSet.objects.create(
        category=SCOPE,
        name="GuardDuty test policy",
        bundle_by=BundleBy.MODEL_NAME_AND_ID,
        bundle_minutes=None,
        handler="ticket://?category=guardduty-test&board=17",
    )


@th.django_unit_setup()
def setup_guardduty_ingress(opts):
    _clear_state()


@th.django_unit_test()
def test_endpoint_is_routed(opts):
    """The receiver must be reachable at its documented URL."""
    response = opts.client.get("/api/aws/guardduty/sns/finding")
    assert response.status_code == 405, (
        "GET /api/aws/guardduty/sns/finding must reach the receiver and be "
        "refused with 405. A 404 means the endpoint is NOT wired into the URL "
        f"map at all, so AWS could never deliver a finding. Got "
        f"{response.status_code}."
    )


@th.django_unit_test()
def test_severity_bands_never_reach_the_incident_threshold(opts):
    from mojo.apps.aws.services.guardduty_findings import (
        GuardDutyPayloadError,
        severity_to_level,
    )
    from mojo.apps.incident.models.event import INCIDENT_LEVEL_THRESHOLD

    expected = {
        0.0: 2, 0.9: 2, 1.0: 4, 3.9: 4, 4.0: 6,
        6.9: 6, 7.0: 6, 8.9: 6, 9.0: 6, 10.0: 6,
    }
    for severity, level in expected.items():
        actual = severity_to_level(severity)
        assert actual == level, (
            f"GuardDuty severity {severity} must map to level {level}, got {actual}"
        )
        assert actual < INCIDENT_LEVEL_THRESHOLD, (
            f"Severity {severity} mapped to level {actual}, which is at or above "
            f"INCIDENT_LEVEL_THRESHOLD ({INCIDENT_LEVEL_THRESHOLD}). This is the "
            "approved policy decision: a GuardDuty finding must never auto-open "
            "an incident, LLM triage job, or IP threat stamp on its own — an "
            "operator opts in with a RuleSet."
        )

    assert severity_to_level(5) == 6, (
        "GuardDuty commonly emits integer severities; 5 must map to level 6"
    )
    for bad in (10.1, -0.1, "high", None, True, float("nan")):
        with th.assert_raises(GuardDutyPayloadError):
            severity_to_level(bad)


@th.django_unit_test()
def test_malformed_findings_are_rejected(opts):
    from django.utils import timezone
    from mojo.apps.aws.services.guardduty_findings import (
        GuardDutyPayloadError,
        normalize,
    )

    now = timezone.now()
    with th.assert_raises(GuardDutyPayloadError):
        normalize(_eventbridge(_finding(now), source="aws.securityhub"))
    with th.assert_raises(GuardDutyPayloadError):
        normalize(_eventbridge(_finding(now), detail_type="GuardDuty Finding v2"))

    for field in ("id", "type", "severity", "title", "accountId", "region", "updatedAt"):
        broken = _finding(now)
        broken.pop(field)
        with th.assert_raises(GuardDutyPayloadError):
            normalize(_eventbridge(broken))

    naive = _finding(now, extras={"updatedAt": "2026-08-06T12:00:00"})
    with th.assert_raises(GuardDutyPayloadError):
        normalize(_eventbridge(naive))

    oversized = _finding(now, extras={"type": "A" * 200})
    with th.assert_raises(GuardDutyPayloadError):
        normalize(_eventbridge(oversized))

    injected = _finding(now, extras={"type": "Recon:EC2/<script>alert(1)</script>"})
    with th.assert_raises(GuardDutyPayloadError):
        normalize(_eventbridge(injected))

    unknown_type = _finding(now, extras={"type": "Brand:New/DetectorFamily.v9-x"})
    normalized = normalize(_eventbridge(unknown_type))
    assert normalized["finding_type"] == "Brand:New/DetectorFamily.v9-x", (
        "An unknown finding type is data, not an enum — a new AWS detector "
        "family must be accepted, never rejected with a 400"
    )


@th.django_unit_test()
def test_high_severity_finding_opens_no_incident_without_a_ruleset(opts):
    from django.utils import timezone
    from mojo.apps.aws.models import GuardDutyFinding
    from mojo.apps.aws.services.guardduty_findings import process_notification
    from mojo.apps.incident.models import RuleSet

    _clear_state()
    process_notification(
        _envelope("gd-no-policy", _eventbridge(_finding(timezone.now(), severity=8.4)))
    )
    event = _events().get()
    assert event.level == 6, (
        f"A High (8.4) finding must record at level 6, got {event.level}"
    )
    assert event.incident_id is None, (
        "APPROVED POLICY: a High GuardDuty finding must NOT auto-open an "
        "incident. Enabling the receiver alone only records events."
    )
    assert not _incidents().exists(), (
        "No incident may exist for a GuardDuty finding with no RuleSet installed"
    )

    RuleSet.ensure_catchall_rules()
    process_notification(
        _envelope(
            "gd-catchall",
            _eventbridge(_finding(timezone.now() + timedelta(minutes=1), severity=9.9)),
        )
    )
    assert not _incidents().exists(), (
        "GuardDuty must not fall through to the seeded wildcard RuleSet — the "
        "receiver publishes with use_catchall=False so that enabling it cannot "
        "silently start opening incidents through a generic catch-all policy"
    )
    finding = GuardDutyFinding.objects.get()
    assert finding.occurrence_count == 2, (
        f"Both deliveries must be recorded on one finding row, got "
        f"{finding.occurrence_count}"
    )


@th.django_unit_test()
def test_repeat_finding_accumulates_on_one_occurrence(opts):
    from django.utils import timezone
    from mojo.apps.aws.models import GuardDutyFinding
    from mojo.apps.aws.services.guardduty_findings import process_notification

    _clear_state()
    _ticket_policy()
    started = timezone.now()
    process_notification(
        _envelope("gd-low", _eventbridge(_finding(started, severity=2.0)))
    )
    incident = _incidents().get()
    assert incident.priority == 4, (
        f"A Low (2.0) finding must open its incident at priority 4, got "
        f"{incident.priority}"
    )

    process_notification(
        _envelope(
            "gd-medium",
            _eventbridge(_finding(started + timedelta(minutes=5), severity=5.0)),
        )
    )
    events = list(_events())
    incident.refresh_from_db()

    assert len(events) == 2, f"Each new occurrence must record an Event, got {len(events)}"
    assert _incidents().count() == 1, (
        "A repeat of the same live finding must reuse its incident, not open a second"
    )
    assert GuardDutyFinding.objects.count() == 1, (
        "Repeat deliveries of one finding must share one GuardDutyFinding row"
    )
    assert events[0].model_id == events[0].pk, (
        "The opening Event names the occurrence, so its model_id must be its own pk"
    )
    assert events[1].model_id == events[0].pk, (
        f"Every Event of one occurrence must carry the SAME rotated model_id "
        f"({events[0].pk}), got {events[1].model_id}"
    )
    assert events[1].incident_id == incident.pk, (
        "The repeat Event must be linked to the finding's live incident"
    )
    assert incident.priority == 6, (
        f"A Medium repeat must escalate the incident priority from 4 to 6, got "
        f"{incident.priority}"
    )
    assert incident.history.filter(kind="priority_escalated").exists(), (
        "A priority escalation must leave a history row for the operator"
    )
    assert (incident.metadata or {}).get("event_count") == 2, (
        f"Linking the repeat must bump the incident event_count, got "
        f"{(incident.metadata or {}).get('event_count')}"
    )


@th.django_unit_test()
def test_replayed_and_out_of_order_deliveries_are_duplicates(opts):
    from django.utils import timezone
    from mojo.apps.aws.models import GuardDutyFinding
    from mojo.apps.aws.services.guardduty_findings import process_notification

    _clear_state()
    latest = timezone.now()
    process_notification(_envelope("gd-first", _eventbridge(_finding(latest))))

    replay = process_notification(
        _envelope("gd-replay", _eventbridge(_finding(latest)))
    )
    assert replay["duplicate"] is True, (
        "A redelivery carrying the same (id, updatedAt) must be a duplicate"
    )

    older = process_notification(
        _envelope("gd-older", _eventbridge(_finding(latest - timedelta(hours=1))))
    )
    assert older["duplicate"] is True, (
        "An out-of-order delivery older than the watermark must be a duplicate"
    )

    assert _events().count() == 1, (
        f"Duplicates must not record a second Event, got {_events().count()}"
    )
    finding = GuardDutyFinding.objects.get()
    assert finding.occurrence_count == 1, (
        f"Duplicates must not bump the occurrence count, got {finding.occurrence_count}"
    )
    assert finding.last_updated_at == latest, (
        f"The dedupe watermark must never regress to an older updatedAt; "
        f"expected {latest}, got {finding.last_updated_at}"
    )


@th.django_unit_test()
def test_shipped_policy_dispatches_once_per_delivery(opts):
    from django.utils import timezone
    from mojo.apps.aws.models import GuardDutyFinding
    from mojo.apps.aws.services.guardduty_findings import process_notification
    from mojo.apps.incident.models import BundleBy, RuleSet

    _clear_state()
    ruleset, _created = RuleSet.ensure_guardduty_rules()
    assert ruleset.category == SCOPE, (
        f"The shipped policy must be reachable by event scope '{SCOPE}', got "
        f"'{ruleset.category}'"
    )
    assert ruleset.bundle_by == BundleBy.MODEL_NAME_AND_ID, (
        "The shipped policy must bundle by the rotating occurrence id"
    )

    process_notification(
        _envelope("gd-policy", _eventbridge(_finding(timezone.now(), severity=8.0)))
    )
    finding = GuardDutyFinding.objects.get()
    event = _events().get()
    assert _incidents().count() == 1, (
        "An explicit GuardDuty RuleSet must open exactly one incident"
    )
    assert _handler_jobs().count() == 1, (
        f"The shipped policy handler must publish exactly one job, got "
        f"{_handler_jobs().count()}"
    )
    assert _handler_jobs().filter(
        idempotency_key__startswith=f"aws-gd:{finding.pk}:{event.pk}",
    ).exists(), (
        "The handler job must be keyed per OCCURRENCE "
        f"(aws-gd:{finding.pk}:{event.pk}), not per finding"
    )
    assert finding.dispatch_status == GuardDutyFinding.DISPATCH_COMPLETE, (
        f"A completed dispatch must clear the pending marker, got "
        f"{finding.dispatch_status}"
    )

    process_notification(
        _envelope("gd-policy-replay", _eventbridge(_finding(finding.last_updated_at)))
    )
    assert _events().count() == 1, "A redelivery must not record a second Event"
    assert _handler_jobs().count() == 1, (
        "A redelivery must not publish a second handler job"
    )


@th.django_unit_test()
def test_persisted_metadata_is_bounded(opts):
    from django.utils import timezone
    from mojo.apps.aws.services.guardduty_findings import process_notification

    _clear_state()
    stuffed = {
        "resourceType": "Instance",
        "instanceDetails": {
            "instanceId": "i-0abc1234def567890",
            "tags": [{"key": f"tag{i}", "value": "v" * 64} for i in range(50)],
            "blob": "z" * 10240,
            "secretAccessKey": "must-not-persist",
            "productCodes": [{"productCodeId": "must-not-persist"}],
        },
    }
    process_notification(
        _envelope(
            "gd-bounded",
            _eventbridge(_finding(timezone.now(), resource=stuffed)),
        )
    )
    event = _events().get()
    persisted = json.dumps(event.metadata)
    assert "must-not-persist" not in persisted, (
        "Only allowlisted leaf names may be copied out of a finding resource; "
        "credential-like and unexpected fields must never be persisted"
    )
    assert "z" * 100 not in persisted, (
        "An oversized blob inside the resource must never reach Event.metadata"
    )
    assert "tag49" not in persisted, (
        "Arbitrary resource tags must not be persisted"
    )
    identifiers = event.metadata.get("identifiers")
    assert isinstance(identifiers, list) and len(identifiers) <= 10, (
        f"The identifier projection must be bounded to 10 entries, got {identifiers!r}"
    )
    assert {"name": "instanceId", "value": "i-0abc1234def567890"} in identifiers, (
        f"The allowlisted instanceId must survive the projection, got {identifiers!r}"
    )
    assert event.source_ip == REMOTE_IP, (
        f"An INBOUND connection's remote address is the actor and must become "
        f"source_ip, got {event.source_ip!r}"
    )

    outbound = _finding(
        timezone.now() + timedelta(minutes=1),
        action=_inbound_action(direction="OUTBOUND"),
    )
    process_notification(_envelope("gd-outbound", _eventbridge(outbound)))
    egress = _events().order_by("id").last()
    assert egress.source_ip is None, (
        "An OUTBOUND connection's remote address is a destination OUR host "
        "chose to contact, not an actor. Feeding it to inbound threat scoring "
        f"would poison GeoLocatedIP threat levels. Got {egress.source_ip!r}"
    )
    assert egress.metadata.get("remote_ip") == REMOTE_IP, (
        "The outbound destination must still be recorded in metadata for triage"
    )


@th.django_unit_test()
def test_occurrence_after_resolution_opens_a_new_incident(opts):
    from django.utils import timezone
    from mojo.apps.aws.models import GuardDutyFinding
    from mojo.apps.aws.services.guardduty_findings import process_notification
    from mojo.apps.incident.services.lifecycle import resolve_incident

    _clear_state()
    _ticket_policy()
    started = timezone.now()
    process_notification(_envelope("gd-open", _eventbridge(_finding(started))))
    first_incident = _incidents().get()
    first_event = _events().get()

    resolve_incident(first_incident, note="handled", kind="guardduty:test")
    process_notification(
        _envelope("gd-reopen", _eventbridge(_finding(started + timedelta(days=1))))
    )

    finding = GuardDutyFinding.objects.get()
    second_event = _events().order_by("id").last()
    assert _incidents().count() == 2, (
        f"A finding recurring after its incident was resolved must open a NEW "
        f"incident, got {_incidents().count()}"
    )
    assert GuardDutyFinding.objects.count() == 1, (
        "A new occurrence must still share the one durable finding row"
    )
    assert finding.opening_event_id == second_event.pk, (
        f"opening_event must rotate to the new occurrence's Event "
        f"({second_event.pk}), got {finding.opening_event_id}"
    )
    assert second_event.model_id == second_event.pk, (
        f"The new occurrence's model_id must rotate off {first_event.pk}"
    )
    assert finding.active_incident_id == second_event.incident_id, (
        "The finding must track its new live incident"
    )


@th.django_unit_test()
def test_recurrence_never_bundles_back_into_a_resolved_incident(opts):
    from django.utils import timezone
    from mojo.apps.aws.services.guardduty_findings import process_notification
    from mojo.apps.incident.services.lifecycle import resolve_incident

    _clear_state()
    _ticket_policy()
    started = timezone.now()
    process_notification(_envelope("gd-bundle-1", _eventbridge(_finding(started))))
    resolved = _incidents().get()
    resolve_incident(resolved, note="closed out", kind="guardduty:test")

    process_notification(
        _envelope("gd-bundle-2", _eventbridge(_finding(started + timedelta(days=3))))
    )
    resolved.refresh_from_db()
    recurrence = _events().order_by("id").last()

    assert resolved.status == "resolved", (
        f"The first incident must stay resolved, got status {resolved.status!r}"
    )
    assert recurrence.incident_id is not None, (
        "The recurrence must still be attached to an incident"
    )
    assert recurrence.incident_id != resolved.pk, (
        "REGRESSION GUARD: determine_bundle_criteria has no status filter and "
        "(with bundle_minutes=None) no time filter, so the bundle key reduces "
        "to {category, model_name, model_id} over all history. If model_id were "
        "stamped with the finding pk instead of the rotating occurrence handle, "
        "this recurrence would bundle straight back into the RESOLVED incident "
        f"({resolved.pk}) and silently reopen closed work."
    )
    assert resolved.events.count() == 1, (
        f"The resolved incident must not gain events after resolution, got "
        f"{resolved.events.count()}"
    )

