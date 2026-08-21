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


def _diagnosis_row(**extra):
    """One failed single-runner attempt carrying a tailed failure diagnosis."""
    from mojo.apps.edge.models import PlatformDeployment
    _clean_deploy_state()
    return PlatformDeployment.objects.create(
        sha=SHA, actor="test", source="test", request_key=str(uuid.uuid4()),
        frozen_roster=["edge-a-engine"], status="failed", transitions=[],
        diagnosis=[{
            "runner": "edge-a-engine", "at": "2026-08-19T00:00:00+00:00",
            "kind": "failure", "proof": {},
            "detail": {"phase": "update_script", "exit": 23, "stderr_tail": [
                "psql: postgres://deploy:hunter2@db.internal/app",
                "ERROR: relation already exists"]}}],
        **extra)


@th.django_unit_test("serialize gates the diagnosis stderr tail like node_evidence")
def test_serialize_gates_diagnosis_stderr(opts):
    from mojo.apps.edge.models import PlatformDeployment
    from mojo.apps.edge.services import platform_deploy

    row = _diagnosis_row()
    public = platform_deploy.serialize(row)
    assert "stderr_tail" not in str(public["diagnosis"]), \
        "the default serialization must strip the diagnosis stderr tail"
    detail = public["diagnosis"][0]["detail"]
    th.assert_eq(detail["phase"], "update_script",
                 "the benign diagnosis detail must survive the strip")
    th.assert_eq(detail["exit"], 23, "the exit code must survive the strip")

    privileged = platform_deploy.serialize(row, include_stderr=True)
    tail = privileged["diagnosis"][0]["detail"]["stderr_tail"]
    th.assert_eq(len(tail), 2,
                 "deploy authority must read the whole diagnosis tail")
    assert "ERROR: relation already exists" in tail[1], \
        "the privileged diagnosis tail must be verbatim, not reshaped"

    stored = PlatformDeployment.objects.get(pk=row.pk)
    assert "hunter2" in str(stored.diagnosis), \
        "serializing must copy, never strip the durable diagnosis itself"

    # Graph boundary: only the permission-gated admin graph carries diagnosis.
    assert "diagnosis" in row.to_dict(graph="admin"), \
        "the admin graph must carry the diagnosis journal"
    for graph in ("basic", "default", "unmapped-name"):
        assert "diagnosis" not in row.to_dict(graph=graph), \
            f"graph={graph} must stay evidence-free"
    _clean_deploy_state()


@th.django_unit_test("diagnosis entries are bounded per kind, first-wins, and deduped")
def test_diagnosis_bounds_and_dedupe(opts):
    from mojo.apps.edge.services import platform_deploy

    _clean_deploy_state()
    row = _attempt("failed", roster=("edge-a-engine",))
    th.assert_true(
        not platform_deploy.record_diagnosis(
            row.pk, "not-in-roster", detail={"phase": "update_script"}),
        "a runner outside the frozen roster must be refused")
    for index in range(20):
        platform_deploy.record_diagnosis(
            row.pk, "edge-a-engine", detail={"phase": f"phase_{index}"})
    row.refresh_from_db()
    failures = [item for item in row.diagnosis if item["kind"] == "failure"]
    th.assert_eq(len(failures), 16,
                 f"the failure budget must cap at 16, got {len(failures)}")
    th.assert_eq(failures[0]["detail"]["phase"], "phase_0",
                 "the FIRST entries are the story and must never be evicted")
    th.assert_true(
        not platform_deploy.record_diagnosis(
            row.pk, "edge-a-engine", detail={"phase": "phase_1"}),
        "a duplicate (kind, runner, phase) must be refused")
    th.assert_true(
        platform_deploy.record_diagnosis(
            row.pk, "edge-a-engine", detail={"phase": "rolled_back"},
            proof={"platform_sha": "9" * 40}, kind="outcome"),
        "an outcome must land even when the failure budget is spent")
    row.refresh_from_db()
    outcomes = [item for item in row.diagnosis if item["kind"] == "outcome"]
    th.assert_eq(len(outcomes), 1,
                 f"exactly one outcome entry must exist, got {outcomes!r}")
    _clean_deploy_state()


