"""Authority and fleet evidence regressions for asynchronous configuration apply."""

from types import SimpleNamespace
from unittest import mock

from django.core import signing
from testit import helpers as th


REVISION = "a" * 32
OPERATION = "b" * 32


def _job():
    intent = dict(operation_id=OPERATION, revision=REVISION,
                  nodes=["node-a", "node-offline"], actor_id=101)
    return SimpleNamespace(pk=OPERATION, payload={
        "intent": signing.dumps(intent, salt="mojo.fleet.apply.intent")},
        metadata={}, status="completed"), intent


def _reply(revision=REVISION, **flags):
    result = dict(revision=revision, node="node-a", installed=True,
                  restarted=True, healthy=True, error_code=None)
    result.update(flags)
    return {"status": "verified", "anomalies": [], "results": [
        {"host": "node-a", "status": "success", "result": result}]}


@th.django_unit_test("fleet apply refuses forged authority and copied job intent")
def test_apply_intent_authority(opts):
    from mojo import errors as me
    from mojo.apps.account.services import fleet_apply

    forged = SimpleNamespace(pk=OPERATION, payload={
        "revision": REVISION, "nodes": ["node-a"], "actor_id": 1})
    with th.assert_raises(me.PermissionDeniedException):
        fleet_apply.run(forged)
    job, intent = _job()
    assert fleet_apply._intent(job, max_age=300) == intent, 'Fleet configuration contract failed: fleet_apply._intent(job, max_age=300) == intent'
    job.pk = "c" * 32
    with th.assert_raises(me.PermissionDeniedException):
        fleet_apply._intent(job, max_age=300)
    job.pk = OPERATION
    job.payload["intent"] += "tampered"
    with th.assert_raises(me.PermissionDeniedException):
        fleet_apply._intent(job, max_age=300)
    job, _ = _job()
    with mock.patch("django.core.signing.time.time", return_value=10**12):
        with th.assert_raises(me.PermissionDeniedException):
            fleet_apply._intent(job, max_age=300)


@th.django_unit_test("fleet operation accepts only signed results bound to its exact intent")
def test_apply_result_authority(opts):
    from mojo.apps.jobs.models import Job
    from mojo.apps.account.services import fleet_apply, provider_setup

    job, intent = _job()
    healthy = dict(status="healthy", healthy_everywhere=True, nodes=[])
    with mock.patch.object(provider_setup, "_superuser"), \
            mock.patch.object(Job.objects, "filter") as lookup:
        lookup.return_value.first.return_value = job
        job.metadata["fleet_config"] = healthy
        result = fleet_apply.operation(object(), OPERATION)
        assert result["healthy_everywhere"] is False, 'Fleet configuration contract failed: result["healthy_everywhere"] is False'
        assert result["error_code"] == "operation_evidence_invalid", 'Fleet configuration contract failed: result["error_code"] == "operation_evidence_invalid"'
        wrong = dict(intent, nodes=["node-a"])
        job.metadata["fleet_config"] = signing.dumps(
            {"intent": wrong, "report": healthy}, salt="mojo.fleet.apply.result")
        assert fleet_apply.operation(object(), OPERATION)["healthy_everywhere"] is False, 'Fleet configuration contract failed: fleet_apply.operation(object(), OPERATION)["healthy_everywhere"] is False'
        job.metadata["fleet_config"] = signing.dumps(
            {"intent": intent, "report": healthy}, salt="mojo.fleet.apply.result")
        assert fleet_apply.operation(object(), OPERATION)["healthy_everywhere"] is True, 'Fleet configuration contract failed: fleet_apply.operation(object(), OPERATION)["healthy_everywhere"] is True'
        job.status = "canceled"
        assert fleet_apply.operation(object(), OPERATION)["healthy_everywhere"] is False, 'Fleet configuration contract failed: fleet_apply.operation(object(), OPERATION)["healthy_everywhere"] is False'


@th.django_unit_test("fleet completion retains offline nodes and rejects stale ambiguous evidence")
def test_apply_node_evidence(opts):
    from mojo.apps.account.services import fleet_apply

    expected = ["node-a", "node-offline"]
    nodes = fleet_apply._nodes(REVISION, expected, _reply())
    assert [node["hostname"] for node in nodes] == expected, 'Fleet configuration contract failed: [node["hostname"] for node in nodes] == expected'
    assert nodes[0]["healthy"] and not nodes[1]["healthy"], 'Fleet configuration contract failed: nodes[0]["healthy"] and not nodes[1]["healthy"]'
    assert nodes[1]["error_code"] == "node_did_not_reply", 'Fleet configuration contract failed: nodes[1]["error_code"] == "node_did_not_reply"'
    assert not fleet_apply._nodes(REVISION, expected, _reply("d" * 32))[0]["healthy"], 'Fleet configuration contract failed: not fleet_apply._nodes(REVISION, expected, _reply("d" * 32))[0]["healthy"]'
    for flags in ({"installed": False}, {"restarted": False}, {"healthy": "true"}):
        assert not fleet_apply._nodes(REVISION, expected, _reply(**flags))[0]["healthy"], 'Fleet configuration contract failed: not fleet_apply._nodes(REVISION, expected, _reply(**flags))[0]["healthy"]'
    reply = _reply()
    reply["anomalies"] = ["duplicate_reply:node-a"]
    assert not fleet_apply._nodes(REVISION, expected, reply)[0]["healthy"], 'Fleet configuration contract failed: not fleet_apply._nodes(REVISION, expected, reply)[0]["healthy"]'
    reply = _reply()
    reply["results"][0]["status"] = "failed"
    assert not fleet_apply._nodes(REVISION, expected, reply)[0]["healthy"], 'Fleet configuration contract failed: not fleet_apply._nodes(REVISION, expected, reply)[0]["healthy"]'
    reply = _reply(node="other-node")
    assert not fleet_apply._nodes(REVISION, expected, reply)[0]["healthy"], \
        "a result identifying a different node proved this host healthy"


