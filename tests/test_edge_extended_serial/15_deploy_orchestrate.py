"""Split out of tests/test_edge/15_deploy_orchestrate.py (maestro #1839).

These tests patch shared production surfaces — mojo.apps.jobs (get_runners)
and mojo.apps.incident.reporter (report_event) — which are process-global, so
they are unsafe under the parallel default tier even though test_edge itself
is serial. The seams, channel discipline and capture_publishes scoping are
exactly the source module's; see its docstring.
"""
import subprocess
from unittest import mock
import uuid

from testit import helpers as th


CHANNEL = "testit_edge_deploy"


SHA_A = "a" * 40


SHA_B = "b" * 40


FRAMEWORK = "9.9.9"


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


def _arm(sha, roster, actor="test", api_roster=None):
    from mojo.apps.edge.models import PlatformDeployment
    from mojo.apps.edge.services import deploy
    row = PlatformDeployment.objects.create(
        sha=sha, actor=actor, source="test", request_key=str(uuid.uuid4()),
        frozen_roster=list(roster), transitions=[],
        detail={} if api_roster is None else {"api_roster": list(api_roster)})
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


@th.django_unit_test("mixed fleet migrates on API canary and types every fan-out")
def test_mixed_fleet_uses_api_canary(opts):
    import mojo.apps.jobs as jobs_module
    from mojo.apps.edge.services import deploy

    deployment = _arm(
        SHA_A, [CANARY_ID, FLEET_ID, opts.me],
        api_roster=[CANARY_ID])
    deploy.set_status(
        deploy.STATUS_DEPLOYING, SHA_A, deployment_id=deployment.pk)
    _publish_orchestrate(SHA_A, deployment)

    with th.capture_publishes(_deploy_publish) as calls, \
         mock.patch.object(jobs_module, "get_runners") as live_read, \
         mock.patch.object(deploy, "resolve_framework_version",
                           return_value=FRAMEWORK):
        _drain(opts)

    node_calls = _node_calls(calls)
    th.assert_eq([call["channel"] for call in node_calls],
                 [CANARY_ID, FLEET_ID, opts.me],
                 "the API canary must release specialized nodes before self")
    th.assert_eq(
        [(call["payload"]["migrate"], call["payload"]["api_cohort"])
         for call in node_calls],
        [(True, True), (False, False), (False, False)],
        "routing must carry the frozen API boundary without a type RPC")
    th.assert_eq(live_read.call_count, 0,
                 "the frozen typed roster must not be rediscovered mid-deploy")


@th.django_unit_test("a specialized-only roster fails before resolving or mutating")
def test_no_api_canary_refuses_deploy(opts):
    import mojo.apps.incident.reporter as reporter_module
    from mojo.apps.edge.services import deploy

    deployment = _arm(SHA_A, [FLEET_ID], api_roster=[])
    _publish_orchestrate(SHA_A, deployment)
    incidents = mock.Mock(return_value=mock.Mock(pk=2815))
    resolve = mock.Mock(return_value=FRAMEWORK)
    with th.capture_publishes(_deploy_publish) as calls, \
         mock.patch.object(deploy, "resolve_framework_version", resolve), \
         mock.patch.object(reporter_module, "report_event", incidents):
        _drain(opts)

    th.assert_eq(_node_calls(calls), [],
                 "a migration-dependent release must not touch an API-free roster")
    th.assert_eq(resolve.call_count, 0,
                 "refusal must precede framework or node mutation")
    deployment.refresh_from_db()
    th.assert_eq(deployment.status, "failed",
                 "the refused attempt must be durably terminal")


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


@th.django_unit_test("orchestrate: a lease stolen mid-canary stands down, quietly and promptly")
def test_poll_loop_supersession(opts):
    """Before this, a superseded orchestrator kept polling for a canary that
    was never going to report — then filed a false 'canary went silent'
    incident and chained a fresh orchestrate ON TOP of the deploy that had
    taken the lease. It must leave instead: no incident, no chain, no
    self-update, and without burning the canary timeout first."""
    import time as _time

    import mojo.apps.incident.reporter as reporter_module
    import mojo.apps.jobs as jobs_module
    from mojo.apps.edge import asyncjobs
    from mojo.apps.edge.services import deploy

    deployment = _arm(SHA_A, [CANARY_ID, opts.me, FLEET_ID])
    _publish_orchestrate(SHA_A, deployment)
    thief = str(uuid.uuid4())
    reads = []

    def stolen_lease(*args, **kwargs):
        reads.append(1)
        if len(reads) == 1:
            # The pre-flight read: this deploy still owns the lease.
            return dict(state=deploy.STATUS_MIGRATING, sha=SHA_A,
                        deployment=str(deployment.pk))
        return dict(state=deploy.STATUS_MIGRATING, sha=SHA_B, deployment=thief)

    incidents = mock.Mock(return_value=mock.Mock(pk=1997))
    started = _time.time()
    with th.capture_publishes(_deploy_publish) as calls, \
         mock.patch.object(jobs_module, "get_runners",
                           return_value=_runners(CANARY_ID, opts.me, FLEET_ID)), \
         mock.patch.object(deploy, "resolve_framework_version",
                           return_value=FRAMEWORK), \
         mock.patch.object(deploy, "canary_timeout", return_value=120), \
         mock.patch.object(asyncjobs, "DEPLOY_POLL_INTERVAL", 0.05), \
         mock.patch.object(deploy, "get_status", side_effect=stolen_lease), \
         mock.patch.object(reporter_module, "report_event", incidents):
        _drain(opts)
    elapsed = _time.time() - started

    th.assert_true(elapsed < 10,
                   f"a superseded orchestrator must break out at once, not wait "
                   f"out the canary timeout — took {elapsed:.1f}s")
    th.assert_eq(len(_node_calls(calls)), 1,
                 f"only the canary may ever have been told, got {calls!r}")
    th.assert_true(not incidents.called,
                   f"supersession is not a canary failure and must file no "
                   f"incident, got {incidents.call_args_list!r}")
    chained = [c for c in calls if c.get("func") == deploy.DEPLOY_ORCHESTRATE_JOB]
    th.assert_eq(chained, [],
                 f"a superseded deploy must not chain a second orchestrate on "
                 f"top of the deploy that took the lease, got {chained!r}")
    deployment.refresh_from_db()
    th.assert_eq(deployment.status, "superseded",
                 f"the stood-down attempt must be recorded superseded, "
                 f"got {deployment.status}")
    deploy.clear_status(deployment.pk)


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
    with mock.patch.object(reporter_module, "report_event", incidents), \
         mock.patch.object(deploy, "deploy_script_argv", return_value=None):
        _drain(opts)

    row = Job.objects.get(id=job_id)
    th.assert_eq(row.status, "failed",
                 f"an unconfigured node must fail the job loudly, got {row.status}")
    th.assert_true(incidents.called,
                   "the refusal must be an incident, not a silent skip")
    th.assert_in("EDGE_DEPLOY_SCRIPT", incidents.call_args.args[0],
                 "the incident must name the missing setting")


