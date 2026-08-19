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


@th.django_unit_test("one proof matcher requires exact UUID and a full live SHA")
def test_shared_proof_matcher(opts):
    from mojo.apps.edge.services import platform_deploy

    row, _ = platform_deploy.create(SHA[:12], source="test")
    good = {"platform_deployment": str(row.pk), "platform_sha": SHA}
    th.assert_true(platform_deploy.proof_matches(row, good),
                   "a full live SHA matching the requested prefix must prove")
    for bad in (
            {**good, "platform_deployment": str(uuid.uuid4())},
            {**good, "platform_sha": SHA[:12]},
            {**good, "platform_sha": "9" * 40},
            {**good, "platform_sha": "not-a-sha"},
            None):
        th.assert_true(not platform_deploy.proof_matches(row, bad),
                       f"mismatched or partial proof was accepted: {bad!r}")


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


def _aged(pk, seconds):
    from datetime import timedelta
    from django.utils import timezone
    from mojo.apps.edge.models import PlatformDeployment
    PlatformDeployment.objects.filter(pk=pk).update(
        modified=timezone.now() - timedelta(seconds=seconds))


def _clean_deploy_state():
    from mojo.apps.edge.models import PlatformDeployment
    from mojo.apps.edge.services import deploy
    PlatformDeployment.objects.all().delete()
    deploy.get_client().delete(deploy.TARGET_KEY, deploy.STATUS_KEY)


def _attempt(status, roster=("edge-a-engine",)):
    from mojo.apps.edge.models import PlatformDeployment
    return PlatformDeployment.objects.create(
        sha=SHA, actor="test", source="test", request_key=str(uuid.uuid4()),
        frozen_roster=list(roster), status=status, transitions=[])


@th.django_unit_test("reconcile_stale closes a node-failed lease, spares live and targeted rows")
def test_reconcile_lease_ownership(opts):
    """The lease-owner skip used to be unconditional, so the one case that
    needed closing most — a node reported `failed` and the orchestrator that
    would have closed the row is exactly what died — was the one case
    reconciliation refused to touch. The row stayed active and the lease held
    the plane shut for its whole TTL."""
    from mojo.apps.edge.services import deploy, platform_deploy

    # 1. The lease is this row's and the node reported it failed. The row is
    #    NOT aged, so only the new branch can possibly close it.
    _clean_deploy_state()
    reported = _attempt("canary")
    deploy.arm_status(SHA, deployment_id=reported.pk)
    th.assert_true(
        deploy.set_status(deploy.STATUS_FAILED, SHA, deployment_id=reported.pk),
        "the node's failure report must land on its own lease")
    changed = platform_deploy.reconcile_stale()
    reported.refresh_from_db()
    th.assert_eq(reported.status, "failed",
                 f"a node-reported failure must close the attempt, got {reported.status}")
    th.assert_eq(reported.transitions[-1]["detail"]["reason"], "node_reported_failed",
                 f"the closure must name its cause, got {reported.transitions[-1]!r}")
    th.assert_true(changed >= 1, f"the closure must be counted, got {changed}")
    th.assert_eq(deploy.get_status(), None,
                 "the terminal lease must be released so the next push starts clean")

    # 2. A live (migrating) lease is hands-off even when the row is stale.
    _clean_deploy_state()
    live = _attempt("canary")
    deploy.arm_status(SHA, deployment_id=live.pk)
    _aged(live.pk, max(60, deploy.status_ttl()) + 60)
    platform_deploy.reconcile_stale()
    live.refresh_from_db()
    th.assert_eq(live.status, "canary",
                 f"a running deploy must not be closed out from under itself, "
                 f"got {live.status}")
    status = deploy.get_status()
    th.assert_true(status and status["deployment"] == str(live.pk),
                   f"a live lease must survive reconciliation, got {status!r}")

    # 3. The requested row named by the live target is the fleet's desired
    #    state, not an abandoned attempt — an unrelated stale row still closes.
    _clean_deploy_state()
    stranded = _attempt("requested")
    orphan = _attempt("requested")
    deploy.set_target(SHA, actor="test", deployment_id=stranded.pk)
    for row in (stranded, orphan):
        _aged(row.pk, max(60, deploy.status_ttl()) + 60)
    platform_deploy.reconcile_stale()
    stranded.refresh_from_db()
    orphan.refresh_from_db()
    th.assert_eq(stranded.status, "requested",
                 f"the live target's row must stay resumable, got {stranded.status}")
    th.assert_eq(orphan.status, "unknown",
                 f"a stale attempt nothing points at must still close, "
                 f"got {orphan.status}")
    _clean_deploy_state()