@th.django_unit_test("fleet observation never shrinks its expected membership to online runners")
def test_apply_expected_membership(opts):
    from mojo.apps.account.services import fleet_apply

    manager = mock.Mock()
    manager.get_runners_bounded.return_value = [
        {"hostname": "node-a"}, {"hostname": "unrelated-node"}]
    manager.broadcast_execute_checked.return_value = _reply()
    with mock.patch.object(fleet_apply, "_channel", return_value="edge"):
        result = fleet_apply.observe(REVISION, ["node-a", "node-offline"], manager=manager)
    assert result["healthy_everywhere"] is False, 'Fleet configuration contract failed: result["healthy_everywhere"] is False'
    assert len(result["nodes"]) == 2, 'Fleet configuration contract failed: len(result["nodes"]) == 2'
    assert manager.broadcast_execute_checked.call_args.kwargs["roster"] == [{"hostname": "node-a"}], 'Fleet configuration contract failed: manager.broadcast_execute_checked.call_args.kwargs["roster"] == [{"hostname": "node-a"}]'
    with mock.patch.object(fleet_apply.settings, "get_static", return_value=["NODE-A", "node-a"]):
        from mojo import errors as me
        with th.assert_raises(me.ValueException):
            fleet_apply.expected_nodes()


@th.django_unit_test("authorized fleet apply commits signed intent then completes through the real job runner")
def test_apply_real_job_lifecycle(opts):
    from django.db import transaction
    from mojo.apps.account.models import User
    from mojo.apps.account.services import fleet_apply, fleet_config
    from mojo.apps.jobs.models import Job, JobEvent
    from mojo.apps.jobs import manager as jobs_manager

    username = "fleet-apply-lifecycle-test"
    User.objects.filter(username=username).delete()
    actor = User.objects.create(username=username, is_active=True, is_superuser=True)
    operation_id = None
    manager = mock.Mock()
    manager.get_runners_bounded.return_value = [{"hostname": "node-a"}]
    manager.broadcast_execute_checked.return_value = _reply()
    static = {"ADMIN_FLEET_CONFIG_EXPECTED_NODES": ["node-a"],
              "ADMIN_FLEET_CONFIG_CHANNEL": "edge"}
    original_static = fleet_apply.settings.get_static

    def get_static(key, default=None, **kwargs):
        return static[key] if key in static else original_static(key, default, **kwargs)

    try:
        with mock.patch.object(fleet_config, "state", return_value={"revision": REVISION}), \
                mock.patch.object(jobs_manager, "get_manager", return_value=manager), \
                mock.patch.object(fleet_apply.settings, "get_static", side_effect=get_static):
            with transaction.atomic():
                queued = fleet_apply.apply(actor, {"expected_revision": REVISION})
                operation_id = queued["operation_id"]
                assert queued["status"] == "queued", "Authorized Apply must return a queued operation"
                assert queued["healthy_everywhere"] is False, "Publishing a job cannot prove fleet health"
                stored = Job.objects.get(pk=operation_id)
                assert set(stored.payload) == {"intent"}, "Queued job must persist only signed authorization"
                intent = fleet_apply._intent(stored, max_age=300)
                assert intent == dict(operation_id=operation_id, actor_id=actor.pk,
                                      revision=REVISION, nodes=["node-a"]), \
                    "Signed intent must bind the returned job, real actor, revision, and exact nodes"
                duplicate = fleet_apply.apply(actor, {"expected_revision": REVISION})
                assert duplicate["operation_id"] == operation_id, \
                    "Repeated Apply before execution must reuse the authorized active operation"
                assert not JobEvent.objects.filter(job_id=operation_id, event="queued").exists(), \
                    "Real job publication must defer its queue event until the outer transaction commits"
                assert not manager.broadcast_execute_checked.called, \
                    "No node command may run before the authorization transaction commits"
            executed = th.run_pending_jobs(func=fleet_apply.JOB_FUNCTION,
                                            payload={"intent": stored.payload["intent"]})
            assert executed == 1, "The real job runner must execute exactly the authorized operation"
            report = fleet_apply.operation(actor, operation_id)
            assert report["operation_id"] == operation_id, "Completion must retain the returned operation id"
            assert report["healthy_everywhere"] is True, "Correlated node proof must complete authorized Apply"
            stored.refresh_from_db()
            assert stored.status == "completed", "The actual asynchronous Job must finish successfully"
            signed = signing.loads(stored.metadata["fleet_config"], salt="mojo.fleet.apply.result")
            assert signed["intent"] == intent, "The persisted result must bind the original signed authorization"
            assert signed["report"]["healthy_everywhere"] is True, "Persisted signed evidence must prove completion"
            calls = manager.broadcast_execute_checked.call_args_list
            assert any(call.args[0] == fleet_apply.TRIGGER_FUNCTION for call in calls), \
                "The asynchronous worker must request the fixed configuration-sync operation"
            assert any(call.args[0] == fleet_apply.REPORT_FUNCTION for call in calls), \
                "The asynchronous worker must collect separate serving-health evidence"
    finally:
        if operation_id:
            from mojo.apps.jobs import get_adapter, JobKeys
            get_adapter().get_client().lrem(JobKeys().queue("default"), 0, operation_id)
            Job.objects.filter(pk=operation_id).delete()
        User.objects.filter(pk=actor.pk).delete()