@th.django_unit_test("record_rollback_outcomes records only a proven foreign identity")
def test_record_rollback_outcomes(opts):
    from mojo.apps.edge.services import deploy, platform_deploy, readiness

    _clean_deploy_state()
    runner = deploy.local_runner_id()
    row = _attempt("failed", roster=(runner,))
    th.assert_true(
        platform_deploy.record_diagnosis(
            row.pk, runner, detail={"phase": "post_deploy_migrate"}),
        "the fixture's failure entry must land")
    th.assert_eq(deploy.get_status(), None,
                 "the sweep must run with no coordination lease present")

    # (a) an absent, empty, or own identity records nothing.
    for proof in (
            {"platform_sha": "", "platform_deployment": ""},
            {"platform_sha": "not-a-sha", "platform_deployment": str(uuid.uuid4())},
            {"platform_sha": SHA, "platform_deployment": str(row.pk)}):
        with mock.patch.object(readiness, "local_node_proof", return_value=proof):
            th.assert_eq(platform_deploy.record_rollback_outcomes(), 0,
                         f"an unproven identity must record nothing: {proof!r}")
    with mock.patch.object(readiness, "local_node_proof",
                           side_effect=RuntimeError("proof offline")):
        th.assert_eq(platform_deploy.record_rollback_outcomes(), 0,
                     "an unavailable proof must record nothing")
    row.refresh_from_db()
    th.assert_eq([item["kind"] for item in row.diagnosis], ["failure"],
                 f"an unproven sweep wrote an outcome: {row.diagnosis!r}")

    # (b) a valid FOREIGN identity records the outcome once, idempotently.
    foreign = {"platform_sha": "9" * 40,
               "platform_deployment": str(uuid.uuid4()), "node_id": "test"}
    with mock.patch.object(readiness, "local_node_proof", return_value=foreign):
        th.assert_eq(platform_deploy.record_rollback_outcomes(), 1,
                     "a proven foreign identity must record one outcome")
        th.assert_eq(platform_deploy.record_rollback_outcomes(), 0,
                     "a repeated sweep must be a no-op")
    row.refresh_from_db()
    th.assert_eq([item["kind"] for item in row.diagnosis],
                 ["failure", "outcome"],
                 f"the outcome must append after the failure: {row.diagnosis!r}")
    th.assert_eq(row.diagnosis[0]["detail"]["phase"], "post_deploy_migrate",
                 "the original failure entry must be untouched")
    outcome = row.diagnosis[-1]
    th.assert_eq(outcome["detail"]["phase"], "rolled_back",
                 f"the outcome must say the node rolled back: {outcome!r}")
    th.assert_eq(outcome["proof"]["platform_sha"], "9" * 40,
                 f"the outcome must carry what the node now runs: {outcome!r}")
    observed = {item["runner"]: item for item in row.node_evidence}
    th.assert_eq(observed[runner]["state"], "rolled_back",
                 f"the live observation must be recorded too: {row.node_evidence!r}")
    th.assert_eq(observed[runner]["detail"]["reason"], "post_restart_observation",
                 f"the observation must name its source: {observed[runner]!r}")

    # (c) a multi-node roster is skipped — this node's identity says nothing
    # about the canary that failed.
    _clean_deploy_state()
    fleet_row = _attempt("failed", roster=(runner, "edge-b-engine"))
    fleet_row.diagnosis = [{
        "runner": runner, "at": "2026-08-19T00:00:00+00:00", "kind": "failure",
        "detail": {"phase": "update_script"}, "proof": {}}]
    fleet_row.save(update_fields=["diagnosis"])
    with mock.patch.object(readiness, "local_node_proof", return_value=foreign):
        th.assert_eq(platform_deploy.record_rollback_outcomes(), 0,
                     "a multi-node roster must never receive a sweep outcome")
    _clean_deploy_state()


