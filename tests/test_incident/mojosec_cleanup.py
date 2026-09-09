import datetime
import io
import json
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError

from testit import helpers as th


MIXED_SENSOR = "mojosec-cleanup-mixed"
SAFE_SENSOR = "mojosec-cleanup-safe"


def _event(sensor, command_path="/usr/local/sbin/mojo-firewall-broker",
           proof_status="missing"):
    from mojo.apps.incident.models import Event

    return Event.objects.create(
        scope="mojosec", category="mojosec.auth.sudo_command", level=8,
        title="MojoSec detected auth.sudo_command",
        metadata={"mojosec": {
            "sensor_id": sensor,
            "kind": "auth.sudo_command",
            "evidence": {
                "command_path": command_path,
                "proof_status": proof_status,
            },
        }},
    )


def _receipt(key, event, handler_state="none", publish_state="published",
             incident=None):
    from mojo.apps.incident.models import MojoSecReceipt

    return MojoSecReceipt.objects.create(
        api_key=key,
        event=event,
        incident=incident,
        sensor_id=event.metadata["mojosec"]["sensor_id"],
        wire_event_id=f"cleanup-{event.pk}",
        payload_digest="a" * 64,
        publish_state=publish_state,
        handler_state=handler_state,
        replay_features={"event": {"id": f"cleanup-{event.pk}"}},
    )


@th.django_unit_setup()
def setup_mojosec_cleanup(opts):
    from mojo.apps.account.models import ApiKey, Group
    from mojo.apps.incident.models import Event, Incident, MojoSecReceipt

    MojoSecReceipt.objects.filter(sensor_id__in=(MIXED_SENSOR, SAFE_SENSOR)).delete()
    Event.objects.filter(
        metadata__mojosec__sensor_id__in=(MIXED_SENSOR, SAFE_SENSOR)).delete()
    ApiKey.objects.filter(name="mojosec_cleanup_key").delete()
    Group.objects.filter(name="mojosec_cleanup_group").delete()
    group = Group.objects.create(name="mojosec_cleanup_group", kind="organization")
    key, unused_token = ApiKey.create_for_group(
        group, "mojosec_cleanup_key", permissions={"mojosec_ingest": True})
    opts.cleanup_key_id = key.pk

    mixed_safe = _event(MIXED_SENSOR)
    _receipt(key, mixed_safe)
    linked_incident = Incident.objects.create(
        category="cleanup-test", scope="mojosec", title="linked")
    linked = _event(MIXED_SENSOR)
    linked.incident = linked_incident
    linked.save(update_fields=["incident"])
    _receipt(key, linked)
    pending = _event(MIXED_SENSOR)
    _receipt(key, pending, handler_state="pending", publish_state="pending")
    receipt_incident = _event(MIXED_SENSOR)
    _receipt(key, receipt_incident, incident=linked_incident)
    unrelated = _event(
        MIXED_SENSOR, command_path="/usr/bin/bash", proof_status="conflict")
    _receipt(key, unrelated)
    opts.mixed_ids = [mixed_safe.pk, linked.pk, pending.pk, receipt_incident.pk]
    opts.unrelated_id = unrelated.pk

    opts.safe_ids = []
    opts.safe_receipt_ids = []
    for handler_state in ("none", "dispatched", "dead"):
        event = _event(SAFE_SENSOR)
        receipt = _receipt(key, event, handler_state=handler_state)
        opts.safe_ids.append(event.pk)
        opts.safe_receipt_ids.append(receipt.pk)
    safe_unrelated = _event(SAFE_SENSOR, command_path="/usr/bin/bash")
    _receipt(key, safe_unrelated)
    opts.safe_unrelated_id = safe_unrelated.pk

    now = datetime.datetime.now(datetime.timezone.utc)
    opts.cleanup_since = (now - datetime.timedelta(hours=1)).isoformat()
    opts.cleanup_before = now.isoformat()


@th.django_unit_test("broker flood dry run separates every unsafe exact match")
def test_broker_flood_cleanup_dry_run_is_exact(opts):
    from mojo.apps.incident.models import Event

    output = io.StringIO()
    call_command(
        "prune_mojosec_broker_flood",
        sensor=MIXED_SENSOR,
        since=opts.cleanup_since,
        before=opts.cleanup_before,
        stdout=output,
    )
    result = json.loads(output.getvalue().strip())
    th.assert_eq(
        (result["matched"], result["safe"], result["unsafe"]), (4, 1, 3),
        "the preview hid linked/nonterminal evidence or widened the signature")
    th.assert_true(Event.objects.filter(pk__in=opts.mixed_ids).count() == 4,
                   "a dry run mutated matching evidence")
    th.assert_true(Event.objects.filter(pk=opts.unrelated_id).exists(),
                   "the exact preview selected unrelated sudo evidence")


@th.django_unit_test("broker flood apply refuses unsafe matches and explicit ceilings")
def test_broker_flood_cleanup_refuses_unsafe_or_unbounded_apply(opts):
    with th.assert_raises(CommandError):
        call_command(
            "prune_mojosec_broker_flood", sensor=MIXED_SENSOR,
            since=opts.cleanup_since, before=opts.cleanup_before,
            apply=True, max_events=10)
    with th.assert_raises(CommandError):
        call_command(
            "prune_mojosec_broker_flood", sensor=SAFE_SENSOR,
            since=opts.cleanup_since, before=opts.cleanup_before, apply=True)
    with th.assert_raises(CommandError):
        call_command(
            "prune_mojosec_broker_flood", sensor=SAFE_SENSOR,
            since=opts.cleanup_since, before=opts.cleanup_before,
            apply=True, max_events=2)
    with th.assert_raises(CommandError):
        call_command(
            "prune_mojosec_broker_flood", sensor=SAFE_SENSOR,
            since="2026-09-09T00:00:00", before=opts.cleanup_before)


@th.django_unit_test("bounded apply deletes Events but retains receipts and unrelated sudo")
def test_broker_flood_cleanup_preserves_audit_receipts(opts):
    from mojo.apps.incident.models import Event, MojoSecReceipt
    from mojo.apps.incident.management.commands import prune_mojosec_broker_flood

    output = io.StringIO()
    with mock.patch.object(prune_mojosec_broker_flood, "BATCH_SIZE", 2):
        call_command(
            "prune_mojosec_broker_flood", sensor=SAFE_SENSOR,
            since=opts.cleanup_since, before=opts.cleanup_before,
            apply=True, max_events=3, stdout=output)
    result = json.loads(output.getvalue().strip())
    th.assert_eq(result["deleted"], 3,
                 "bounded apply did not delete the exact previewed Events")
    th.assert_true(not Event.objects.filter(pk__in=opts.safe_ids).exists(),
                   "exact safe flood Events remained after approved apply")
    th.assert_true(Event.objects.filter(pk=opts.safe_unrelated_id).exists(),
                   "cleanup deleted unrelated sudo evidence")
    receipts = MojoSecReceipt.objects.filter(pk__in=opts.safe_receipt_ids)
    th.assert_eq(receipts.count(), 3,
                 "cleanup deleted durable receipt/replay audit rows")
    th.assert_true(not receipts.exclude(event__isnull=True).exists(),
                   "retained receipts still reference deleted Event rows")
