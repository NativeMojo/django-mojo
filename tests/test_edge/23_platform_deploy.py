"""Durable UUID deployment truth and adversarial callback isolation."""

import uuid
from unittest import mock

from testit import helpers as th


SHA = "8" * 40


@th.django_unit_setup()
def setup_platform_deploy(opts):
    from mojo.apps.edge.models import PlatformDeployment
    from mojo.apps.edge.services import deploy
    PlatformDeployment.objects.all().delete()
    deploy.get_client().delete(deploy.TARGET_KEY, deploy.STATUS_KEY)


@th.django_unit_test("GitHub delivery ids are durable dedupe keys")
def test_github_delivery_dedupe(opts):
    from mojo.apps.edge.models import PlatformDeployment
    from mojo.apps.edge.services import platform_deploy
    first, replayed = platform_deploy.create(
        SHA, actor="github:first", source="github", source_delivery="delivery-1818",
        idempotency_key="request-one")
    second, replayed_again = platform_deploy.create(
        "9" * 40, actor="github:second", source="github",
        source_delivery="delivery-1818", idempotency_key="request-two")
    assert replayed is False and replayed_again is True
    assert first.pk == second.pk, "duplicate delivery created a second durable attempt"
    assert PlatformDeployment.objects.filter(
        source="github", source_delivery="delivery-1818").count() == 1


@th.django_unit_test("same-SHA callbacks are isolated by deployment UUID")
def test_same_sha_stale_callback_refused(opts):
    from mojo.apps.edge.services import deploy, platform_deploy
    old, _ = platform_deploy.create(SHA, source="test")
    new, _ = platform_deploy.create(SHA, source="test")
    deploy.arm_status(SHA, deployment_id=new.pk, force=True)
    assert not deploy.set_status(
        deploy.STATUS_DEPLOYING, SHA, deployment_id=old.pk), \
        "old same-SHA callback settled the newer attempt"
    assert not deploy.clear_status(old.pk), "old attempt deleted the newer Redis lease"
    current = deploy.get_status()
    assert current["deployment"] == str(new.pk)
    deploy.clear_status(new.pk)


@th.django_unit_test("node evidence retains latest truth for every frozen runner")
def test_latest_per_runner_evidence(opts):
    from mojo.apps.edge.services import platform_deploy
    row, _ = platform_deploy.create(SHA, source="test")
    row.frozen_roster = ["edge-a-engine", "edge-b-engine"]
    row.save(update_fields=["frozen_roster"])
    for revision in range(200):
        platform_deploy.evidence(
            row.pk, "edge-a-engine", "proven", proof={"revision": revision})
    platform_deploy.evidence(row.pk, "edge-b-engine", "unavailable")
    row.refresh_from_db()
    assert len(row.node_evidence) == 2
    by_runner = {item["runner"]: item for item in row.node_evidence}
    assert by_runner["edge-a-engine"]["proof"]["revision"] == 199
    assert "edge-b-engine" in by_runner, "repeated callback evicted another roster member"


@th.django_unit_test("only the edge-channel roster is frozen")
def test_exact_edge_roster(opts):
    from mojo.apps.edge.services import platform_deploy
    rows = [
        {"runner_id": "edge-b-engine", "alive": True},
        {"runner_id": "edge-a-engine", "alive": True},
        {"runner_id": "dead-engine", "alive": False},
    ]
    with mock.patch(
            "mojo.apps.jobs.get_runners_bounded",
            return_value=rows) as get_runners:
        assert platform_deploy.edge_roster() == ["edge-a-engine", "edge-b-engine"]
    get_runners.assert_called_once_with(
        channel="edge", limit=128, max_scan_pages=16, timeout=1.0)


@th.django_unit_test("edge roster overflow fails closed instead of omitting nodes")
def test_edge_roster_overflow(opts):
    from mojo.apps.edge.services import platform_deploy
    with mock.patch(
            "mojo.apps.jobs.get_runners_bounded",
            side_effect=RuntimeError("runner_roster_overflow")):
        with th.assert_raises(RuntimeError):
            platform_deploy.edge_roster()