@th.django_unit_test("resume_stranded_target republishes exactly one wedged deploy")
def test_resume_stranded_target(opts):
    """The TTL wedge: something armed the lease and died before publishing an
    orchestrator. The target is recorded, the row says `requested`, and
    nothing was ever going to move it — the fleet sat on the old commit until
    a human pushed again."""
    from mojo.apps.edge.services import deploy

    # Nothing armed, target names a `requested` row: resume it.
    _clean_deploy_state()
    stranded = _attempt("requested")
    deploy.set_target(SHA, actor="test", deployment_id=stranded.pk)
    with mock.patch("mojo.apps.jobs.publish") as publish:
        resumed = deploy.resume_stranded_target()
    th.assert_eq(resumed, SHA, f"the stranded target must be resumed, got {resumed!r}")
    th.assert_eq(publish.call_count, 1,
                 f"exactly one orchestrate must be republished, got {publish.call_args_list!r}")
    th.assert_eq(publish.call_args.kwargs["func"], deploy.DEPLOY_ORCHESTRATE_JOB,
                 f"the republished job must be the orchestrator, got {publish.call_args!r}")
    th.assert_eq(publish.call_args.kwargs["payload"],
                 {"sha": SHA, "deployment": str(stranded.pk)},
                 f"the republish must carry the stranded attempt, got {publish.call_args!r}")
    th.assert_eq(publish.call_args.kwargs["max_retries"], 0,
                 "a redelivered orchestrator would double-drive the protocol")
    th.assert_true(publish.call_args.kwargs.get("expires_in"),
                   "the republished orchestrate must expire like any deploy job")
    status = deploy.get_status()
    th.assert_true(status and status["deployment"] == str(stranded.pk),
                   f"resuming must claim the lease atomically, got {status!r}")
    stranded.refresh_from_db()
    th.assert_eq(stranded.transitions[-1]["detail"]["reason"], "stranded_target",
                 f"the resume must be journalled, got {stranded.transitions[-1]!r}")

    # The claim is what stops a second sweep (or a second node) republishing.
    with mock.patch("mojo.apps.jobs.publish") as publish:
        th.assert_eq(deploy.resume_stranded_target(), None,
                     "a resumed deploy must not be resumed again while it runs")
    th.assert_eq(publish.call_count, 0,
                 f"the live lease must stop a duplicate republish, got {publish.call_args_list!r}")

    # A row that is already past `requested` is not stranded, it is running.
    _clean_deploy_state()
    running = _attempt("canary")
    deploy.set_target(SHA, actor="test", deployment_id=running.pk)
    with mock.patch("mojo.apps.jobs.publish") as publish:
        th.assert_eq(deploy.resume_stranded_target(), None,
                     "an attempt past `requested` is in flight, not stranded")
    th.assert_eq(publish.call_count, 0, "a canary-stage attempt must not be republished")

    # Neither is a terminal one.
    _clean_deploy_state()
    done = _attempt("failed")
    deploy.set_target(SHA, actor="test", deployment_id=done.pk)
    with mock.patch("mojo.apps.jobs.publish") as publish:
        th.assert_eq(deploy.resume_stranded_target(), None,
                     "a terminal attempt must never be republished")
    th.assert_eq(publish.call_count, 0, "a closed attempt must not be republished")
    _clean_deploy_state()


