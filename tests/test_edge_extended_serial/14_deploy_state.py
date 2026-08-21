"""Split out of tests/test_edge/14_deploy_state.py (maestro #1839).

These tests patch shared production surfaces (mojo.apps.incident.reporter,
mojo.apps.jobs.publish, mojo.apps.jobs.manager.JobManager) and patch.dict
os.environ — process-global, so unsafe under the parallel default tier even
though test_edge itself is serial. Same real-Redis rules as the source module.
"""
import uuid
from unittest import mock

from testit import helpers as th


SHA_A = "a" * 40


SHA_B = "b" * 40


SHA_C = "c1" * 20


@th.django_unit_setup()
def setup_state(opts):
    from mojo.apps.edge.services import deploy
    from mojo.apps.edge.models import PlatformDeployment
    from mojo.apps.jobs.models import Job

    deploy.get_client().delete(deploy.TARGET_KEY, deploy.STATUS_KEY)
    PlatformDeployment.objects.all().delete()
    Job.objects.filter(
        func__in=[deploy.DEPLOY_ORCHESTRATE_JOB, deploy.DEPLOY_NODE_JOB]).delete()


def _node_job(deployment, channel=None):
    from mojo.apps.jobs.adapters import get_adapter
    from mojo.apps.jobs.keys import JobKeys
    from mojo.apps.jobs.models import Job
    from mojo.apps.edge.services import deploy

    # The row belongs to THIS node — that is the only kind the handoff closes,
    # and `payload["runner"]` is what it matches on (see
    # asyncjobs._publish_deploy_node).
    if channel is None:
        channel = deploy.local_runner_id()
    # Long-lived Redis: clear this channel's in-flight set before adding to it.
    get_adapter().delete(JobKeys().processing(channel))
    job = Job.objects.create(
        id=uuid.uuid4().hex, channel=channel, func=deploy.DEPLOY_NODE_JOB,
        status="running", runner_id=channel,
        payload={"sha": SHA_C, "deployment": str(deployment),
                 "runner": channel})
    get_adapter().zadd(JobKeys().processing(channel), {job.id: 1})
    return job


def _inflight(channel, job_id):
    from mojo.apps.jobs.adapters import get_adapter
    from mojo.apps.jobs.keys import JobKeys

    return job_id in (get_adapter().zrangebyscore(
        JobKeys().processing(channel), float("-inf"), float("inf")) or [])


def _armed_attempt(sha=SHA_C):
    """A durable attempt owned by THIS runner, armed on the coordination
    lease, with its node job in flight."""
    from mojo.apps.edge.models import PlatformDeployment
    from mojo.apps.edge.services import deploy

    deploy.get_client().delete(deploy.TARGET_KEY, deploy.STATUS_KEY)
    row = PlatformDeployment.objects.create(
        sha=sha, actor="test", source="test", request_key=str(uuid.uuid4()),
        frozen_roster=[deploy.local_runner_id()], transitions=[])
    deploy.set_target(sha, actor="test", deployment_id=row.pk)
    deploy.arm_status(sha, deployment_id=row.pk)
    return row, _node_job(row.pk)


@th.django_unit_test(
    "deploy_status failure creates an incident with sanitized command evidence")