@th.django_unit_test("bounded runner discovery proves completeness and rejects overflow")
def test_bounded_runner_discovery(opts):
    from mojo.apps.jobs.manager import JobManager

    client = mock.MagicMock()
    client.__enter__.return_value = client
    client.zcount.return_value = 0
    client.zrangebyscore.return_value = ["a", "b", "c"]
    pipe = client.pipeline.return_value
    manager = JobManager()
    with mock.patch(
            "mojo.helpers.redis.get_bounded_connection",
            return_value=client):
        with th.assert_raises(RuntimeError):
            manager.get_runners_bounded("edge", limit=2)
    assert not pipe.get.called, \
        "overflowing runner index should fail before heartbeat reads"


@th.django_unit_test("bounded runner discovery ignores unrelated Redis keyspace size")
def test_bounded_runner_discovery_does_not_scan_global_keyspace(opts):
    import json
    from django.utils import timezone
    from mojo.apps.jobs.manager import JobManager

    client = mock.MagicMock()
    client.__enter__.return_value = client
    client.zcount.return_value = 0
    client.zrangebyscore.return_value = [b"mv1-engine", b"mv2-engine"]
    client.scan.side_effect = AssertionError(
        "runner discovery must not scan the shared Redis keyspace")
    pipe = client.pipeline.return_value
    pipe.execute.return_value = [json.dumps({
        "runner_id": name, "channels": ["edge"],
        "last_heartbeat": timezone.now().isoformat(),
    }) for name in ("mv1-engine", "mv2-engine")]
    manager = JobManager()
    with mock.patch(
            "mojo.helpers.redis.get_bounded_connection",
            return_value=client) as get_connection:
        rows = manager.get_runners_bounded(
            "edge", limit=2, max_scan_pages=1)
    assert [row["runner_id"] for row in rows] == [
        "mv1-engine", "mv2-engine"], \
        "dedicated roster lookup did not return both healthy edge runners"
    assert not client.scan.called, \
        "runner discovery still depends on unrelated Redis keyspace pages"
    assert get_connection.call_args.kwargs["read_from_replicas"] is False, \
        "fleet safety discovery allowed a replica-lagged roster read"
    assert client.zrangebyscore.call_args.args[0].endswith(
        "runner_registry:edge"), \
        "edge discovery read a global rather than channel-specific index"


@th.django_unit_test("bounded Redis safety reads can require the cluster primary")
def test_bounded_connection_primary_override(opts):
    from mojo.helpers.redis import client as redis_client

    def setting(name, default=None):
        if name == "REDIS_CLUSTER":
            return True
        if name == "REDIS_READ_FROM_REPLICAS":
            return "1"
        return default

    with mock.patch.object(redis_client.settings, "get_static",
                           side_effect=setting), \
         mock.patch.object(redis_client.RedisCluster,
                           "from_url") as from_url:
        redis_client.get_bounded_connection(read_from_replicas=False)
    assert from_url.call_args.kwargs["read_from_replicas"] is False, \
        "explicit primary-only safety read inherited replica settings"


@th.django_unit_test("released fleets are reconciled into terminal UUID proof")
def test_reconcile_released_fleet(opts):
    from datetime import timedelta
    from django.utils import timezone
    from mojo.apps.edge.models import PlatformDeployment
    from mojo.apps.edge.services import platform_deploy

    PlatformDeployment.objects.filter(status="fleet").delete()
    row = PlatformDeployment.objects.create(
        sha=SHA, actor="test", source="test", request_key=str(uuid.uuid4()),
        frozen_roster=["mv1-engine", "mv2-engine"], status="fleet",
        transitions=[])
    PlatformDeployment.objects.filter(pk=row.pk).update(
        modified=timezone.now() - timedelta(
            seconds=platform_deploy.FLEET_PROOF_GRACE + 1))
    result = mock.Mock(status=PlatformDeployment.STATUS_CONVERGED)
    with mock.patch.object(
            platform_deploy, "verify", return_value=result) as verify:
        changed = platform_deploy.reconcile_stale()
    verify.assert_called_once_with(row.pk)
    assert changed == 1, \
        "the released fleet was not closed from restarted-runner proof"