@th.django_unit_test(
    "post-restart finalizer clears exact terminal owners and resumes one successor")
def test_post_restart_finalizer_and_successor_resume(opts):
    from mojo.apps.edge.models import PlatformDeployment
    from mojo.apps.edge.services import deploy, platform_deploy, readiness

    _clean_deploy_state()
    runner = deploy.local_runner_id()
    finished = _attempt(PlatformDeployment.STATUS_VERIFIED, roster=(runner,))
    platform_deploy.evidence(
        finished.pk, runner, deploy.STATUS_DEPLOYING,
        proof={"platform_deployment": str(finished.pk), "platform_sha": SHA})
    deploy.arm_status(SHA, deployment_id=finished.pk)
    deploy.set_status(deploy.STATUS_DEPLOYING, SHA, deployment_id=finished.pk)

    successor = PlatformDeployment.objects.create(
        sha="9" * 40, actor="test", source="test", request_key=str(uuid.uuid4()),
        frozen_roster=[runner], status=PlatformDeployment.STATUS_REQUESTED,
        transitions=[])
    deploy.set_target(successor.sha, actor="next", deployment_id=successor.pk)
    proof = {"platform_deployment": str(finished.pk), "platform_sha": SHA}
    with mock.patch.object(readiness, "local_node_proof", return_value=proof), \
         mock.patch("mojo.apps.jobs.publish") as publish:
        result = platform_deploy.finalize_post_restart()
        again = platform_deploy.finalize_post_restart()

    th.assert_eq(result, str(finished.pk),
                 f"the exact terminal owner was not finalized: {result!r}")
    th.assert_eq(again, None,
                 f"a second startup finalized or resumed twice: {again!r}")
    finished.refresh_from_db()
    th.assert_eq(finished.status, PlatformDeployment.STATUS_CONVERGED,
                 f"matching restarted proof did not converge: {finished.status}")
    status = deploy.get_status()
    th.assert_eq((status or {}).get("deployment"), str(successor.pk),
                 f"the successor was not armed after exact cleanup: {status!r}")
    th.assert_eq(publish.call_count, 1,
                 f"the successor must publish exactly once: {publish.call_args_list!r}")
    th.assert_eq(publish.call_args.kwargs["payload"],
                 {"sha": successor.sha, "deployment": str(successor.pk)},
                 f"the successor publish carried the wrong attempt: {publish.call_args!r}")
    _clean_deploy_state()


@th.django_unit_test("post-restart finalizer closes delayed v1 identity proof")
def test_post_restart_finalizer_v1_bridge(opts):
    from mojo.apps.edge.models import PlatformDeployment
    from mojo.apps.edge.services import deploy, platform_deploy, readiness

    _clean_deploy_state()
    runner = deploy.local_runner_id()
    bridged = _attempt(PlatformDeployment.STATUS_FLEET, roster=(runner,))
    platform_deploy.evidence(
        bridged.pk, runner, "identity_pending", proof={},
        detail={"reason": "legacy_script_identity_order"})
    deploy.arm_status(SHA, deployment_id=bridged.pk)
    deploy.set_status(deploy.STATUS_DEPLOYING, SHA, deployment_id=bridged.pk)
    proof = {"platform_deployment": str(bridged.pk), "platform_sha": SHA}

    with mock.patch.object(readiness, "local_node_proof", return_value=proof):
        result = platform_deploy.finalize_post_restart()

    th.assert_eq(result, str(bridged.pk),
                 f"the predecessor bridge was not finalized: {result!r}")
    bridged.refresh_from_db()
    th.assert_eq(bridged.status, PlatformDeployment.STATUS_CONVERGED,
                 f"delayed v1 proof did not converge: {bridged.status}")
    th.assert_eq(deploy.get_status(), None,
                 "the completed v1 bridge retained its terminal lease")
    _clean_deploy_state()


@th.django_unit_test(
    "post-restart finalizer cleans failed/missing owners but preserves live and legacy leases")