@th.django_unit_test("node_summary buckets partition the observed evidence")
def test_node_summary_buckets(opts):
    from mojo.apps.edge.services import platform_deploy

    _clean_deploy_state()
    row = _attempt("canary", roster=("edge-a-engine", "edge-b-engine"))
    platform_deploy.evidence(row.pk, "edge-a-engine", "dispatched",
                             detail={"migrate": True, "job": "1"})
    row.refresh_from_db()
    summary = platform_deploy.serialize(row)["node_summary"]
    th.assert_eq(summary["expected"], 2,
                 f"expected must be the frozen roster size: {summary!r}")
    th.assert_eq(summary["dispatched"], 1,
                 f"a dispatched node must count as dispatched: {summary!r}")

    # A post-verify `unavailable` observation lands in `other`, never in
    # `failed` — the failure story lives in the diagnosis.
    platform_deploy.evidence(row.pk, "edge-a-engine", "unavailable",
                             detail={"reason": "proof_unavailable"})
    platform_deploy.evidence(row.pk, "edge-b-engine", "deploying")
    row.refresh_from_db()
    summary = platform_deploy.serialize(row)["node_summary"]
    th.assert_eq(summary["other"], 1,
                 f"unavailable must bucket as other: {summary!r}")
    th.assert_eq(summary["reported"], 1,
                 f"deploying must bucket as reported: {summary!r}")
    th.assert_eq(summary["failed"], 0,
                 f"nothing here is a failure observation: {summary!r}")
    total = sum(summary[key] for key in
                ("proven", "reported", "failed", "dispatched", "other"))
    th.assert_eq(total, len(row.node_evidence),
                 f"buckets must partition the observed entries: {summary!r}")

    _clean_deploy_state()
    single = _attempt("converged", roster=("edge-a-engine",))
    platform_deploy.evidence(
        single.pk, "edge-a-engine", "proven",
        proof={"platform_deployment": str(single.pk), "platform_sha": SHA})
    single.refresh_from_db()
    summary = platform_deploy.serialize(single)["node_summary"]
    th.assert_eq((summary["proven"], summary["expected"]), (1, 1),
                 f"a converged single runner must read 1 of 1: {summary!r}")
    _clean_deploy_state()


# ---------------------------------------------------------------------------
# the node job's handoff — closing a row whose engine is about to be killed
# ---------------------------------------------------------------------------


_SAME_AS_CHANNEL = object()


def _node_job(deployment, channel=None, status="running", runner_id=None,
              payload_runner=_SAME_AS_CHANNEL):
    """A `deploy_node` job as the orchestrator publishes it, plus the in-flight
    ZSET entry the engine would normally remove when the job ends.

    `channel` defaults to this box's runner id — that is what a row belonging
    to this node looks like — and `runner_id` defaults to the channel, since
    the orchestrator publishes to the target runner's own channel and the
    engine that claims the row stamps its own id. All three of channel,
    `runner_id` and `payload["runner"]` are separable so each matcher can be
    exercised alone; `payload_runner=None` publishes a row from BEFORE the
    payload carried a runner, which is what the compatibility match exists
    for."""
    from mojo.apps.jobs.adapters import get_adapter
    from mojo.apps.jobs.keys import JobKeys
    from mojo.apps.jobs.models import Job
    from mojo.apps.edge.services import deploy

    if channel is None:
        channel = deploy.local_runner_id()
    payload = {"sha": SHA, "deployment": str(deployment)}
    if payload_runner is _SAME_AS_CHANNEL:
        payload_runner = channel
    if payload_runner is not None:
        payload["runner"] = payload_runner
    job = Job.objects.create(
        id=uuid.uuid4().hex, channel=channel, func=deploy.DEPLOY_NODE_JOB,
        status=status, runner_id=channel if runner_id is None else runner_id,
        payload=payload)
    get_adapter().zadd(JobKeys().processing(channel), {job.id: 1})
    return job


def _inflight(channel, job_id):
    from mojo.apps.jobs.adapters import get_adapter
    from mojo.apps.jobs.keys import JobKeys

    return job_id in (get_adapter().zrangebyscore(
        JobKeys().processing(channel), float("-inf"), float("inf")) or [])


def _clean_node_jobs():
    from mojo.apps.jobs.models import Job, JobEvent
    from mojo.apps.edge.services import deploy

    JobEvent.objects.filter(job__func=deploy.DEPLOY_NODE_JOB).delete()
    Job.objects.filter(func=deploy.DEPLOY_NODE_JOB).delete()