@th.django_unit_test("deploy_node refuses routing/local lifecycle disagreement")
def test_node_type_mismatch_stops_before_shell(opts):
    from mojo.apps import jobs
    from mojo.apps.edge.services import deploy
    from mojo.apps.jobs.models import Job

    payload, deployment = _node_payload(opts, SHA_A, False)
    payload["api_cohort"] = True
    job_id = jobs.publish(
        func=deploy.DEPLOY_NODE_JOB, payload=payload, channel=CHANNEL)
    ran = mock.Mock(return_value=FakeProc(0))
    with mock.patch.object(deploy, "local_node_type", return_value="sites"), \
         mock.patch.object(deploy, "deploy_script_argv", return_value=["/bin/echo"]), \
         mock.patch.object(deploy, "_run", ran):
        _drain(opts)

    th.assert_eq(Job.objects.get(pk=job_id).status, "failed",
                 "an edge/API job on a Sites node must fail loudly")
    th.assert_eq(ran.call_count, 0,
                 "cohort mismatch must be refused before checkout or pip")


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
                               23,
                               stderr="password=sentinel-secret pip exploded\n"
                                      "collecting wheels for numpy\n")), \
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
    th.assert_true("numpy" not in incident_message,
                   "the stderr tail is evidence only — it must never enter an incident")
    deployment.refresh_from_db()
    th.assert_true("sentinel-secret" not in str(deployment.node_evidence),
                   "raw process output must never enter durable evidence")
    tail = [(item.get("detail") or {}).get("stderr_tail")
            for item in (deployment.node_evidence or [])]
    tail = [entry for entry in tail if entry]
    th.assert_eq(tail, [["[redacted]", "collecting wheels for numpy"]],
                 f"the tail must redact per line and keep the benign one "
                 f"verbatim, got {tail!r}")
    th.assert_eq(calls, [],
                 "one node's failure after release must not publish anything — no rollback")
    status = deploy.get_status()
    th.assert_true(status and status["sha"] == SHA_A,
                   f"a fleet-node failure must not touch the deploy status, got {status!r}")
    deploy.clear_status(deployment.pk)


@th.django_unit_test("deploy_node: a script that cannot be executed is reported, not swallowed")
def test_node_exec_failure_is_reported(opts):
    """Regression: the update script exists but the engine user cannot exec it
    (a shim committed 0644). `_run` raises before any process starts, so the
    old code let the exception escape with no incident, no evidence, and a
    lease left `migrating` until its TTL — an invisible deploy."""
    import errno

    import mojo.apps.incident.reporter as reporter_module
    from mojo.apps import jobs
    from mojo.apps.edge.services import deploy
    from mojo.apps.jobs.models import Job

    deployment = _arm(SHA_A, [opts.me])
    job_id = jobs.publish(
        func=deploy.DEPLOY_NODE_JOB,
        payload=dict(sha=SHA_A, framework=FRAMEWORK, migrate=True,
                     deployment=str(deployment.pk)),
        channel=CHANNEL)
    incidents = mock.Mock(return_value=mock.Mock(pk=1997))
    with th.capture_publishes(_deploy_publish), \
         mock.patch.object(deploy, "deploy_script_argv", return_value=["/bin/echo"]), \
         mock.patch.object(deploy, "_run", side_effect=PermissionError(
             errno.EACCES, "password=exec-sentinel denied")), \
         mock.patch.object(reporter_module, "report_event", incidents):
        _drain(opts)

    row = Job.objects.get(id=job_id)
    th.assert_eq(row.status, "failed",
                 f"an unexecutable script must fail the node job, got {row.status}")
    th.assert_true(incidents.called,
                   "a script that cannot be executed must file an incident")
    th.assert_eq(incidents.call_args.kwargs.get("level"), 7,
                 f"an exec failure is a level-7 incident, got {incidents.call_args!r}")
    message = incidents.call_args.args[0]
    th.assert_in("EACCES", message,
                 f"the incident must name the errno so an operator can act, got {message!r}")
    th.assert_in("/bin/echo", message,
                 f"the incident must name the script that could not run, got {message!r}")
    th.assert_true("exec-sentinel" not in message,
                   "raw OSError text must never enter an incident")

    deployment.refresh_from_db()
    phases = [(item.get("detail") or {}).get("phase")
              for item in (deployment.node_evidence or [])]
    th.assert_in("exec_failed", phases,
                 f"the failure must land as durable node evidence, got {phases!r}")
    th.assert_true("exec-sentinel" not in str(deployment.node_evidence),
                   "raw OSError text must never enter durable evidence")
    th.assert_eq(deployment.status, "failed",
                 f"a migrating node's exec failure must close the attempt, "
                 f"got {deployment.status}")
    status = deploy.get_status()
    th.assert_true(status and status.get("state") == deploy.STATUS_FAILED,
                   f"the migrating node must release the lease as failed, got {status!r}")
    th.assert_eq((status or {}).get("detail"), "exec_failed",
                 f"the lease must carry the fixed exec phase, got {status!r}")
    deploy.clear_status(deployment.pk)


