"""
The deploy orchestrator and node job (maestro item #1458, D4).

The fleet is mocked at exactly two seams — `mojo.apps.jobs` (get_runners /
publish) and `deploy._run` — everything else is real: real Redis state, real
job rows drained with `th.run_pending_jobs` on a private channel so the
handlers run with the engine's own calling convention (`func(job)` with a Job
instance) without touching any other module's queue.

Publishes are captured with `th.capture_publishes` scoped to the deploy
plane's own job funcs: test modules run as parallel threads, and a plain
publish mock here swallowed other modules' real publishes mid-window (the
test_run_jobs_helper flake). Only deploy_node/deploy_orchestrate publishes
are recorded and suppressed; foreign traffic flows through untouched.

The properties under test are mostly ORDERINGS and ABSENCES: the fleet is
never told before the canary proves the release, self is told last, a moved
target is chained rather than lost, and a ghost cannot stomp a later deploy.
"""
from unittest import mock
import uuid

from testit import helpers as th

CHANNEL = "testit_edge_deploy"
SHA_A = "a" * 40
SHA_B = "b" * 40
FRAMEWORK = "9.9.9"

# Sort below/above any real hostname-derived runner id.
CANARY_ID = "0000-canary-engine"
FLEET_ID = "zzzz-fleet-engine"
DEAD_ID = "0000-dead-engine"


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _deploy_publish(call):
    """capture_publishes predicate: only the deploy plane's own publishes."""
    from mojo.apps.edge.services import deploy

    return call.get("func") in (deploy.DEPLOY_NODE_JOB,
                                deploy.DEPLOY_ORCHESTRATE_JOB)


def _channels(calls):
    return [c.get("channel") for c in calls]


def _node_calls(calls):
    from mojo.apps.edge.services import deploy

    return [c for c in calls if c.get("func") == deploy.DEPLOY_NODE_JOB]


def _runners(*alive_ids, dead=()):
    out = [dict(runner_id=r, alive=True) for r in alive_ids]
    out.extend(dict(runner_id=r, alive=False) for r in dead)
    return out


def _drain(opts):
    """Run the queued deploy job(s) with the real Job calling convention."""
    return th.run_pending_jobs(channel=CHANNEL)


@th.django_unit_setup()
def setup_orchestrate(opts):
    from mojo.apps.edge.services import deploy
    from mojo.apps.edge.models import PlatformDeployment
    from mojo.apps.jobs.models import Job

    deploy.get_client().delete(deploy.TARGET_KEY, deploy.STATUS_KEY)
    PlatformDeployment.objects.all().delete()
    Job.objects.filter(channel=CHANNEL).delete()
    opts.me = deploy.local_runner_id()


@th.django_unit_test("pick_canary: deterministic, never self, single-runner is None")
def test_pick_canary(opts):
    from mojo.apps.edge.services import deploy

    ids = [FLEET_ID, CANARY_ID, opts.me]
    th.assert_eq(deploy.pick_canary(ids, opts.me), CANARY_ID,
                 "the canary must be the lowest runner id excluding self")
    th.assert_eq(deploy.pick_canary([opts.me], opts.me), None,
                 "a single-runner fleet has no canary")
    th.assert_eq(deploy.pick_canary(sorted([CANARY_ID, opts.me]), CANARY_ID), opts.me,
                 "the canary picker must never return the caller itself")


@th.django_unit_test("alive_runner_ids drops runners the heartbeat says are dead")
def test_alive_filter(opts):
    from mojo.apps.edge.services import deploy

    runners = _runners(FLEET_ID, opts.me, dead=(DEAD_ID,))
    ids = deploy.alive_runner_ids(runners)
    th.assert_eq(ids, sorted([FLEET_ID, opts.me]),
                 f"dead runners must not be deploy targets, got {ids!r}")


def _arm(sha, roster, actor="test"):
    from mojo.apps.edge.models import PlatformDeployment
    from mojo.apps.edge.services import deploy
    row = PlatformDeployment.objects.create(
        sha=sha, actor=actor, source="test", request_key=str(uuid.uuid4()),
        frozen_roster=list(roster), transitions=[])
    deploy.set_target(sha, actor=actor, deployment_id=row.pk)
    deploy.arm_status(sha, deployment_id=row.pk)
    return row


