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
    with mock.patch("mojo.apps.jobs.get_runners", return_value=rows) as get_runners:
        assert platform_deploy.edge_roster() == ["edge-a-engine", "edge-b-engine"]
    get_runners.assert_called_once_with(channel="edge")


@th.django_unit_test("an empty frozen roster cannot fall back to the local node")
def test_empty_roster_fails_closed(opts):
    from mojo.apps.edge import asyncjobs
    from mojo.apps.edge.services import deploy, platform_deploy
    with mock.patch("mojo.apps.jobs.get_runners", return_value=[]):
        row, _ = platform_deploy.create(SHA, source="test")
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
    with mock.patch("mojo.apps.jobs.get_runners", return_value=[]), \
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
            "mojo.apps.jobs.get_runners",
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