@th.django_unit_test("deploy_node: a timed-out script reports a redacted stderr tail")
def test_node_timeout_is_reported(opts):
    """Regression: `_run` kills the script at SCRIPT_TIMEOUT and raises. The old
    code swallowed that the same way as an exec failure. The tail matters
    because a timeout leaves nothing else to look at — and on POSIX
    TimeoutExpired carries BYTES despite text=True, so it must be decoded
    before it is split and sanitized."""
    import mojo.apps.incident.reporter as reporter_module
    from mojo.apps import jobs
    from mojo.apps.edge.services import deploy
    from mojo.apps.jobs.models import Job

    deployment = _arm(SHA_A, [opts.me])
    job_id = jobs.publish(
        func=deploy.DEPLOY_NODE_JOB,
        payload=dict(sha=SHA_A, framework=FRAMEWORK, migrate=True,
                     deployment=str(deployment.pk)),
        channel=CHANNEL)
    timeout = subprocess.TimeoutExpired(
        cmd=["/bin/echo"], timeout=900, output=b"",
        stderr=b"password=timeout-sentinel\ncollecting wheels for numpy\n")
    incidents = mock.Mock(return_value=mock.Mock(pk=1997))
    with th.capture_publishes(_deploy_publish), \
         mock.patch.object(deploy, "deploy_script_argv", return_value=["/bin/echo"]), \
         mock.patch.object(deploy, "_run", side_effect=timeout), \
         mock.patch.object(reporter_module, "report_event", incidents):
        _drain(opts)

    row = Job.objects.get(id=job_id)
    th.assert_eq(row.status, "failed",
                 f"a timed-out script must fail the node job, got {row.status}")
    th.assert_true(incidents.called, "a script timeout must file an incident")
    message = incidents.call_args.args[0]
    th.assert_true("timeout-sentinel" not in message,
                   "raw process output must never enter an incident")

    deployment.refresh_from_db()
    entries = [item for item in (deployment.node_evidence or [])
               if (item.get("detail") or {}).get("phase") == "script_timeout"]
    th.assert_eq(len(entries), 1,
                 f"the timeout must land as durable node evidence, "
                 f"got {deployment.node_evidence!r}")
    tail = (entries[0].get("detail") or {}).get("stderr_tail") or []
    th.assert_true("timeout-sentinel" not in str(tail),
                   f"a credential-shaped stderr line must be redacted, got {tail!r}")
    th.assert_in("[redacted]", tail,
                 f"the credential line must survive as a redaction marker, got {tail!r}")
    th.assert_in("collecting wheels for numpy", tail,
                 f"a benign stderr line must survive decoded and verbatim, got {tail!r}")
    status = deploy.get_status()
    th.assert_eq((status or {}).get("detail"), "script_timeout",
                 f"the lease must carry the fixed timeout phase, got {status!r}")
    deploy.clear_status(deployment.pk)


@th.django_unit_test("deploy_node: a FLEET node's reported failure never touches the lease")
def test_node_fleet_failure_leaves_lease_alone(opts):
    """The invariant the new reporting had to preserve: the canary already
    proved this release, so one fleet node failing is an incident about that
    node — not a failed deploy. Only a migrating node may write the lease."""
    import mojo.apps.incident.reporter as reporter_module
    from mojo.apps import jobs
    from mojo.apps.edge.services import deploy

    deployment = _arm(SHA_A, [opts.me])
    jobs.publish(
        func=deploy.DEPLOY_NODE_JOB,
        payload=dict(sha=SHA_A, framework=FRAMEWORK, migrate=False,
                     deployment=str(deployment.pk)),
        channel=CHANNEL)
    timeout = subprocess.TimeoutExpired(cmd=["/bin/echo"], timeout=900)
    incidents = mock.Mock(return_value=mock.Mock(pk=1997))
    with th.capture_publishes(_deploy_publish) as calls, \
         mock.patch.object(deploy, "deploy_script_argv", return_value=["/bin/echo"]), \
         mock.patch.object(deploy, "_run", side_effect=timeout), \
         mock.patch.object(reporter_module, "report_event", incidents):
        _drain(opts)

    th.assert_true(incidents.called,
                   "a fleet node's timeout must still be visible as an incident")
    th.assert_eq(calls, [],
                 f"a fleet node's failure must publish nothing — no rollback, got {calls!r}")
    status = deploy.get_status()
    th.assert_true(status and status.get("state") == deploy.STATUS_MIGRATING,
                   f"a non-migrating node must not touch the deploy status, got {status!r}")
    deployment.refresh_from_db()
    th.assert_true(deployment.status != "failed",
                   f"one fleet node must not close the whole attempt, "
                   f"got {deployment.status}")
    deploy.clear_status(deployment.pk)