@th.django_unit_test(
    "close_handoff_job closes this node's row, matched on payload['runner']")
def test_close_handoff_job_closes_this_nodes_row(opts):
    """The single-node case this whole mechanism exists for, and the matcher
    it uses to find the row: `payload["runner"]`, the node the orchestrator
    PUBLISHED for. `runner_id` is stamped by whichever engine claimed the row
    and the channel is routing; the payload is published intent, and nothing
    rewrites it."""
    from mojo.apps.jobs.models import JobEvent
    from mojo.apps.edge.services import deploy, platform_deploy

    _clean_deploy_state()
    _clean_node_jobs()
    row = _attempt("canary")
    me = deploy.local_runner_id()
    # Neither column names this box — only the payload does.
    job = _node_job(row.pk, channel="edge-published-elsewhere",
                    runner_id="some-other-engine", payload_runner=me)

    closed = platform_deploy.close_handoff_job(row.pk, "completed")

    th.assert_eq(closed, 1,
                 f"the row published for this node is this node's row, got "
                 f"{closed}")
    job.refresh_from_db()
    th.assert_eq(job.status, "completed",
                 f"the handoff writes the terminal status the node reported, "
                 f"got {job.status!r}")
    th.assert_true(job.finished_at is not None,
                   "a closed job must be stamped finished")
    th.assert_eq((job.metadata or {}).get("handoff"), True,
                 f"the row must say it was closed by handoff, not by its own "
                 f"engine, got {job.metadata!r}")
    event = JobEvent.objects.filter(job=job).first()
    th.assert_true(event is not None, "the closure must leave an event trail")
    th.assert_eq(event.details.get("reason"), "deploy_engine_handoff",
                 f"the event must name the handoff, got {event.details!r}")
    th.assert_true(
        not _inflight("edge-published-elsewhere", job.id),
        "the in-flight lease must be released on the job's OWN channel — "
        "otherwise the next engine's reaper is the only thing that ever "
        "clears it, a visibility timeout later")
    _clean_node_jobs()
    _clean_deploy_state()


@th.django_unit_test(
    "close_handoff_job still closes a row published before payload['runner']")
def test_close_handoff_job_compat_match(opts):
    """One deploy's worth of rows exist without the payload stamp: the deploy
    that ships the stamp published its own `deploy_node` jobs from the OLD
    framework. Those still close, on `runner_id`/`channel` — and this match is
    node-scoped too, which is the whole point."""
    from mojo.apps.edge.services import deploy, platform_deploy

    _clean_deploy_state()
    _clean_node_jobs()
    row = _attempt("canary")
    me = deploy.local_runner_id()
    mine = _node_job(row.pk, channel=me, payload_runner=None)
    peer = _node_job(row.pk, channel="edge-peer-engine", payload_runner=None)

    closed = platform_deploy.close_handoff_job(row.pk, "completed")

    th.assert_eq(closed, 1,
                 f"an unstamped row on this node's channel must still close, "
                 f"got {closed}")
    mine.refresh_from_db()
    th.assert_eq(mine.status, "completed",
                 f"the compatibility match closes the row, got {mine.status!r}")
    peer.refresh_from_db()
    th.assert_eq(peer.status, "running",
                 f"the compatibility match is node-scoped as well — an "
                 f"unstamped PEER is still a peer, got {peer.status!r}")
    th.assert_true(_inflight("edge-peer-engine", peer.id),
                   "the peer's in-flight lease must survive the compat match")
    _clean_node_jobs()
    _clean_deploy_state()


@th.django_unit_test(
    "a second close_handoff_job call closes nothing, never the fleet's rows")