@th.django_unit_test("stale declared heartbeats fail roster discovery closed")
def test_bounded_runner_discovery_rejects_stale_heartbeat(opts):
    import json
    from datetime import timedelta
    from django.utils import timezone
    from mojo.apps.jobs.manager import JobManager

    client = mock.MagicMock()
    client.__enter__.return_value = client
    client.zcount.return_value = 0
    client.zrangebyscore.return_value = ["mv1-engine"]
    client.pipeline.return_value.execute.return_value = [json.dumps({
        "runner_id": "mv1-engine", "channels": ["edge"],
        "last_heartbeat": (timezone.now() - timedelta(minutes=1)).isoformat(),
    })]
    with mock.patch(
            "mojo.helpers.redis.get_bounded_connection",
            return_value=client):
        with th.assert_raises(RuntimeError):
            JobManager().get_runners_bounded("edge")


@th.django_unit_test("malformed declared heartbeats fail roster discovery closed")
def test_bounded_runner_discovery_rejects_malformed_heartbeat(opts):
    import json
    from django.utils import timezone
    from mojo.apps.jobs.manager import JobManager

    client = mock.MagicMock()
    client.__enter__.return_value = client
    client.zcount.return_value = 0
    client.zrangebyscore.return_value = ["mv1-engine"]
    client.pipeline.return_value.execute.return_value = [json.dumps({
        "runner_id": "mv1-engine", "channels": "edge",
        "last_heartbeat": timezone.now().isoformat(),
    })]
    with mock.patch(
            "mojo.helpers.redis.get_bounded_connection",
            return_value=client):
        with th.assert_raises(RuntimeError):
            JobManager().get_runners_bounded("edge")


@th.django_unit_test("future registry timestamps fail roster discovery closed")
def test_bounded_runner_discovery_rejects_future_score(opts):
    from mojo.apps.jobs.manager import JobManager

    client = mock.MagicMock()
    client.__enter__.return_value = client
    client.zrangebyscore.return_value = []
    client.zcount.return_value = 1
    with mock.patch(
            "mojo.helpers.redis.get_bounded_connection",
            return_value=client):
        with th.assert_raises(RuntimeError):
            JobManager().get_runners_bounded("edge")


@th.django_unit_test("bounded runner discovery rejects a missing heartbeat document")
def test_bounded_runner_discovery_missing_heartbeat(opts):
    import json
    from django.utils import timezone
    from mojo.apps.jobs.manager import JobManager

    client = mock.MagicMock()
    client.__enter__.return_value = client
    client.zcount.return_value = 0
    client.zrangebyscore.return_value = ["a", "b"]
    pipe = client.pipeline.return_value
    pipe.execute.return_value = [json.dumps({
        "runner_id": "a", "channels": ["edge"],
        "last_heartbeat": timezone.now().isoformat(),
    }), None]
    manager = JobManager()
    with mock.patch(
            "mojo.helpers.redis.get_bounded_connection",
            return_value=client):
        with th.assert_raises(RuntimeError):
            manager.get_runners_bounded("edge", limit=2, max_scan_pages=3)


@th.django_unit_test("an empty frozen roster cannot fall back to the local node")
def test_empty_roster_fails_closed(opts):
    from mojo.apps.edge import asyncjobs
    from mojo.apps.edge.services import deploy, platform_deploy
    with mock.patch("mojo.apps.jobs.get_runners_bounded", return_value=[]):
        row, _ = platform_deploy.create(SHA, source="test")
    assert row.status == "failed"
    assert row.detail["reason"] == "roster_unavailable"
    deploy.set_target(SHA, deployment_id=row.pk)
    deploy.arm_status(SHA, deployment_id=row.pk)
    job = mock.Mock(payload={"deployment": str(row.pk)}, runner_id="local-engine")
    incident = mock.Mock(pk=1818)
    with mock.patch(
            "mojo.apps.incident.reporter.report_event", return_value=incident), \
         mock.patch("mojo.apps.jobs.publish") as publish:
        result = asyncjobs.deploy_orchestrate(job)
    assert result == f"failed:{SHA}"
    assert not publish.called
    row.refresh_from_db()
    assert row.status == "failed"