def test_post_restart_finalizer_ownership_guards(opts):
    from mojo.apps.edge.models import PlatformDeployment
    from mojo.apps.edge.services import deploy, platform_deploy

    runner = deploy.local_runner_id()

    _clean_deploy_state()
    failed = _attempt(PlatformDeployment.STATUS_FAILED, roster=(runner,))
    platform_deploy.evidence(failed.pk, runner, "failed", detail={"phase": "rollback"})
    deploy.arm_status(SHA, deployment_id=failed.pk)
    deploy.set_status(deploy.STATUS_FAILED, SHA, deployment_id=failed.pk)
    th.assert_eq(platform_deploy.finalize_post_restart(), str(failed.pk),
                 "the failed row outside ACTIVE_STATUSES retained its lease")
    th.assert_eq(deploy.get_status(), None,
                 "the exact failed owner was not cleared")

    missing = str(uuid.uuid4())
    deploy.arm_status(SHA, deployment_id=missing)
    deploy.set_status(deploy.STATUS_FAILED, SHA, deployment_id=missing)
    th.assert_eq(platform_deploy.finalize_post_restart(), missing,
                 "a terminal valid-UUID lease with no durable owner was not cleaned")
    th.assert_eq(deploy.get_status(), None,
                 "the missing terminal owner retained the lease")

    live = _attempt(PlatformDeployment.STATUS_CANARY, roster=(runner,))
    deploy.arm_status(SHA, deployment_id=live.pk)
    th.assert_eq(platform_deploy.finalize_post_restart(), None,
                 "a live migrating owner was stolen")
    th.assert_eq((deploy.get_status() or {}).get("deployment"), str(live.pk),
                 "the live owner lost its lease")
    deploy.clear_status(live.pk)

    foreign = _attempt(PlatformDeployment.STATUS_VERIFIED,
                       roster=("another-runner",))
    platform_deploy.evidence(
        foreign.pk, "another-runner", deploy.STATUS_DEPLOYING,
        proof={"platform_deployment": str(foreign.pk), "platform_sha": SHA})
    deploy.arm_status(SHA, deployment_id=foreign.pk)
    deploy.set_status(deploy.STATUS_DEPLOYING, SHA, deployment_id=foreign.pk)
    th.assert_eq(platform_deploy.finalize_post_restart(), None,
                 "this node finalized another runner's terminal owner")
    th.assert_eq((deploy.get_status() or {}).get("deployment"), str(foreign.pk),
                 "a foreign exact-UUID owner lost its lease")
    deploy.clear_status(foreign.pk)

    deploy.arm_status(SHA)
    deploy.set_status(deploy.STATUS_FAILED, SHA)
    th.assert_eq(platform_deploy.finalize_post_restart(), None,
                 "a legacy unowned lease must remain TTL-bound")
    th.assert_eq((deploy.get_status() or {}).get("deployment"), "",
                 "the legacy lease was guessed away")
    deploy.get_client().delete(deploy.STATUS_KEY)
    _clean_deploy_state()


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


def _tail_row():
    """One attempt carrying a stderr tail in its evidence detail."""
    from mojo.apps.edge.models import PlatformDeployment
    _clean_deploy_state()
    row = PlatformDeployment.objects.create(
        sha=SHA, actor="test", source="test", request_key=str(uuid.uuid4()),
        frozen_roster=["edge-a-engine"], status="failed", transitions=[],
        node_evidence=[{
            "runner": "edge-a-engine", "state": "failed",
            "detail": {"phase": "update_script", "exit": 23, "stderr_tail": [
                "psql: postgres://deploy:hunter2@db.internal/app",
                "ERROR: relation already exists"]}}])
    return row