def test_deploy_status_failure_evidence(opts):
    import os
    import uuid

    from django.conf import settings as django_settings
    from django.core.management import call_command
    import mojo.apps.incident.reporter as reporter_module
    from mojo.apps.edge.models import PlatformDeployment
    from mojo.apps.edge.services import deploy

    runner = deploy.local_runner_id()
    row = PlatformDeployment.objects.create(
        sha=SHA_C, actor="test", source="test", request_key=str(uuid.uuid4()),
        frozen_roster=[runner], transitions=[])
    deploy.set_target(SHA_C, actor="test", deployment_id=row.pk)
    deploy.arm_status(SHA_C, deployment_id=row.pk)

    var_root = os.path.join(django_settings.PROJECT_ROOT, "var")
    path = os.path.join(var_root, "deploy_failure_output")
    os.makedirs(var_root, exist_ok=True)
    with open(path, "w") as stream:
        stream.write("password=deploy-sentinel\n")
        stream.write("FATAL: migration command refused schema drift\n")
    # The rollback target update.sh captured before anything moved.
    with open(os.path.join(var_root, "previous_sha"), "w") as stream:
        stream.write("f" * 40 + "\n")
    with open(os.path.join(var_root, "previous_framework"), "w") as stream:
        stream.write("1.11.6\n")

    incident = mock.Mock(return_value=mock.Mock(pk=9127))
    try:
        with mock.patch.object(reporter_module, "report_event", incident):
            call_command(
                "deploy_status", "set", "failed", sha=SHA_C,
                deployment=str(row.pk), detail="post_deploy (migrate)",
                evidence=True)
    finally:
        for name in ("deploy_failure_output", "previous_sha", "previous_framework"):
            try:
                os.unlink(os.path.join(var_root, name))
            except FileNotFoundError:
                pass

    row.refresh_from_db()
    entry = row.node_evidence[-1]
    detail = entry.get("detail") or {}
    th.assert_eq(detail.get("phase"), "post_deploy_migrate",
                 f"the fixed failure phase must survive, got {detail!r}")
    th.assert_eq(
        detail.get("stderr_tail"),
        ["[redacted]", "FATAL: migration command refused schema drift"],
        f"the durable tail must redact secrets line-by-line, got {detail!r}")
    diagnosis = [item for item in (row.diagnosis or []) if isinstance(item, dict)]
    th.assert_true(diagnosis,
                   "a terminal failure report must land in the diagnosis journal")
    story = diagnosis[0]
    th.assert_eq(story.get("kind"), "failure",
                 f"the first diagnosis entry must be the failure, got {story!r}")
    story_detail = story.get("detail") or {}
    th.assert_eq(story_detail.get("rollback_to"),
                 {"sha": "f" * 40, "framework": "1.11.6"},
                 f"the diagnosis must carry the rollback target, got {story_detail!r}")
    th.assert_eq(
        story_detail.get("stderr_tail"),
        ["[redacted]", "FATAL: migration command refused schema drift"],
        f"the diagnosis tail must stay redacted line-by-line, got {story_detail!r}")
    th.assert_true(incident.called,
                   "a script-reported canary failure must create an incident")
    message = incident.call_args.args[0]
    th.assert_in("phase=post_deploy_migrate", message,
                 f"the incident must name the safe phase, got {message!r}")
    th.assert_true("deploy-sentinel" not in message,
                   "raw command output must never enter the incident message")
    th.assert_in("9127", (row.links or {}).get("incident_events", []),
                 f"the deployment must link its incident, got {row.links!r}")
    deploy.clear_status(row.pk)


@th.django_unit_test(
    "deploy_status: a rollback report on an already-failed row appends an outcome")
def test_deploy_status_rollback_report_appends_outcome(opts):
    """update.sh reports twice on a failed migrate: the failure itself, then how
    the rollback went. The second report used to vanish (the row was already
    failed, node_evidence is latest-per-runner). It must append an outcome-kind
    diagnosis entry with its own tail, leaving the first entry and the runner's
    evidence untouched."""
    import os
    import uuid

    from django.conf import settings as django_settings
    from django.core.management import call_command
    import mojo.apps.incident.reporter as reporter_module
    from mojo.apps.edge.models import PlatformDeployment
    from mojo.apps.edge.services import deploy

    runner = deploy.local_runner_id()
    row = PlatformDeployment.objects.create(
        sha=SHA_C, actor="test", source="test", request_key=str(uuid.uuid4()),
        frozen_roster=[runner], transitions=[])
    deploy.set_target(SHA_C, actor="test", deployment_id=row.pk)
    deploy.arm_status(SHA_C, deployment_id=row.pk)

    var_root = os.path.join(django_settings.PROJECT_ROOT, "var")
    os.makedirs(var_root, exist_ok=True)
    incident = mock.Mock(return_value=mock.Mock(pk=9128))
    try:
        with open(os.path.join(var_root, "deploy_failure_output"), "w") as stream:
            stream.write("ERROR: relation already exists\n")
        with mock.patch.object(reporter_module, "report_event", incident):
            call_command(
                "deploy_status", "set", "failed", sha=SHA_C,
                deployment=str(row.pk), detail="post_deploy (migrate)",
                evidence=True)
        row.refresh_from_db()
        evidence_before = list(row.node_evidence)
        first_entry = dict((row.diagnosis or [{}])[0])

        with open(os.path.join(var_root, "deploy_failure_output"), "w") as stream:
            stream.write("mv: cannot restore previous release\n")
        with mock.patch.object(reporter_module, "report_event", incident):
            call_command(
                "deploy_status", "set", "failed", sha=SHA_C,
                deployment=str(row.pk), detail="rollback failed",
                evidence=True)
    finally:
        try:
            os.unlink(os.path.join(var_root, "deploy_failure_output"))
        except FileNotFoundError:
            pass

    row.refresh_from_db()
    th.assert_eq(row.node_evidence, evidence_before,
                 "the rollback report must not disturb the runner's evidence")
    diagnosis = [item for item in (row.diagnosis or []) if isinstance(item, dict)]
    th.assert_eq(len(diagnosis), 2,
                 f"the rollback outcome must append one entry, got {diagnosis!r}")
    th.assert_eq(diagnosis[0], first_entry,
                 "the original failure entry must be untouched")
    outcome = diagnosis[-1]
    th.assert_eq(outcome.get("kind"), "outcome",
                 f"a rollback report is an outcome, got {outcome!r}")
    outcome_detail = outcome.get("detail") or {}
    th.assert_eq(outcome_detail.get("phase"), "rollback_failed",
                 f"the outcome must carry the fixed phase, got {outcome_detail!r}")
    th.assert_eq(outcome_detail.get("stderr_tail"),
                 ["mv: cannot restore previous release"],
                 f"the outcome must carry its own tail, got {outcome_detail!r}")
    deploy.clear_status(row.pk)