@th.django_unit_test("deploy_node: a non-executable script is refused before it is run")
def test_node_preflight_executability(opts):
    """A shim committed 0644 is the shape of this failure. An explicit path is
    probed up front and named; a BARE command name (the documented
    ["sudo", "-n", ...] argv) must skip the probe entirely — os.access does no
    PATH resolution, so probing it would refuse every configured deploy."""
    import os
    import tempfile

    import mojo.apps.incident.reporter as reporter_module
    from mojo.apps import jobs
    from mojo.apps.edge.services import deploy
    from mojo.apps.jobs.models import Job

    handle, script = tempfile.mkstemp(prefix="deploy-shim-", suffix=".sh")
    os.close(handle)
    os.chmod(script, 0o644)
    try:
        deployment = _arm(SHA_A, [opts.me])
        job_id = jobs.publish(
            func=deploy.DEPLOY_NODE_JOB,
            payload=dict(sha=SHA_A, framework=FRAMEWORK, migrate=True,
                         deployment=str(deployment.pk)),
            channel=CHANNEL)
        ran = []
        incidents = mock.Mock(return_value=mock.Mock(pk=1997))
        with th.capture_publishes(_deploy_publish), \
             mock.patch.object(deploy, "deploy_script_argv", return_value=[script]), \
             mock.patch.object(deploy, "_run",
                               side_effect=lambda argv: ran.append(list(argv))), \
             mock.patch.object(reporter_module, "report_event", incidents):
            _drain(opts)

        th.assert_eq(ran, [],
                     f"a non-executable script must never be run, got {ran!r}")
        th.assert_eq(Job.objects.get(id=job_id).status, "failed",
                     "the refused deploy must fail the node job")
        message = incidents.call_args.args[0]
        th.assert_in("not executable", message,
                     f"the incident must name the refusal, got {message!r}")
        th.assert_in(script, message,
                     f"the incident must name the script, got {message!r}")
        th.assert_in("--chmod=+x", message,
                     f"the incident must carry the cure, got {message!r}")
        deployment.refresh_from_db()
        phases = [(item.get("detail") or {}).get("phase")
                  for item in (deployment.node_evidence or [])]
        th.assert_in("preflight_failed", phases,
                     f"the refusal must land as durable evidence, got {phases!r}")
        deploy.clear_status(deployment.pk)

        # A bare command name carries no path separator: skip the probe.
        sudo_deployment = _arm(SHA_B, [opts.me])
        jobs.publish(
            func=deploy.DEPLOY_NODE_JOB,
            payload=dict(sha=SHA_B, framework=FRAMEWORK, migrate=False,
                         deployment=str(sudo_deployment.pk)),
            channel=CHANNEL)
        with th.capture_publishes(_deploy_publish), \
             mock.patch.object(deploy, "deploy_script_argv",
                               return_value=["sudo", "-n", script]), \
             mock.patch.object(deploy, "_run",
                               side_effect=lambda argv: ran.append(list(argv)) or FakeProc(0)):
            _drain(opts)
        th.assert_eq(len(ran), 1,
                     f"a sudo-shaped argv must pass preflight and reach the "
                     f"script, got {ran!r}")
        th.assert_eq(ran[0][:3], ["sudo", "-n", script],
                     f"the configured argv base must travel unchanged, got {ran!r}")
        deploy.clear_status(sudo_deployment.pk)
    finally:
        os.unlink(script)


@th.django_unit_test("deploy_node: an unconfigured node reports through the same helper")
def test_node_unconfigured_reports_failure(opts):
    """The refusal predates this item; what it never did was leave a trace
    anywhere except the incident — no evidence, and a migrating node's lease
    held `migrating` until its TTL."""
    import mojo.apps.incident.reporter as reporter_module
    from mojo.apps import jobs
    from mojo.apps.edge.services import deploy

    deployment = _arm(SHA_A, [opts.me])
    jobs.publish(
        func=deploy.DEPLOY_NODE_JOB,
        payload=dict(sha=SHA_A, framework=FRAMEWORK, migrate=True,
                     deployment=str(deployment.pk)),
        channel=CHANNEL)
    incidents = mock.Mock(return_value=mock.Mock(pk=1997))
    with th.capture_publishes(_deploy_publish), \
         mock.patch.object(deploy, "deploy_script_argv", return_value=None), \
         mock.patch.object(reporter_module, "report_event", incidents):
        _drain(opts)

    message = incidents.call_args.args[0]
    th.assert_in("EDGE_DEPLOY_SCRIPT", message,
                 f"the incident must still name the missing setting, got {message!r}")
    th.assert_eq(incidents.call_args.kwargs.get("title"),
                 "Edge deploy node unconfigured",
                 f"the unconfigured incident keeps its own title, got {incidents.call_args!r}")
    deployment.refresh_from_db()
    phases = [(item.get("detail") or {}).get("phase")
              for item in (deployment.node_evidence or [])]
    th.assert_in("unconfigured", phases,
                 f"the refusal must land as durable evidence, got {phases!r}")
    th.assert_eq(deployment.status, "failed",
                 f"a migrating node's refusal must close the attempt, "
                 f"got {deployment.status}")
    status = deploy.get_status()
    th.assert_eq((status or {}).get("detail"), "unconfigured",
                 f"the lease must be released as failed, got {status!r}")
    deploy.clear_status(deployment.pk)


# ----------------------------------------------------------------------
# lease expiry vs supersession (maestro #4857)
# ----------------------------------------------------------------------
#
# Production, 2026-09-18: the orchestrate job waited 9 minutes for a worker,
# the canary job never got one, and the 15-minute lease armed at webhook time
# expired mid-canary. The poll loop read the missing lease as "someone else
# took the plane" and recorded the attempt superseded — no incident, no
# failure, no successor, fleet still on the old release.


def _orchestrate_patches(opts, incidents, **extra):
    """The shared patch set: roster, framework pin, fast poll, incident sink."""
    import mojo.apps.incident.reporter as reporter_module
    import mojo.apps.jobs as jobs_module
    from mojo.apps.edge import asyncjobs
    from mojo.apps.edge.services import deploy

    patches = [
        mock.patch.object(jobs_module, "get_runners",
                          return_value=_runners(CANARY_ID, opts.me, FLEET_ID)),
        mock.patch.object(deploy, "resolve_framework_version",
                          return_value=FRAMEWORK),
        mock.patch.object(asyncjobs, "DEPLOY_POLL_INTERVAL", 0.05),
        mock.patch.object(reporter_module, "report_event", incidents),
    ]
    for name, value in extra.items():
        patches.append(mock.patch.object(deploy, name, **value))
    return patches


def _last_transition(deployment):
    deployment.refresh_from_db()
    return (deployment.transitions or [])[-1]