def test_close_handoff_job_second_call_closes_nothing(opts):
    """`close_handoff_job` runs TWICE per node per deploy: `deploy_status set`
    closes the row (leaving it `completed`, so it drops out of the candidate
    set), and then update.sh's restart tail runs `deploy_status handoff`,
    which cannot know the first call happened.

    A fallback that widens its match on "no rows found" therefore fires on
    that second call — at which point the only rows still `running` on this
    deployment are the PEERS. That is the original bug, reached by a different
    door, and on a same-release fleet re-deploy every node does it at once."""
    from mojo.apps.jobs.models import JobEvent
    from mojo.apps.edge.services import deploy, platform_deploy

    _clean_deploy_state()
    _clean_node_jobs()
    row = _attempt("canary")
    me = deploy.local_runner_id()
    mine = _node_job(row.pk, channel=me)
    peers = [_node_job(row.pk, channel=f"edge-peer-{index}-engine")
             for index in range(3)]

    first = platform_deploy.close_handoff_job(row.pk, "completed")
    second = platform_deploy.close_handoff_job(row.pk, "completed")

    th.assert_eq(first, 1,
                 f"the first call closes this node's own row, got {first}")
    th.assert_eq(second, 0,
                 f"this node's row is already closed and every other running "
                 f"row on this deployment belongs to a peer — the second call "
                 f"must close NOTHING, got {second}")
    mine.refresh_from_db()
    th.assert_eq(mine.status, "completed",
                 f"the row closed by the first call keeps its outcome, got "
                 f"{mine.status!r}")
    for peer in peers:
        peer.refresh_from_db()
        th.assert_eq(peer.status, "running",
                     f"{peer.channel} is still deploying and must stay "
                     f"`running` — a peer recorded terminal here is a peer "
                     f"whose hang nobody will ever notice, got "
                     f"{peer.status!r}")
        th.assert_eq(JobEvent.objects.filter(job=peer).count(), 0,
                     f"{peer.channel} collects no handoff event from another "
                     f"node's restart tail")
        th.assert_true(_inflight(peer.channel, peer.id),
                       f"{peer.channel}'s in-flight lease must survive — its "
                       f"expiry is the reaper's only evidence of an abandoned "
                       f"node deploy")
    _clean_node_jobs()
    _clean_deploy_state()


@th.django_unit_test(
    "close_handoff_job closes THIS node's row, never a fleet peer's")
def test_close_handoff_job_is_scoped_to_this_node(opts):
    """One fleet deploy publishes ONE deployment UUID to EVERY node, and every
    node reaches this call at the end of its own update.sh.

    Matching on the deployment alone therefore let whichever node finished
    first record its still-running PEERS `completed` and drop their in-flight
    leases — and lease expiry is the only thing that ever detects a node deploy
    that hung or never came back. On the failure path the same match rewrote
    healthy peers to `failed`."""
    from mojo.apps.jobs.models import JobEvent
    from mojo.apps.edge.services import deploy, platform_deploy

    _clean_deploy_state()
    _clean_node_jobs()
    row = _attempt("canary")
    me = deploy.local_runner_id()
    mine = _node_job(row.pk, channel=me)
    peer = _node_job(row.pk, channel="edge-peer-engine")
    third = _node_job(row.pk, channel="edge-third-engine")

    closed = platform_deploy.close_handoff_job(row.pk, "failed")

    th.assert_eq(closed, 1,
                 f"exactly this node's row is closed, not one per fleet node, "
                 f"got {closed}")
    mine.refresh_from_db()
    th.assert_eq(mine.status, "failed",
                 f"this node's own row takes the outcome it reported, got "
                 f"{mine.status!r}")
    th.assert_true(not _inflight(me, mine.id),
                   "this node's in-flight lease is released")
    for other, label in ((peer, "edge-peer-engine"),
                         (third, "edge-third-engine")):
        other.refresh_from_db()
        th.assert_eq(other.status, "running",
                     f"{label} is still deploying and must stay `running` — "
                     f"a peer recorded terminal here is a peer whose hang "
                     f"nobody will ever notice, got {other.status!r}")
        th.assert_eq(JobEvent.objects.filter(job=other).count(), 0,
                     f"{label} collects no handoff event from another node")
        th.assert_true(_inflight(label, other.id),
                       f"{label}'s in-flight lease must survive — its expiry "
                       f"is the reaper's only evidence of an abandoned node "
                       f"deploy")
    _clean_node_jobs()
    _clean_deploy_state()


@th.django_unit_test(
    "close_handoff_job matches this node on runner_id as well as on channel")