def _publish_orchestrate(sha, deployment):
    from mojo.apps import jobs
    from mojo.apps.edge.services import deploy

    return jobs.publish(
        func=deploy.DEPLOY_ORCHESTRATE_JOB,
        payload=dict(sha=sha, deployment=str(deployment.pk)), channel=CHANNEL)


def _node_payload(opts, sha, migrate):
    from mojo.apps.edge.models import PlatformDeployment
    row = PlatformDeployment.objects.create(
        sha=sha, actor="test", source="test", request_key=str(uuid.uuid4()),
        frozen_roster=[opts.me], transitions=[])
    return dict(
        sha=sha, framework=FRAMEWORK, migrate=bool(migrate),
        deployment=str(row.pk)), row


@th.django_unit_test("single-runner fleet: local canary update, fire-and-forget")
def test_single_runner(opts):
    import mojo.apps.jobs as jobs_module
    from mojo.apps.edge.services import deploy

    deployment = _arm(SHA_A, [opts.me])
    _publish_orchestrate(SHA_A, deployment)

    with th.capture_publishes(_deploy_publish) as calls, \
         mock.patch.object(jobs_module, "get_runners",
                           return_value=_runners(opts.me)), \
         mock.patch.object(deploy, "resolve_framework_version",
                           return_value=FRAMEWORK):
        ran = _drain(opts)

    th.assert_eq(ran, 1, f"the orchestrate job must have executed, ran={ran}")
    th.assert_eq(len(calls), 1,
                 f"a single-runner fleet publishes exactly one node job, got {calls!r}")
    call = calls[0]
    th.assert_eq(call["channel"], opts.me, "the local node must be the target")
    th.assert_true(call["payload"]["migrate"],
                   "the single node runs the migrating update")
    th.assert_eq(call["payload"]["sha"], SHA_A, "the pinned sha must travel in the payload")
    th.assert_eq(call["payload"]["framework"], FRAMEWORK,
                 "the pinned framework version must travel in the payload")
    th.assert_eq(call["max_retries"], 0,
                 "deploy jobs publish max_retries=0 — a redelivery re-runs an update")
    th.assert_true(call.get("expires_in"),
                   "deploy jobs must expire rather than fire hours late")
    deploy.clear_status(deployment.pk)


@th.django_unit_test("canary failure: no fleet node is ever told, incident filed, status cleared")
def test_canary_failure(opts):
    import mojo.apps.incident.reporter as reporter_module
    import mojo.apps.jobs as jobs_module
    from mojo.apps.edge.services import deploy

    deployment = _arm(SHA_A, [CANARY_ID, opts.me, FLEET_ID])
    # The canary already reported failure; the poll sees it immediately.
    deploy.set_status(
        deploy.STATUS_FAILED, SHA_A, detail="sanity check failed: local request",
        deployment_id=deployment.pk)
    _publish_orchestrate(SHA_A, deployment)

    incidents = mock.Mock()
    with th.capture_publishes(_deploy_publish) as calls, \
         mock.patch.object(jobs_module, "get_runners",
                           return_value=_runners(CANARY_ID, opts.me, FLEET_ID)), \
         mock.patch.object(deploy, "resolve_framework_version",
                           return_value=FRAMEWORK), \
         mock.patch.object(reporter_module, "report_event", incidents):
        _drain(opts)

    th.assert_eq(len(_node_calls(calls)), 1,
                 f"only the canary may ever have been told, got {calls!r}")
    th.assert_eq(_node_calls(calls)[0]["channel"], CANARY_ID,
                 "the one node publish must be the canary")
    th.assert_true(incidents.called, "a canary failure must file an incident")
    th.assert_eq(incidents.call_args.kwargs.get("level"), 7,
                 f"canary failure is a level-7 incident, got {incidents.call_args!r}")
    th.assert_in("update_failed", incidents.call_args.args[0],
                 "the canary failure must use a fixed incident phase")
    th.assert_true("local request" not in incidents.call_args.args[0],
                   "raw canary output reached incident evidence")
    th.assert_eq(deploy.get_status(), None,
                 "the terminal path must clear the status so the next push starts clean")