@th.django_unit_test("orchestrate: a lease that expired with NO successor is a failure with an incident, not a supersession")
def test_lease_expired_without_successor_is_failure(opts):
    import contextlib
    import time as _time

    from mojo.apps.edge.services import deploy

    deployment = _arm(SHA_A, [CANARY_ID, opts.me, FLEET_ID])
    _publish_orchestrate(SHA_A, deployment)
    reads = []

    def expired_lease(*args, **kwargs):
        reads.append(1)
        if len(reads) == 1:
            # The pre-flight read: this deploy still owns the lease.
            return dict(state=deploy.STATUS_MIGRATING, sha=SHA_A,
                        deployment=str(deployment.pk))
        return None  # gone — expired or flushed, nobody armed anything

    incidents = mock.Mock(return_value=mock.Mock(pk=4857))
    started = _time.time()
    with contextlib.ExitStack() as stack:
        calls = stack.enter_context(th.capture_publishes(_deploy_publish))
        for patch in _orchestrate_patches(
                opts, incidents,
                canary_timeout=dict(return_value=120),
                get_status=dict(side_effect=expired_lease)):
            stack.enter_context(patch)
        _drain(opts)
    elapsed = _time.time() - started

    th.assert_true(elapsed < 10,
                   f"a lost lease must be reported at once, not after the "
                   f"canary timeout — took {elapsed:.1f}s")
    th.assert_eq(len(_node_calls(calls)), 1,
                 f"only the canary may ever have been told, got {calls!r}")
    chained = [c for c in calls if c.get("func") == deploy.DEPLOY_ORCHESTRATE_JOB]
    th.assert_eq(chained, [],
                 f"nothing took the lease, so there is nothing to chain, got {chained!r}")
    deployment.refresh_from_db()
    th.assert_eq(deployment.status, "failed",
                 f"an expired lease with no successor is a FAILED attempt, "
                 f"got {deployment.status!r}")
    last = _last_transition(deployment)
    th.assert_eq(last["detail"].get("reason"), "lease_expired_mid_canary",
                 f"the failure must name the lease expiry, got {last!r}")
    th.assert_in("may still be", incidents.call_args.args[0],
                 f"mid-canary, the incident must not claim the whole fleet is "
                 f"on the previous release, got {incidents.call_args!r}")
    th.assert_eq((last["detail"].get("diagnosis") or {}).get("waiting_on"), CANARY_ID,
                 f"the diagnosis must say which canary was being waited on, got {last!r}")
    th.assert_true(incidents.called,
                   "a lost lease must file an incident — the fleet is stuck on "
                   "the old release and nothing will retry by itself")
    message = incidents.call_args.args[0]
    th.assert_in("lease expired", message,
                 f"the incident must say the lease expired, got {message!r}")
    th.assert_in("retry", message,
                 f"the incident must tell the operator what to do, got {message!r}")


@th.django_unit_test("orchestrate: the lease is renewed while waiting, so queue delay never counts against the canary")
def test_orchestrator_renews_lease_while_waiting(opts):
    """A lease armed at webhook time with 1s left must survive a 3s canary
    wait: the orchestrator renews it on arrival and on every poll. Before
    #4857 it expired ~1s in and the attempt ended 'superseded'."""
    import contextlib
    import time as _time

    from mojo.apps.edge.services import deploy

    deployment = _arm(SHA_A, [CANARY_ID, opts.me, FLEET_ID])
    _publish_orchestrate(SHA_A, deployment)  # canary remains silent
    # The push armed the lease long ago; the orchestrate job sat in the queue
    # and only 1s of the lease is left when it finally gets a worker.
    deploy.get_client().expire(deploy.STATUS_KEY, 1)
    _time.sleep(0.6)

    incidents = mock.Mock(return_value=mock.Mock(pk=4857))
    with contextlib.ExitStack() as stack:
        calls = stack.enter_context(th.capture_publishes(_deploy_publish))
        for patch in _orchestrate_patches(
                opts, incidents,
                canary_timeout=dict(return_value=3),
                status_ttl=dict(return_value=1)):
            stack.enter_context(patch)
        _drain(opts)

    th.assert_eq(len(_node_calls(calls)), 1,
                 f"a silent canary must leave the fleet untouched, got {calls!r}")
    deployment.refresh_from_db()
    th.assert_eq(deployment.status, "failed",
                 f"a silent canary is a failed attempt, got {deployment.status!r}")
    last = _last_transition(deployment)
    th.assert_eq(last["detail"].get("reason"), "canary_not_proven",
                 f"the lease must have outlived the canary wait — a lease "
                 f"expiry here means it was not renewed, got {last!r}")
    th.assert_true(incidents.called, "a canary timeout must file an incident")
    th.assert_in("did not report", incidents.call_args.args[0],
                 f"the incident must be the canary timeout, not a lease loss, "
                 f"got {incidents.call_args!r}")
    th.assert_eq(deploy.get_status(), None,
                 "the timeout terminal must still clear the status")


@th.django_unit_test("orchestrate: a canary job that never got a worker is named as such in the incident")
def test_canary_never_started_is_diagnosed(opts):
    import contextlib
    import uuid as _uuid

    from mojo.apps.edge.services import deploy
    from mojo.apps.jobs.models import Job

    deployment = _arm(SHA_A, [CANARY_ID, opts.me, FLEET_ID])
    _publish_orchestrate(SHA_A, deployment)
    # The canary's job row as production showed it: expired before execution,
    # attempt 0, never started — every worker on that node was busy.
    canary_job = Job.objects.create(
        id=_uuid.uuid4().hex, channel=CANARY_ID, func=deploy.DEPLOY_NODE_JOB,
        payload={"sha": SHA_A}, status="expired", attempt=0)
    canary_job_id = canary_job.pk  # delete() below clears .pk

    incidents = mock.Mock(return_value=mock.Mock(pk=4857))
    try:
        with contextlib.ExitStack() as stack:
            stack.enter_context(
                th.capture_publishes(_deploy_publish, result=canary_job_id))
            for patch in _orchestrate_patches(
                    opts, incidents, canary_timeout=dict(return_value=1)):
                stack.enter_context(patch)
            _drain(opts)
    finally:
        canary_job.delete()

    th.assert_true(incidents.called, "a canary timeout must file an incident")
    message = incidents.call_args.args[0]
    th.assert_in("never started", message,
                 f"the incident must say the canary job never got a worker, "
                 f"got {message!r}")
    th.assert_in(CANARY_ID, message,
                 f"the incident must name the starved node, got {message!r}")
    last = _last_transition(deployment)
    diagnosis = last["detail"].get("diagnosis") or {}
    th.assert_eq(diagnosis.get("state"), "never_started",
                 f"the durable row must carry the same diagnosis, got {last!r}")
    th.assert_eq(diagnosis.get("canary_job"), canary_job_id,
                 f"the diagnosis must point at the canary job, got {diagnosis!r}")
    th.assert_eq(deploy.get_status(), None,
                 "the timeout terminal must still clear the status")