@th.django_unit_test(
    "deploy_warning files and links a fixed-phase incident without veto power")
def test_deploy_warning_incident(opts):
    import uuid

    from django.core.management import call_command
    import mojo.apps.incident.reporter as reporter_module
    from mojo.apps.edge.models import PlatformDeployment
    from mojo.apps.edge.services import deploy

    runner = deploy.local_runner_id()
    row = PlatformDeployment.objects.create(
        sha=SHA_C, actor="test", source="test", request_key=str(uuid.uuid4()),
        frozen_roster=[runner], transitions=[])
    deploy.arm_status(SHA_C, deployment_id=row.pk)

    incident = mock.Mock(return_value=mock.Mock(pk=9131))
    with mock.patch.object(reporter_module, "report_event", incident):
        call_command("deploy_warning", "timers")

    row.refresh_from_db()
    th.assert_true(incident.called,
                   "an auxiliary deployment failure must create an incident")
    message = incident.call_args.args[0]
    th.assert_in("phase=timers", message,
                 f"the incident must carry only the fixed warning phase, got {message!r}")
    th.assert_eq(incident.call_args.kwargs.get("level"), 5,
                 f"a non-veto warning must be level 5, got {incident.call_args!r}")
    th.assert_in("9131", (row.links or {}).get("incident_events", []),
                 f"the deployment must link its warning incident, got {row.links!r}")

    # The command deliberately swallows an incident-system outage. The shell
    # may warn that reporting was unavailable, but the healthy app deploy must
    # never become non-zero because its incident recorder is down.
    with mock.patch.object(
            reporter_module, "report_event", side_effect=RuntimeError("offline")):
        call_command("deploy_warning", "cron")
    deploy.clear_status(row.pk)


@th.django_unit_test("deploy_status persists proof before exposing canary success")
def test_deploy_status_proof_precedes_success(opts):
    import os
    import uuid

    from django.core.management import call_command
    from django.core.management.base import CommandError
    from mojo.apps.edge.models import PlatformDeployment
    from mojo.apps.edge.services import deploy, platform_deploy, readiness

    runner = deploy.local_runner_id()
    row = PlatformDeployment.objects.create(
        sha=SHA_C, actor="test", source="test", request_key=str(uuid.uuid4()),
        frozen_roster=[runner], transitions=[])
    deploy.set_target(SHA_C, actor="test", deployment_id=row.pk)
    deploy.arm_status(SHA_C, deployment_id=row.pk)

    with mock.patch.dict(os.environ, {"MOJO_DEPLOY_IDENTITY_READY": "2"}), \
         mock.patch.object(
             readiness, "local_node_proof",
             side_effect=RuntimeError("proof failed")):
        with th.assert_raises(CommandError):
            call_command(
                "deploy_status", "set", "deploying", sha=SHA_C,
                deployment=str(row.pk))
    status = deploy.get_status()
    th.assert_eq(status["state"], deploy.STATUS_MIGRATING,
                 "proof failure announced canary success to the orchestrator")

    order = []
    matching = {
        "node_id": "test", "platform_sha": SHA_C,
        "platform_deployment": str(row.pk),
    }
    with mock.patch.dict(os.environ, {"MOJO_DEPLOY_IDENTITY_READY": "2"}), \
         mock.patch.object(
            readiness, "local_node_proof", return_value=matching), \
         mock.patch.object(
             platform_deploy, "evidence",
             side_effect=lambda *a, **k: order.append("proof") or True), \
         mock.patch.object(
             deploy, "set_status",
             side_effect=lambda *a, **k: order.append("status") or True), \
         mock.patch.object(deploy, "resume_stranded_target") as resume, \
         mock.patch("mojo.apps.jobs.publish") as publish:
        call_command(
            "deploy_status", "set", "deploying", sha=SHA_C,
            deployment=str(row.pk))
    th.assert_eq(order, ["proof", "status"],
                 f"success escaped before durable proof: {order}")
    th.assert_eq(resume.call_count, 0,
                 "callback-time code resumed a successor before self-stop")
    th.assert_eq(publish.call_count, 0,
                 "callback-time code published work before self-stop")
    row.refresh_from_db()
    th.assert_eq(row.status, "verified",
                 f"one-runner v2 callback must record terminal intent: {row.status}")
    deploy.clear_status(row.pk)