@th.django_unit_test("canary success: fleet told the same pins, self told LAST, status cleared")
def test_canary_success_flow(opts):
    import mojo.apps.jobs as jobs_module
    from mojo.apps.edge.services import deploy

    deployment = _arm(SHA_A, [CANARY_ID, opts.me, FLEET_ID])
    deploy.set_status(
        deploy.STATUS_DEPLOYING, SHA_A, deployment_id=deployment.pk)
    _publish_orchestrate(SHA_A, deployment)

    get_runners = mock.Mock(return_value=_runners(CANARY_ID, opts.me, FLEET_ID,
                                                  dead=(DEAD_ID,)))
    with th.capture_publishes(_deploy_publish) as calls, \
         mock.patch.object(jobs_module, "get_runners", get_runners), \
         mock.patch.object(deploy, "resolve_framework_version",
                           return_value=FRAMEWORK):
        _drain(opts)

    node_calls = _node_calls(calls)
    th.assert_eq(
        [c["channel"] for c in node_calls], [CANARY_ID, FLEET_ID, opts.me],
        f"order must be canary, fleet, SELF LAST — got {_channels(calls)!r}")
    th.assert_true(node_calls[0]["payload"]["migrate"],
                   "only the canary migrates")
    th.assert_true(not node_calls[1]["payload"]["migrate"],
                   "fleet nodes must not migrate")
    th.assert_true(not node_calls[2]["payload"]["migrate"],
                   "the orchestrator's own update must not migrate")
    for call in node_calls:
        th.assert_eq(call["payload"]["sha"], SHA_A,
                     f"every node must be told the SAME commit, got {call!r}")
        th.assert_eq(call["payload"]["framework"], FRAMEWORK,
                     f"every node must be told the SAME framework version, got {call!r}")
        th.assert_eq(call["max_retries"], 0,
                     f"every deploy publish is max_retries=0, got {call!r}")
        th.assert_true(call.get("expires_in"),
                       f"every deploy publish carries an expiry, got {call!r}")
    th.assert_true(DEAD_ID not in _channels(calls),
                   "a dead runner must never be told to deploy")
    th.assert_eq(get_runners.call_count, 0,
                 "orchestration must use the durable frozen roster, never a live re-read")
    th.assert_eq(deploy.get_status(), None,
                 "the multi-node terminal must delete the status")
    deployment.refresh_from_db()
    th.assert_eq(deployment.status, "fleet",
                 "canary proof was mislabeled as healthy fleet verification")
    th.assert_true(deployment.finished is None,
                   "fleet dispatch became terminal before restarted-node proof")


@th.django_unit_test("canary timeout: failed + incident, fleet untouched")
def test_canary_timeout(opts):
    import mojo.apps.incident.reporter as reporter_module
    import mojo.apps.jobs as jobs_module
    from mojo.apps.edge import asyncjobs
    from mojo.apps.edge.services import deploy

    deployment = _arm(SHA_A, [CANARY_ID, opts.me, FLEET_ID])
    _publish_orchestrate(SHA_A, deployment)  # canary remains silent

    incidents = mock.Mock()
    with th.capture_publishes(_deploy_publish) as calls, \
         mock.patch.object(jobs_module, "get_runners",
                           return_value=_runners(CANARY_ID, opts.me, FLEET_ID)), \
         mock.patch.object(deploy, "resolve_framework_version",
                           return_value=FRAMEWORK), \
         mock.patch.object(deploy, "canary_timeout", return_value=1), \
         mock.patch.object(asyncjobs, "DEPLOY_POLL_INTERVAL", 0.05), \
         mock.patch.object(reporter_module, "report_event", incidents):
        _drain(opts)

    th.assert_eq(len(_node_calls(calls)), 1,
                 f"a timed-out canary must leave the fleet untouched, got {calls!r}")
    th.assert_true(incidents.called, "a canary timeout must file an incident")
    th.assert_in("did not report", incidents.call_args.args[0],
                 f"the incident must say the canary went silent, got {incidents.call_args!r}")
    th.assert_eq(deploy.get_status(), None,
                 "the timeout terminal must still clear the status")