@th.django_unit_test("orchestrate: coordination that expired before the orchestrator ran is reported, not called superseded")
def test_preflight_expired_coordination_is_failure(opts):
    import contextlib

    from mojo.apps.edge.services import deploy

    deployment = _arm(SHA_A, [CANARY_ID, opts.me, FLEET_ID])
    _publish_orchestrate(SHA_A, deployment)
    # Both keys expired while the job sat in the queue; nobody re-armed.
    deploy.get_client().delete(deploy.TARGET_KEY, deploy.STATUS_KEY)

    incidents = mock.Mock(return_value=mock.Mock(pk=4857))
    with contextlib.ExitStack() as stack:
        calls = stack.enter_context(th.capture_publishes(_deploy_publish))
        for patch in _orchestrate_patches(opts, incidents):
            stack.enter_context(patch)
        _drain(opts)

    th.assert_eq(calls, [], f"no node may be told without a lease, got {calls!r}")
    deployment.refresh_from_db()
    th.assert_eq(deployment.status, "failed",
                 f"expired coordination with no successor is a failure, "
                 f"got {deployment.status!r}")
    th.assert_eq(_last_transition(deployment)["detail"].get("reason"),
                 "lease_expired_before_start",
                 f"the failure must name the expiry, got {_last_transition(deployment)!r}")
    th.assert_true(incidents.called, "expired coordination must file an incident")
    th.assert_in("before the orchestrator", incidents.call_args.args[0],
                 f"the incident must say the lease died before the orchestrator "
                 f"ran, got {incidents.call_args!r}")


@th.django_unit_test("touch_status renews only the lease it owns, and never creates one")
def test_touch_status_is_owner_gated(opts):
    from mojo.apps.edge.services import deploy

    th.assert_eq(deploy.touch_status("nobody"), False,
                 "touching with no lease armed must not create one")
    th.assert_eq(deploy.get_status(), None,
                 "touching with no lease armed must leave the key absent")

    deployment = _arm(SHA_A, [CANARY_ID, opts.me])
    client = deploy.get_client()
    client.expire(deploy.STATUS_KEY, 5)
    th.assert_eq(deploy.touch_status("someone-else"), False,
                 "a foreign deployment must not be able to renew the lease")
    th.assert_true(client.ttl(deploy.STATUS_KEY) <= 5,
                   f"a refused touch must not move the expiry, ttl={client.ttl(deploy.STATUS_KEY)}")
    th.assert_eq(deploy.touch_status(deployment.pk), True,
                 "the owner must be able to renew its own lease")
    th.assert_true(client.ttl(deploy.STATUS_KEY) > 5,
                   f"a renewed lease must carry a full TTL, ttl={client.ttl(deploy.STATUS_KEY)}")
    status = deploy.get_status()
    th.assert_eq((status or {}).get("deployment"), str(deployment.pk),
                 f"renewing must not rewrite the lease body, got {status!r}")
    deploy.clear_status(deployment.pk)


@th.django_unit_test("touch_status extends the target with the lease, and never creates one")
def test_touch_status_extends_target(opts):
    from mojo.apps.edge.services import deploy

    deployment = _arm(SHA_A, [CANARY_ID, opts.me])
    client = deploy.get_client()
    # A push recorded onto this running deploy: the target names another row.
    deploy.set_target(SHA_B, actor="test", deployment_id=str(uuid.uuid4()))
    client.expire(deploy.TARGET_KEY, 5)
    th.assert_eq(deploy.touch_status("someone-else"), False,
                 "a foreign deployment renews nothing")
    th.assert_true(client.ttl(deploy.TARGET_KEY) <= 5,
                   f"a refused touch must not move the target's expiry, ttl={client.ttl(deploy.TARGET_KEY)}")
    th.assert_eq(deploy.touch_status(deployment.pk), True, "the owner renews")
    th.assert_true(client.ttl(deploy.TARGET_KEY) > 5,
                   f"the target must be extended with the lease, ttl={client.ttl(deploy.TARGET_KEY)}")
    th.assert_eq((deploy.get_target() or {}).get("sha"), SHA_B,
                 "extending must not rewrite the target")

    client.delete(deploy.TARGET_KEY)
    th.assert_eq(deploy.touch_status(deployment.pk), True,
                 "a missing target does not stop the lease renewal")
    th.assert_eq(deploy.get_target(), None, "a touch must never create a target")
    deploy.clear_status(deployment.pk)


@th.django_unit_test("orchestration is published on priority only when a live engine consumes it, else on default")
def test_orchestrate_channel_falls_back_to_default(opts):
    import contextlib
    from mojo.apps.edge.services import deploy, platform_deploy

    def resumed_channel(roster):
        deployment = _arm(SHA_A, [CANARY_ID, opts.me])
        deploy.get_client().delete(deploy.STATUS_KEY)  # stranded: target, no lease
        with contextlib.ExitStack() as stack:
            calls = stack.enter_context(th.capture_publishes(_deploy_publish))
            stack.enter_context(mock.patch.object(platform_deploy, "_channel_roster", roster))
            resumed = deploy.resume_stranded_target()
        th.assert_eq(resumed, SHA_A, f"the stranded target must be resumed, got {resumed!r}")
        th.assert_eq(len(calls), 1, f"exactly one orchestrate is published, got {calls!r}")
        deploy.get_client().delete(deploy.TARGET_KEY, deploy.STATUS_KEY)
        deployment.delete()
        return calls[0].get("channel")

    seen = []

    def nobody(channel):
        seen.append(channel)
        return []

    th.assert_eq(resumed_channel(nobody), "default",
                 "with no live engine on priority the deploy must still be "
                 "published where engines listen")
    th.assert_eq(seen, ["priority"], f"the roster asked about is priority's, got {seen}")
    th.assert_eq(resumed_channel(lambda channel: ["some-engine"]), "priority",
                 "with a live engine on priority the deploy rides the reserved channel")

    def broken(channel):
        raise RuntimeError("roster unavailable")

    th.assert_eq(resumed_channel(broken), "default",
                 "an unreadable roster must fall back to the channel that always worked")


