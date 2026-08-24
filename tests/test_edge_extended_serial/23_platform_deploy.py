"""Split out of tests/test_edge/23_platform_deploy.py (maestro #1839).

These tests patch shared production surfaces (mojo.apps.jobs.publish /
get_runners_bounded, mojo.apps.jobs.manager.JobManager,
mojo.apps.incident.reporter) — process-global, so unsafe under the parallel
default tier.
"""

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


def _clean_node_jobs():
    from mojo.apps.jobs.models import Job, JobEvent
    from mojo.apps.edge.services import deploy

    JobEvent.objects.filter(job__func=deploy.DEPLOY_NODE_JOB).delete()
    Job.objects.filter(func=deploy.DEPLOY_NODE_JOB).delete()


@th.django_unit_test("API and specialized deploy channels freeze one typed roster")
def test_exact_edge_roster(opts):
    from mojo.apps.edge.services import platform_deploy
    api_rows = [
        {"runner_id": "edge-b-engine", "alive": True},
        {"runner_id": "edge-a-engine", "alive": True},
        {"runner_id": "dead-engine", "alive": False},
    ]
    specialized_rows = [
        {"runner_id": "sites-engine", "alive": True},
        {"runner_id": "edge-b-engine", "alive": True},
    ]
    with mock.patch(
            "mojo.apps.jobs.get_runners_bounded",
            side_effect=lambda **kw: api_rows if kw["channel"] == "edge"
            else specialized_rows) as get_runners:
        roster, api = platform_deploy.edge_roster(with_api=True)
    th.assert_eq(roster, ["edge-a-engine", "edge-b-engine", "sites-engine"],
                 "the webhook must freeze both deployment cohorts")
    th.assert_eq(api, ["edge-a-engine", "edge-b-engine"],
                 "a duplicate specialized consumer remains classified API")
    th.assert_eq([call.kwargs["channel"] for call in get_runners.call_args_list],
                 ["edge", "platform-deploy"],
                 "roster discovery must use only the two fixed channels")


@th.django_unit_test("edge roster overflow fails closed instead of omitting nodes")
def test_edge_roster_overflow(opts):
    from mojo.apps.edge.services import platform_deploy
    with mock.patch(
            "mojo.apps.jobs.get_runners_bounded",
            side_effect=RuntimeError("runner_roster_overflow")):
        with th.assert_raises(RuntimeError):
            platform_deploy.edge_roster()


@th.django_unit_test("creation preserves a 33-of-128 API cohort without sanitizer truncation")
def test_full_api_cohort_is_persisted(opts):
    from mojo.apps.edge.services import platform_deploy

    api = [f"api-{index:03d}-engine" for index in range(33)]
    specialized = [f"sites-{index:03d}-engine" for index in range(95)]
    roster = api + specialized
    with mock.patch.object(
            platform_deploy, "edge_roster", return_value=(roster, api)):
        row, replayed = platform_deploy.create(
            "e" * 40, actor="typed-roster-test", source="test")
    th.assert_true(not replayed, "a fresh typed roster unexpectedly replayed")
    th.assert_eq(len(row.frozen_roster), 128,
                 "the bounded union lost specialized runners")
    th.assert_eq(row.detail.get("api_roster"), api,
                 "the generic detail sanitizer truncated the migration cohort")


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


@th.django_unit_test("verify() preserves the terminal failure diagnosis")
def test_verify_preserves_diagnosis(opts):
    """The headline regression (item 2225): node_evidence is latest-per-runner,
    so an Admin clicking Verify on a FAILED deploy used to replace the only
    copy of the failure (phase + stderr tail) with `unavailable` — destroying
    the evidence the button was pressed to inspect. The immutable `diagnosis`
    journal is what must survive that."""
    from mojo.apps.edge.services import platform_deploy

    _clean_deploy_state()
    with mock.patch("mojo.apps.jobs.get_runners_bounded", return_value=[{
            "runner_id": "edge-a-engine", "alive": True}]):
        row, _ = platform_deploy.create(SHA, source="test")
    th.assert_true(platform_deploy.evidence(
        row.pk, "edge-a-engine", "failed",
        detail={"phase": "post_deploy_migrate",
                "stderr_tail": ["FATAL: migration command refused schema drift"]}),
        "the frozen-roster runner's failure report must land")
    platform_deploy.transition(
        row.pk, "failed", {"phase": "post_deploy_migrate", "source": "node_report"})

    with mock.patch(
            "mojo.apps.jobs.manager.JobManager.execute_on_runner",
            return_value=None):
        result = platform_deploy.verify(row.pk)

    th.assert_eq(result.node_evidence[0]["state"], "unavailable",
                 "verify must still record the live observation it made")
    th.assert_eq(result.status, "failed",
                 f"verify must never move a terminal row, got {result.status}")
    entries = [item for item in (result.diagnosis or [])
               if isinstance(item, dict)]
    th.assert_true(entries,
                   "verify() destroyed the terminal failure diagnosis")
    first = entries[0]
    th.assert_eq(first.get("kind"), "failure",
                 f"the first diagnosis entry must be the failure, got {first!r}")
    th.assert_eq(first.get("runner"), "edge-a-engine",
                 f"the diagnosis must name the failed runner, got {first!r}")
    detail = first.get("detail") or {}
    th.assert_eq(detail.get("phase"), "post_deploy_migrate",
                 f"the failure phase must survive verify, got {detail!r}")
    th.assert_eq(detail.get("stderr_tail"),
                 ["FATAL: migration command refused schema drift"],
                 f"the stderr tail must survive verify, got {detail!r}")
    th.assert_true(first.get("at"),
                   f"the diagnosis entry must carry its timestamp, got {first!r}")
    _clean_deploy_state()


@th.django_unit_test("a jobs-plane failure never propagates out of the handoff")
def test_close_handoff_job_swallows_failures(opts):
    """It runs on the deploy callback and on the rollback report. Neither may
    be blocked by a Redis or jobs problem — the same rule the incident
    reporter follows."""
    from mojo.apps.jobs.manager import JobManager
    from mojo.apps.edge.services import platform_deploy

    _clean_deploy_state()
    _clean_node_jobs()
    row = _attempt("canary")
    _node_job(row.pk)

    with mock.patch.object(JobManager, "release_inflight",
                           side_effect=RuntimeError("redis is down")):
        closed = platform_deploy.close_handoff_job(row.pk, "completed")

    th.assert_eq(closed, 0,
                 f"a failed handoff reports nothing closed rather than "
                 f"raising, got {closed}")
    _clean_node_jobs()
    _clean_deploy_state()


# ---------------------------------------------------------------------------
# Moved from tests/test_edge/23_platform_deploy.py (maestro #2558): these two
# assert the CONNECTION wiring itself, which is only observable by patching
# mojo.helpers.redis.get_bounded_connection / the redis client's settings
# singleton — process-global, so unsafe under the parallel default tier. The
# fail-closed discovery semantics stay in the default module through the
# `client=` seam on JobManager.get_runners_bounded.
# ---------------------------------------------------------------------------


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