def test_close_handoff_job_scopes_on_runner_id(opts):
    """The compatibility match reads two columns, not one. `runner_id` is
    stamped by the engine that claimed the row; `channel` is what the
    orchestrator published to. Either one naming this box, on a row with no
    payload stamp, is this box's row — and neither alone may be trusted to be
    populated."""
    from mojo.apps.edge.services import deploy, platform_deploy

    _clean_deploy_state()
    _clean_node_jobs()
    row = _attempt("canary")
    me = deploy.local_runner_id()
    mine = _node_job(row.pk, channel="edge-published-elsewhere", runner_id=me,
                     payload_runner=None)
    peer = _node_job(row.pk, channel="edge-peer-engine")

    closed = platform_deploy.close_handoff_job(row.pk, "completed")

    th.assert_eq(closed, 1,
                 f"the row this engine claimed is this node's row, got "
                 f"{closed}")
    mine.refresh_from_db()
    th.assert_eq(mine.status, "completed",
                 f"a runner_id match closes the row, got {mine.status!r}")
    peer.refresh_from_db()
    th.assert_eq(peer.status, "running",
                 f"the peer is untouched, got {peer.status!r}")
    _clean_node_jobs()
    _clean_deploy_state()


@th.django_unit_test("close_handoff_job normalizes an uppercase deployment UUID")
def test_close_handoff_job_normalizes_uuid(opts):
    """update.sh validates --deployment against a case-insensitive hex pattern,
    while the payload carries `str(uuid.UUID(...))`, which is lowercase."""
    from mojo.apps.edge.services import platform_deploy

    _clean_deploy_state()
    _clean_node_jobs()
    row = _attempt("canary")
    job = _node_job(row.pk)

    closed = platform_deploy.close_handoff_job(str(row.pk).upper(), "failed")

    th.assert_eq(closed, 1,
                 f"an uppercase UUID must normalize to the stored payload "
                 f"value, got {closed}")
    job.refresh_from_db()
    th.assert_eq(job.status, "failed",
                 f"a failed deploy closes its node job failed, got "
                 f"{job.status!r}")
    _clean_node_jobs()
    _clean_deploy_state()


@th.django_unit_test("close_handoff_job leaves already-terminal and foreign rows alone")
def test_close_handoff_job_is_narrow(opts):
    from mojo.apps.jobs.models import JobEvent
    from mojo.apps.edge.services import deploy, platform_deploy

    _clean_deploy_state()
    _clean_node_jobs()
    row = _attempt("canary")
    other = _attempt("canary")
    # This node's OWN row, already terminal: the status filter, not the node
    # scoping, is what has to refuse it.
    done = _node_job(row.pk, status="completed")
    foreign = _node_job(other.pk, channel=deploy.local_runner_id())

    closed = platform_deploy.close_handoff_job(row.pk, "failed")

    th.assert_eq(closed, 0,
                 f"a job that already reported its own outcome must not be "
                 f"rewritten, got {closed}")
    done.refresh_from_db()
    th.assert_eq(done.status, "completed",
                 f"the job's own outcome stands, got {done.status!r}")
    th.assert_eq(JobEvent.objects.filter(job=done).count(), 0,
                 "an untouched job collects no handoff event")
    foreign.refresh_from_db()
    th.assert_eq(foreign.status, "running",
                 f"another deployment's node job must not be touched, even on "
                 f"this node's own channel, got {foreign.status!r}")
    th.assert_true(_inflight(foreign.channel, foreign.id),
                   "another deployment's in-flight lease must be left alone")

    th.assert_eq(platform_deploy.close_handoff_job(None, "completed"), 0,
                 "an unusable deployment id closes nothing")
    th.assert_eq(platform_deploy.close_handoff_job(row.pk, "canceled"), 0,
                 "only the two terminal states this reports are accepted")
    _clean_node_jobs()
    _clean_deploy_state()


# ---------------------------------------------------------------------------
# phase timings — where a deploy's seconds went
# ---------------------------------------------------------------------------