@th.django_unit_test("the stale sweep files one incident for a deployment that was never orchestrated, and none for one that was")
def test_sweep_reports_never_orchestrated(opts):
    import datetime
    import mojo.apps.incident.reporter as reporter_module
    from django.utils import timezone
    from mojo.apps.edge.models import PlatformDeployment
    from mojo.apps.edge.services import deploy, platform_deploy

    deploy.get_client().delete(deploy.TARGET_KEY, deploy.STATUS_KEY)
    old = timezone.now() - datetime.timedelta(seconds=deploy.status_ttl() + 600)

    def aged(status):
        row = PlatformDeployment.objects.create(
            sha=SHA_A, actor="test", source="test", request_key=str(uuid.uuid4()),
            frozen_roster=[CANARY_ID, opts.me], transitions=[], status=status)
        PlatformDeployment.objects.filter(pk=row.pk).update(modified=old)
        return row

    never = aged(PlatformDeployment.STATUS_REQUESTED)
    driven = aged(PlatformDeployment.STATUS_CANARY)

    incidents = mock.Mock(return_value=mock.Mock(pk=4857))
    with mock.patch.object(reporter_module, "report_event", incidents):
        platform_deploy.reconcile_stale(verify_fleet=lambda *args, **kwargs: None)
        first = incidents.call_count
        platform_deploy.reconcile_stale(verify_fleet=lambda *args, **kwargs: None)

    for row in (never, driven):
        row.refresh_from_db()
        th.assert_eq(row.status, "unknown",
                     f"an aged-out deployment nobody drives ends unknown, got {row.status!r}")
        th.assert_eq(_last_transition(row)["detail"].get("reason"),
                     "coordination_lease_expired",
                     f"the sweep keeps its own reason, got {_last_transition(row)!r}")
    th.assert_eq(first, 1,
                 f"exactly one incident: the never-orchestrated deployment, got {incidents.call_args_list!r}")
    th.assert_eq(incidents.call_count, 1,
                 "a second sweep must not report the same deployment again")
    th.assert_in("never orchestrated", incidents.call_args.args[0],
                 f"the incident must say no orchestrator ran, got {incidents.call_args!r}")
    th.assert_in("4857", [str(v) for v in (never.links or {}).get("incident_events", [])],
                 f"the incident must be linked on the deployment, got {never.links!r}")
    th.assert_eq((driven.links or {}).get("incident_events", []), [],
                 f"a deployment that reached its canary is not this incident, got {driven.links!r}")
    PlatformDeployment.objects.filter(pk__in=[never.pk, driven.pk]).delete()


# --- #4857 review 84971: a successor named by only one coordination key ------
#
# The expiry report ran whenever either key was missing, before the key that
# was still there was asked who owns the plane. A deploy that had been
# replaced was then reported as "no newer deploy took over".


def _successor(opts):
    """A second deployment row, not armed: the test stores the one key it wants."""
    from mojo.apps.edge.models import PlatformDeployment
    return PlatformDeployment.objects.create(
        sha=SHA_B, actor="test", source="test", request_key=str(uuid.uuid4()),
        frozen_roster=[CANARY_ID, opts.me, FLEET_ID], transitions=[], detail={})


@th.django_unit_test("orchestrate: a newer target with no lease left is a supersession, not an expiry")
def test_preflight_newer_target_without_lease_is_superseded(opts):
    import contextlib

    from mojo.apps.edge.services import deploy

    deployment = _arm(SHA_A, [CANARY_ID, opts.me, FLEET_ID])
    _publish_orchestrate(SHA_A, deployment)
    newer = _successor(opts)
    # A's lease expired in the queue after B was recorded as the next target.
    deploy.get_client().delete(deploy.STATUS_KEY)
    deploy.set_target(SHA_B, actor="test", deployment_id=newer.pk)

    incidents = mock.Mock(return_value=mock.Mock(pk=4857))
    with contextlib.ExitStack() as stack:
        calls = stack.enter_context(th.capture_publishes(_deploy_publish))
        for patch in _orchestrate_patches(opts, incidents):
            stack.enter_context(patch)
        _drain(opts)

    th.assert_true(not incidents.called,
                   f"a deploy replaced by a newer one files no incident, "
                   f"got {incidents.call_args_list!r}")
    deployment.refresh_from_db()
    th.assert_eq(deployment.status, "superseded",
                 f"a newer target means superseded, got {deployment.status!r}")
    th.assert_eq(_node_calls(calls), [],
                 f"the replaced deploy must tell no node, got {calls!r}")
    chained = [c for c in calls if c.get("func") == deploy.DEPLOY_ORCHESTRATE_JOB]
    th.assert_eq([c["payload"].get("deployment") for c in chained], [str(newer.pk)],
                 f"the newer target is started once, got {chained!r}")
    th.assert_eq((deploy.get_status() or {}).get("deployment"), str(newer.pk),
                 "and the lease is armed for the newer deployment")
    deploy.get_client().delete(deploy.TARGET_KEY, deploy.STATUS_KEY)