@th.django_unit_test("a target overwritten mid-deploy is chained, and the stale self-update skipped")
def test_chain_on_moved_target(opts):
    import mojo.apps.jobs as jobs_module
    from mojo.apps.edge.services import deploy

    deployment = _arm(SHA_A, [CANARY_ID, opts.me, FLEET_ID])
    _publish_orchestrate(SHA_A, deployment)

    def status_with_push(*args, **kwargs):
        # The canary proved SHA_A — and meanwhile a new push moved the target.
        next_row = _arm(SHA_B, [CANARY_ID, opts.me, FLEET_ID], actor="github:later")
        return dict(
            state=deploy.STATUS_DEPLOYING, sha=SHA_A,
            deployment=str(deployment.pk))

    with th.capture_publishes(_deploy_publish) as calls, \
         mock.patch.object(jobs_module, "get_runners",
                           return_value=_runners(CANARY_ID, opts.me, FLEET_ID)), \
         mock.patch.object(deploy, "resolve_framework_version",
                           return_value=FRAMEWORK), \
         mock.patch.object(deploy, "get_status", side_effect=status_with_push):
        _drain(opts)

    channels = [c["channel"] for c in _node_calls(calls)]
    th.assert_eq(channels, [CANARY_ID, FLEET_ID],
                 f"the proven release still ships to the fleet, but the stale "
                 f"self-update must be skipped — got {channels!r}")
    chained = [c for c in calls
               if c.get("func") == deploy.DEPLOY_ORCHESTRATE_JOB]
    th.assert_eq(len(chained), 1,
                 f"the moved target must chain exactly one fresh orchestrate, got {calls!r}")
    th.assert_eq(chained[-1]["payload"]["sha"], SHA_B,
                 "the chained deploy must carry the NEW target")
    status = deploy.get_status()
    th.assert_true(status and status["sha"] == SHA_B
                   and status["state"] == deploy.STATUS_MIGRATING,
                   f"the chain must re-arm the status for the new deploy, got {status!r}")
    deploy.clear_status(status["deployment"])


@th.django_unit_test("framework resolution failure fails the deploy before any node is told")
def test_resolution_failure_fails_deploy(opts):
    import mojo.apps.incident.reporter as reporter_module
    import mojo.apps.jobs as jobs_module
    from mojo.apps.edge.services import deploy

    deployment = _arm(SHA_A, [CANARY_ID, opts.me])
    _publish_orchestrate(SHA_A, deployment)

    incidents = mock.Mock()
    with th.capture_publishes(_deploy_publish) as calls, \
         mock.patch.object(jobs_module, "get_runners",
                           return_value=_runners(CANARY_ID, opts.me)), \
         mock.patch.object(deploy, "resolve_framework_version",
                           side_effect=ValueError(
                               "provider password=framework-sentinel")), \
         mock.patch.object(reporter_module, "report_event", incidents):
        _drain(opts)

    th.assert_eq(_node_calls(calls), [],
                 "an unpinned deploy must never reach a node (C1)")
    th.assert_true(incidents.called, "the failed resolution must file an incident")
    th.assert_true("framework-sentinel" not in incidents.call_args.args[0],
                   "provider exception messages must never enter incidents")
    deployment.refresh_from_db()
    th.assert_true("framework-sentinel" not in str(deployment.transitions),
                   "provider exception messages must never enter the journal")
    th.assert_eq(deploy.get_status(), None,
                 "the failed deploy must clear the status for the next push")


@th.django_unit_test("deploy_node: refuses to run when EDGE_DEPLOY_SCRIPT is not configured")
def test_node_unconfigured(opts):
    import mojo.apps.incident.reporter as reporter_module
    from mojo.apps import jobs
    from mojo.apps.edge.services import deploy
    from mojo.apps.jobs.models import Job

    payload, deployment = _node_payload(opts, SHA_A, True)
    job_id = jobs.publish(
        func=deploy.DEPLOY_NODE_JOB,
        payload=payload,
        channel=CHANNEL)
    incidents = mock.Mock()
    with mock.patch.object(reporter_module, "report_event", incidents):
        _drain(opts)

    row = Job.objects.get(id=job_id)
    th.assert_eq(row.status, "failed",
                 f"an unconfigured node must fail the job loudly, got {row.status}")
    th.assert_true(incidents.called,
                   "the refusal must be an incident, not a silent skip")
    th.assert_in("EDGE_DEPLOY_SCRIPT", incidents.call_args.args[0],
                 "the incident must name the missing setting")