@th.django_unit_test("publish failure is durable and contains no provider detail")
def test_publish_failure_durable(opts):
    from mojo.apps.edge.models import PlatformDeployment
    from mojo.apps.edge.services import deploy
    with mock.patch("mojo.apps.jobs.get_runners_bounded", return_value=[{
            "runner_id": "edge-engine", "alive": True}]), \
         mock.patch("mojo.apps.jobs.publish", side_effect=RuntimeError("redis password=secret")):
        with th.assert_raises(RuntimeError):
            deploy.request_deploy(SHA, actor="test", source="test")
    row = PlatformDeployment.objects.latest("created")
    assert row.status == "failed"
    assert "secret" not in str(row.transitions).lower()
    assert row.transitions[-1]["detail"]["reason"] == "orchestrator_publish_failed"


@th.django_unit_test("runner roster failure is durable and fails closed")
def test_roster_failure_durable(opts):
    from mojo.apps.edge.models import PlatformDeployment
    from mojo.apps.edge.services import deploy
    with mock.patch(
            "mojo.apps.jobs.get_runners_bounded",
            side_effect=RuntimeError("password=roster-sentinel")):
        with th.assert_raises(deploy.DeploymentCoordinationError):
            deploy.request_deploy(SHA, actor="test", source="test")
    row = PlatformDeployment.objects.latest("created")
    assert row.status == "failed" and row.finished is not None
    assert row.detail == {"reason": "roster_unavailable"}
    assert "roster-sentinel" not in str(row.transitions)


@th.django_unit_test("provider and callback messages never persist as deploy detail")
def test_failure_detail_is_classified(opts):
    from mojo.apps.edge.services import deploy, platform_deploy
    row, _ = platform_deploy.create(SHA, source="test")
    deploy.arm_status(SHA, deployment_id=row.pk, force=True)
    sentinel = "password=sentinel-secret"
    assert deploy.set_status(
        deploy.STATUS_FAILED, SHA, detail=sentinel, deployment_id=row.pk)
    assert deploy.get_status()["detail"] == "update_failed"
    row.refresh_from_db()
    assert "sentinel-secret" not in str(row.transitions)


@th.django_unit_test("generic model REST cannot mutate deployment journal fields")
def test_journal_restmeta_is_immutable(opts):
    from mojo.apps.edge.models import PlatformDeployment
    writable = set(PlatformDeployment.RestMeta.NO_SAVE_FIELDS)
    required = {field.name for field in PlatformDeployment._meta.fields}
    assert required <= writable, f"journal fields escaped NO_SAVE_FIELDS: {required - writable}"
    assert PlatformDeployment.RestMeta.CAN_CREATE is False
    assert PlatformDeployment.RestMeta.CAN_UPDATE is False
    assert PlatformDeployment.RestMeta.CAN_DELETE is False


@th.django_unit_test("verification refuses proof for another deployment UUID")
def test_verify_uuid_mismatch(opts):
    from mojo.apps.edge.services import platform_deploy
    with mock.patch("mojo.apps.jobs.get_runners_bounded", return_value=[{
            "runner_id": "edge-a-engine", "alive": True}]):
        row, _ = platform_deploy.create(SHA, source="test")
    row.frozen_roster = ["edge-a-engine"]
    row.save(update_fields=["frozen_roster"])
    response = {"status": "success", "result": {
        "platform_sha": SHA,
        "platform_deployment": str(uuid.uuid4()),
    }}
    with mock.patch(
            "mojo.apps.jobs.manager.JobManager.execute_on_runner",
            return_value=response):
        result = platform_deploy.verify(row.pk)
    assert result.status == "unknown"
    assert result.node_evidence[0]["state"] == "unavailable"