@th.django_unit_test("orchestrate: a newer deploy's lease with no target left is a supersession, and its lease is kept")
def test_preflight_newer_lease_without_target_is_superseded(opts):
    import contextlib

    from mojo.apps.edge.services import deploy

    deployment = _arm(SHA_A, [CANARY_ID, opts.me, FLEET_ID])
    _publish_orchestrate(SHA_A, deployment)
    newer = _successor(opts)
    # B holds the lease; the target key is gone.
    deploy.arm_status(SHA_B, force=True, deployment_id=newer.pk)
    deploy.get_client().delete(deploy.TARGET_KEY)

    incidents = mock.Mock(return_value=mock.Mock(pk=4857))
    with contextlib.ExitStack() as stack:
        calls = stack.enter_context(th.capture_publishes(_deploy_publish))
        for patch in _orchestrate_patches(opts, incidents):
            stack.enter_context(patch)
        _drain(opts)

    th.assert_true(not incidents.called,
                   f"a deploy whose lease a newer one took files no incident, "
                   f"got {incidents.call_args_list!r}")
    deployment.refresh_from_db()
    th.assert_eq(deployment.status, "superseded",
                 f"a lease held by a newer deploy means superseded, not failed, "
                 f"got {deployment.status!r}")
    th.assert_eq(calls, [], f"nothing is told and nothing is chained, got {calls!r}")
    th.assert_eq((deploy.get_status() or {}).get("deployment"), str(newer.pk),
                 "the newer deployment keeps its lease")
    deploy.get_client().delete(deploy.TARGET_KEY, deploy.STATUS_KEY)


@th.django_unit_test("orchestrate: a lease lost mid-canary with a newer target recorded is a supersession, not an expiry")
def test_mid_canary_lost_lease_with_newer_target_is_superseded(opts):
    import contextlib

    from mojo.apps.edge.services import deploy

    deployment = _arm(SHA_A, [CANARY_ID, opts.me, FLEET_ID])
    _publish_orchestrate(SHA_A, deployment)
    newer = _successor(opts)
    reads = []

    def expired_after_a_push(*args, **kwargs):
        reads.append(1)
        if len(reads) == 1:
            # The pre-flight read: this deploy still owns the lease.
            return dict(state=deploy.STATUS_MIGRATING, sha=SHA_A,
                        deployment=str(deployment.pk))
        if len(reads) == 2:
            # While the canary works, a push records B and A's lease expires.
            deploy.get_client().delete(deploy.STATUS_KEY)
            deploy.set_target(SHA_B, actor="test", deployment_id=newer.pk)
        return None

    incidents = mock.Mock(return_value=mock.Mock(pk=4857))
    with contextlib.ExitStack() as stack:
        calls = stack.enter_context(th.capture_publishes(_deploy_publish))
        for patch in _orchestrate_patches(
                opts, incidents,
                canary_timeout=dict(return_value=120),
                get_status=dict(side_effect=expired_after_a_push)):
            stack.enter_context(patch)
        _drain(opts)

    th.assert_true(not incidents.called,
                   f"a deploy replaced mid-canary files no lease incident, "
                   f"got {incidents.call_args_list!r}")
    deployment.refresh_from_db()
    th.assert_eq(deployment.status, "superseded",
                 f"a newer target means superseded, got {deployment.status!r}")
    chained = [c for c in calls if c.get("func") == deploy.DEPLOY_ORCHESTRATE_JOB]
    th.assert_eq([c["payload"].get("deployment") for c in chained], [str(newer.pk)],
                 f"the newer target is started once, got {chained!r}")
    deploy.get_client().delete(deploy.TARGET_KEY, deploy.STATUS_KEY)


@th.django_unit_test("the stale sweep files one incident when two sweeps hold the same snapshot")
def test_overlapping_sweeps_report_once(opts):
    """Two sweeps read the same requested row before either closes it: the
    cron claim fails open, and a slow sweep can overlap the next minute's.
    The second sweep runs here, whole, inside the first one, after the first
    has taken its snapshot and just before it closes the row."""
    import datetime
    import mojo.apps.incident.reporter as reporter_module
    from django.utils import timezone
    from mojo.apps.edge.models import PlatformDeployment
    from mojo.apps.edge.services import deploy, platform_deploy

    deploy.get_client().delete(deploy.TARGET_KEY, deploy.STATUS_KEY)
    old = timezone.now() - datetime.timedelta(seconds=deploy.status_ttl() + 600)
    never = PlatformDeployment.objects.create(
        sha=SHA_A, actor="test", source="test", request_key=str(uuid.uuid4()),
        frozen_roster=[CANARY_ID, opts.me], transitions=[],
        status=PlatformDeployment.STATUS_REQUESTED)
    PlatformDeployment.objects.filter(pk=never.pk).update(modified=old)

    real_transition = platform_deploy.transition
    inner = []

    def other_sweep_gets_there_first(*args, **kwargs):
        # The first transition asked for is the outer sweep's, on the row it
        # snapshotted. Before it runs, the other sweep runs whole.
        if not inner:
            inner.append(None)  # set first: the other sweep transitions too
            inner[0] = platform_deploy.reconcile_stale(
                verify_fleet=lambda *a, **k: None)
        return real_transition(*args, **kwargs)

    incidents = mock.Mock(return_value=mock.Mock(pk=4857))
    with mock.patch.object(reporter_module, "report_event", incidents), \
         mock.patch.object(platform_deploy, "transition",
                           side_effect=other_sweep_gets_there_first):
        outer = platform_deploy.reconcile_stale(verify_fleet=lambda *a, **k: None)

    th.assert_eq(inner, [1], f"the sweep that got there first closes the row, got {inner!r}")
    th.assert_eq(outer, 0, f"the sweep holding the stale snapshot changes nothing, got {outer!r}")
    th.assert_eq(incidents.call_count, 1,
                 f"two overlapping sweeps must file one incident, got {incidents.call_args_list!r}")
    never.refresh_from_db()
    unknown = [t for t in (never.transitions or []) if t.get("status") == "unknown"]
    th.assert_eq(len(unknown), 1,
                 f"and record one transition to unknown, got {never.transitions!r}")
    PlatformDeployment.objects.filter(pk=never.pk).delete()