@th.django_unit_test("deploy_node: constant argv, validated inputs, success completes")
def test_node_runs_script(opts):
    from mojo.apps import jobs
    from mojo.apps.edge.services import deploy
    from mojo.apps.jobs.models import Job

    payload, deployment = _node_payload(opts, SHA_A, True)
    job_id = jobs.publish(
        func=deploy.DEPLOY_NODE_JOB,
        payload=payload,
        channel=CHANNEL)
    ran = []
    with mock.patch.object(deploy, "deploy_script_argv",
                           return_value=["/bin/echo"]), \
         mock.patch.object(deploy, "_run",
                           side_effect=lambda argv: ran.append(list(argv)) or FakeProc(0)):
        _drain(opts)

    row = Job.objects.get(id=job_id)
    th.assert_eq(row.status, "completed",
                 f"a zero-exit script completes the job, got {row.status}")
    th.assert_eq(
        ran, [["/bin/echo", "--sha", SHA_A, "--framework", FRAMEWORK,
               "--deployment", str(deployment.pk), "--migrate"]],
        f"the argv must be the configured base plus validated args, got {ran!r}")


@th.django_unit_test("deploy_node: a fleet-node failure is an incident, never a rollback")
def test_node_failure_no_rollback(opts):
    import mojo.apps.incident.reporter as reporter_module
    from mojo.apps import jobs
    from mojo.apps.edge.services import deploy
    from mojo.apps.jobs.models import Job

    deployment = _arm(SHA_A, [opts.me])
    payload = dict(
        sha=SHA_A, framework=FRAMEWORK, migrate=False,
        deployment=str(deployment.pk))
    job_id = jobs.publish(
        func=deploy.DEPLOY_NODE_JOB,
        payload=payload,
        channel=CHANNEL)
    incidents = mock.Mock()
    with th.capture_publishes(_deploy_publish) as calls, \
         mock.patch.object(deploy, "deploy_script_argv",
                           return_value=["/bin/echo"]), \
         mock.patch.object(deploy, "_run",
                           return_value=FakeProc(
                               23, stderr="password=sentinel-secret pip exploded")), \
         mock.patch.object(reporter_module, "report_event", incidents):
        _drain(opts)

    row = Job.objects.get(id=job_id)
    th.assert_eq(row.status, "failed", f"the node job must fail, got {row.status}")
    th.assert_true(incidents.called, "the failed node must appear on the dashboard (D7)")
    incident_message = incidents.call_args.args[0]
    th.assert_in("phase=update_script, exit=23", incident_message,
                 "the incident must retain fixed phase and exit metadata")
    th.assert_true("sentinel-secret" not in incident_message,
                   "raw process output must never enter an incident")
    deployment.refresh_from_db()
    th.assert_true("sentinel-secret" not in str(deployment.node_evidence),
                   "raw process output must never enter durable evidence")
    th.assert_eq(calls, [],
                 "one node's failure after release must not publish anything — no rollback")
    status = deploy.get_status()
    th.assert_true(status and status["sha"] == SHA_A,
                   f"a fleet-node failure must not touch the deploy status, got {status!r}")
    deploy.clear_status(deployment.pk)


@th.django_unit_test("deploy_node: an invalid sha never reaches the script")
def test_node_validates_sha(opts):
    from mojo.apps import jobs
    from mojo.apps.edge.services import deploy
    from mojo.apps.jobs.models import Job

    payload, deployment = _node_payload(opts, "main; rm -rf /", False)
    job_id = jobs.publish(
        func=deploy.DEPLOY_NODE_JOB,
        payload=payload,
        channel=CHANNEL)
    ran = []
    with mock.patch.object(deploy, "deploy_script_argv",
                           return_value=["/bin/echo"]), \
         mock.patch.object(deploy, "_run",
                           side_effect=lambda argv: ran.append(list(argv)) or FakeProc(0)):
        _drain(opts)

    row = Job.objects.get(id=job_id)
    th.assert_eq(row.status, "failed",
                 f"an invalid sha must fail the job, got {row.status}")
    th.assert_eq(ran, [], "the script must never run with an unvalidated sha")