@th.django_unit_test("deploy_status v2 refuses a stale manifest through its own UUID lease")
def test_deploy_status_v2_identity_mismatch(opts):
    import os
    import uuid

    from django.core.management import call_command
    from django.core.management.base import CommandError
    from mojo.apps.edge.models import PlatformDeployment
    from mojo.apps.edge.services import deploy, platform_deploy, readiness

    runner = deploy.local_runner_id()
    row = PlatformDeployment.objects.create(
        sha=SHA_C, actor="test", source="test", request_key=str(uuid.uuid4()),
        frozen_roster=[runner], transitions=[])
    deploy.set_target(SHA_C, actor="test", deployment_id=row.pk)
    deploy.arm_status(SHA_C, deployment_id=row.pk)
    stale = {
        "node_id": "test", "platform_sha": "d" * 40,
        "platform_deployment": str(uuid.uuid4()),
    }

    with mock.patch.dict(os.environ, {"MOJO_DEPLOY_IDENTITY_READY": "2"}), \
         mock.patch.object(readiness, "local_node_proof", return_value=stale):
        with th.assert_raises(CommandError):
            call_command(
                "deploy_status", "set", "deploying", sha=SHA_C,
                deployment=str(row.pk))

    status = deploy.get_status()
    th.assert_eq((status or {}).get("state"), deploy.STATUS_FAILED,
                 f"a v2 identity mismatch must fail its exact lease, got {status!r}")
    th.assert_eq((status or {}).get("deployment"), str(row.pk),
                 f"the mismatch touched a foreign lease: {status!r}")
    row.refresh_from_db()
    th.assert_eq(row.status, "failed",
                 f"the mismatched attempt must close failed, got {row.status}")
    entry = row.node_evidence[-1]
    th.assert_eq(entry.get("proof"), {},
                 f"stale identity must be detail, never durable proof: {entry!r}")
    th.assert_eq((entry.get("detail") or {}).get("reason"), "identity_mismatch",
                 f"the bounded refusal reason was lost: {entry!r}")
    deploy.clear_status(row.pk)


@th.django_unit_test("a `failed` report closes this node's deploy job failed")
def test_deploy_status_failed_closes_the_node_job(opts):
    from django.core.management import call_command
    from mojo.apps.edge.services import deploy

    row, job = _armed_attempt()
    try:
        with mock.patch("mojo.apps.incident.reporter.report_event",
                        mock.Mock(return_value=mock.Mock(pk=1))):
            call_command("deploy_status", "set", "failed", sha=SHA_C,
                         deployment=str(row.pk), detail="post_deploy")

        job.refresh_from_db()
        th.assert_eq(job.status, "failed",
                     f"a failed deploy closes its node job failed, got "
                     f"{job.status!r}")
        th.assert_true(not _inflight(job.channel, job.id),
                       "a failed deploy releases its lease too")
    finally:
        deploy.clear_status(row.pk)


@th.django_unit_test("a handoff failure never fails the deploy report")
def test_deploy_status_handoff_failure_is_swallowed(opts):
    from django.core.management import call_command
    from mojo.apps.jobs.manager import JobManager
    from mojo.apps.edge.services import deploy, platform_deploy

    row, job = _armed_attempt()
    try:
        with mock.patch.object(JobManager, "release_inflight",
                               side_effect=RuntimeError("redis is down")):
            with mock.patch.object(platform_deploy, "evidence"):
                call_command("deploy_status", "set", "deploying", sha=SHA_C,
                             deployment=str(row.pk))

        status = deploy.get_status() or {}
        th.assert_eq(status.get("state"), deploy.STATUS_DEPLOYING,
                     f"the deploy report is the command's job and must still "
                     f"have applied, got {status!r}")
    finally:
        deploy.clear_status(row.pk)