@th.django_unit_test("serialize withholds the stderr tail and never eats the stored copy")
def test_stderr_tail_withheld_by_default(opts):
    """The tail is redacted per line, but the redactor has gaps a credential
    survives, so it belongs to the security tier — while everything else in
    node_evidence stays readable at view_platform."""
    from mojo.apps.edge.models import PlatformDeployment
    from mojo.apps.edge.services import platform_deploy

    row = _tail_row()
    public = platform_deploy.serialize(row)
    assert "stderr_tail" not in str(public), \
        "the default serialization must not carry the stderr tail anywhere"
    detail = public["node_evidence"][0]["detail"]
    th.assert_eq(detail["phase"], "update_script",
                 "the benign evidence detail must survive the strip")
    th.assert_eq(detail["exit"], 23, "the exit code must survive the strip")
    th.assert_eq(public["node_evidence"][0]["runner"], "edge-a-engine",
                 "the runner identity must survive the strip")

    stored = PlatformDeployment.objects.get(pk=row.pk)
    assert "hunter2" in str(stored.node_evidence), \
        "serializing must copy, never strip the durable row itself"


@th.django_unit_test("the privileged serialization keeps the stderr tail verbatim")
def test_stderr_tail_included_when_privileged(opts):
    from mojo.apps.edge.services import platform_deploy

    row = _tail_row()
    privileged = platform_deploy.serialize(row, include_stderr=True)
    tail = privileged["node_evidence"][0]["detail"]["stderr_tail"]
    th.assert_eq(len(tail), 2,
                 "deploy authority must still read the whole diagnostic tail")
    assert "ERROR: relation already exists" in tail[1], \
        "the privileged tail must be verbatim, not reshaped"


@th.django_unit_test("the admin graph carries raw evidence; the boundary gates it")
def test_admin_graph_evidence_gated_at_boundary(opts):
    """Item 2102 inverted this. The serializer is request-free by design, so
    to_dict(graph="admin") now returns the RAW node_evidence (stderr tail
    included). Access is gated at the REST boundary and the assistant tools by
    RestMeta.GRAPH_PERMISSIONS (see tests/test_models/graph_permissions.py) —
    not by hiding the field from the serializer, which had no way to let a
    privileged reader (superuser included) opt back in."""
    row = _tail_row()

    admin = row.to_dict(graph="admin")
    tail = admin["node_evidence"][0]["detail"]["stderr_tail"]
    th.assert_eq(len(tail), 2,
                 "the admin graph now serves the raw diagnostic tail")
    assert "ERROR: relation already exists" in tail[1], \
        "the admin tail must be verbatim, not reshaped"
    th.assert_eq(admin["node_evidence"][0]["detail"]["phase"], "update_script",
                 "the admin graph keeps the benign evidence it always showed")

    # The evidence-free graphs never carry node_evidence.
    basic = row.to_dict(graph="basic")
    assert "node_evidence" not in basic, \
        "the basic graph never carried evidence and must not start"
    default = row.to_dict(graph="default")
    assert "node_evidence" not in default, \
        "the default graph is evidence-free"

    # An unmapped name resolves to the evidence-free default (the model now
    # defines one), so no ungated serialization path carries the tail.
    for graph in ("list", "unmapped-name"):
        rendered = row.to_dict(graph=graph)
        assert "stderr_tail" not in str(rendered), \
            f"graph={graph} must fall back to the evidence-free default"


@th.django_unit_test("same_sha_retry returns the deployment row, not create()'s tuple")
def test_same_sha_retry_returns_a_row(opts):
    from mojo.apps.edge.models import PlatformDeployment
    from mojo.apps.edge.services import platform_deploy
    original, _ = platform_deploy.create(
        SHA, source="test", idempotency_key="retry-origin")
    retried = platform_deploy.same_sha_retry(
        original, actor="framework-update:test", idempotency_key="retry-again")
    assert isinstance(retried, PlatformDeployment), \
        (f"same_sha_retry returned {type(retried).__name__}, not a "
         "PlatformDeployment — the framework-update endpoint reads .pk off "
         "this and 500s on a tuple")
    assert retried.pk != original.pk, "the retry did not create a new attempt"
    assert retried.retry_of_id == original.pk, \
        "the retry does not reference the row it retried"
