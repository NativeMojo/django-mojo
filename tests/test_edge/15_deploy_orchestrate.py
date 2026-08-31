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
        deployment=str(row.pk), api_cohort=True), row


@th.django_unit_test("deploy_node: constant argv, validated inputs, success completes")
def test_node_runs_script(opts):
    from mojo.apps import jobs
    from mojo.apps.edge.services import deploy
    from mojo.apps.jobs.models import Job

    payload, deployment = _node_payload(opts, SHA_A, True)
    deploy.arm_status(SHA_A, deployment_id=deployment.pk)
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
               "--deployment", str(deployment.pk), "--node-type", "api",
               "--migrate"]],
        f"the argv must be the configured base plus validated args, got {ran!r}")


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


@th.django_unit_test("deploy recycle replaces BOTH job components")
def test_recycle_command_covers_engine_and_scheduler(opts):
    """Item #3429: the recycle uses jobman's bare verbs, which walk engine
    then scheduler — an engine-scoped recycle left a root scheduler (and a
    stale scheduler on every healthy deploy) running forever."""
    from mojo.apps.edge import asyncjobs

    command = asyncjobs._recycle_command()
    th.assert_in('-m mojo.deploy.jobman stop --root "$2" --grace 2', command,
                 "the recycle must stop BOTH components (bare stop verb)")
    th.assert_in('-m mojo.deploy.jobman start --root "$2"', command,
                 "the recycle must start BOTH components (bare start verb)")
    th.assert_true(" stop engine" not in command
                   and " start engine" not in command,
                   "an engine-scoped recycle leaves the scheduler behind")