@th.django_unit_test("phase timings parse defensively and normalize seconds")
def test_parse_phases(opts):
    """The input is lines a shell script appended on a node, read by the
    platform. Everything that does not fit the shape is dropped rather than
    reported: a malformed line is a missing timing, never a failed callback."""
    from mojo.apps.edge.services import platform_deploy

    entries = platform_deploy.parse_phases(
        "deploy git_sync 1200 ms\n"
        "deploy post_deploy 41230 ms\n"
        "rollback post_deploy 9 s\n"
        "deploy Bad-Name 10 ms\n"          # name charset
        "deploy ok twelve ms\n"            # non-numeric
        "deploy ok 10 minutes\n"           # unknown unit
        "sideways ok 10 ms\n"              # unknown pass
        "deploy ok 10\n"                   # wrong field count
        "deploy ok 10 ms extra\n"
        "\n")

    th.assert_eq([item["phase"] for item in entries],
                 ["git_sync", "post_deploy", "post_deploy"],
                 f"only well-formed lines survive, got {entries!r}")
    th.assert_eq(entries[0], {"phase": "git_sync", "pass": "deploy", "ms": 1200},
                 f"a millisecond entry is carried as-is, got {entries[0]!r}")
    th.assert_eq(entries[2],
                 {"phase": "post_deploy", "pass": "rollback", "ms": 9000,
                  "approx": True},
                 f"seconds normalize to ms and say so — a node whose date has "
                 f"no %3N still has something worth reporting, got "
                 f"{entries[2]!r}")

    flood = "\n".join(["deploy padding 1 ms"] * 200)
    th.assert_eq(len(platform_deploy.parse_phases(flood)),
                 platform_deploy.MAX_PHASES,
                 "the entry count is bounded")
    th.assert_eq(platform_deploy.parse_phases(""), [],
                 "no timings is not an error")


@th.django_unit_test("recorded phases land on the durable row's detail")
def test_record_phases(opts):
    from mojo.apps.edge.services import platform_deploy

    _clean_deploy_state()
    row = _attempt("canary")
    entries = platform_deploy.parse_phases("deploy post_deploy 41230 ms\n")

    th.assert_true(platform_deploy.record_phases(row.pk, entries),
                   "well-formed timings must be stored")
    row.refresh_from_db()
    th.assert_eq(row.detail.get("phases"), entries,
                 f"the timings must be readable at detail.phases, got "
                 f"{row.detail!r}")
    th.assert_eq(platform_deploy.serialize(row)["detail"]["phases"], entries,
                 "and must survive serialization to the API")
    th.assert_true(not platform_deploy.record_phases(row.pk, []),
                   "nothing to record is not a write")
    _clean_deploy_state()


@th.django_unit_test("the engine restart is timed on the platform side")
def test_record_engine_restart(opts):
    """The node cannot time this phase: its last act is to kill the process
    that would have. It is measured from the `verified` transition the dying
    node wrote to the moment its replacement converges the row."""
    from datetime import timedelta

    from django.utils import timezone
    from mojo.apps.edge.models import PlatformDeployment
    from mojo.apps.edge.services import platform_deploy

    _clean_deploy_state()
    row = _attempt("verified")
    verified_at = (timezone.now() - timedelta(seconds=12)).isoformat()
    row.transitions = [{"status": PlatformDeployment.STATUS_VERIFIED,
                        "at": verified_at, "detail": {}}]
    row.detail = {"phases": [{"phase": "post_deploy", "pass": "deploy",
                              "ms": 41230}]}
    row.save(update_fields=["transitions", "detail"])

    th.assert_true(platform_deploy.record_engine_restart(row.pk),
                   "a verified row must get its restart phase")
    row.refresh_from_db()
    phases = row.detail.get("phases") or []
    th.assert_eq([item["phase"] for item in phases],
                 ["post_deploy", "engine_restart"],
                 f"the restart closes the table the node started, got "
                 f"{phases!r}")
    restart = phases[-1]
    th.assert_true(11000 <= restart["ms"] <= 20000,
                   f"the restart must be measured from the verified "
                   f"transition (~12s here), got {restart!r}")

    th.assert_true(not platform_deploy.record_engine_restart(row.pk),
                   "a second finalize must not append a second entry")
    row.refresh_from_db()
    th.assert_eq(len(row.detail.get("phases") or []), 2,
                 "the phase table stays as it was")

    other = _attempt("canary")
    th.assert_true(not platform_deploy.record_engine_restart(other.pk),
                   "a row that never reached `verified` has nothing to measure")
    _clean_deploy_state()

