"""
The deploy coordination state (maestro item #1458, D3).

These run against the checkout's REAL Redis — the NX arm, the Lua
compare-and-set and the TTLs are exactly the properties under test, and a
mocked Redis would prove nothing about them. Keys are cleared in setup and
per-test where armed state matters; the database and Redis are long-lived.
"""
import json
import uuid
from unittest import mock

from testit import helpers as th
from tests.test_edge._helpers import declare_edge_runner, with_setting

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


@th.django_unit_test("target round-trips and carries a TTL")
def test_target_roundtrip(opts):
    from mojo.apps.edge.services import deploy

    deploy.set_target(SHA_A, actor="github:tester")
    target = deploy.get_target()
    th.assert_eq(target["sha"], SHA_A, f"target sha must round-trip, got {target!r}")
    th.assert_eq(target["actor"], "github:tester",
                 f"target actor must round-trip, got {target!r}")
    ttl = deploy.get_client().ttl(deploy.TARGET_KEY)
    th.assert_true(ttl > 0,
                   f"the target key must expire (TTL is load-bearing), ttl={ttl}")


@th.django_unit_test("arming is SET NX — a second same-moment deploy records nothing")
def test_arm_is_nx(opts):
    from mojo.apps.edge.services import deploy

    deploy.clear_status()
    th.assert_true(deploy.arm_status(SHA_A), "first arm must land")
    th.assert_true(not deploy.arm_status(SHA_B),
                   "second arm must be refused while one deploy is armed (NX)")
    status = deploy.get_status()
    th.assert_eq(status["sha"], SHA_A,
                 f"the armed deploy must still be the first one, got {status!r}")
    ttl = deploy.get_client().ttl(deploy.STATUS_KEY)
    th.assert_true(ttl > 0, f"the status key must expire, ttl={ttl}")
    th.assert_true(deploy.arm_status(SHA_B, force=True),
                   "the chain re-arm (force=True) must replace the status")


@th.django_unit_test("arming re-arms over a terminal failed lease, never over a live one")
def test_arm_over_failed_lease(opts):
    """A `failed` lease describes a deploy that is OVER. Refusing the next
    deploy for the rest of the status TTL is how one broken release used to
    swallow every push behind it. `migrating`/`deploying` stay unstealable —
    that is the actual invariant, and it is about live deploys."""
    from mojo.apps.edge.services import deploy

    deploy.clear_status()
    th.assert_true(deploy.arm_status(SHA_A), "the first arm must land on an empty key")
    th.assert_true(not deploy.arm_status(SHA_B),
                   "a migrating lease must never be stolen")

    th.assert_true(deploy.set_status(deploy.STATUS_DEPLOYING, SHA_A),
                   "the armed deploy's terminal write must apply")
    th.assert_true(not deploy.arm_status(SHA_B),
                   "a deploying lease is still a live deploy and must not be stolen")

    th.assert_true(deploy.set_status(deploy.STATUS_FAILED, SHA_A),
                   "the failing deploy's terminal write must apply")
    th.assert_true(deploy.arm_status(SHA_B),
                   "the next deploy must re-arm over a terminal failed lease "
                   "rather than wait out its TTL")
    status = deploy.get_status()
    th.assert_eq(status["sha"], SHA_B,
                 f"the re-arm must belong to the new deploy, got {status!r}")
    th.assert_eq(status["state"], deploy.STATUS_MIGRATING,
                 f"the re-armed lease must start at migrating, got {status!r}")
    ttl = deploy.get_client().ttl(deploy.STATUS_KEY)
    th.assert_true(ttl > 0, f"the re-armed lease must still expire, ttl={ttl}")
    deploy.clear_status()


@th.django_unit_test("terminal writes are compare-and-set on the stamped SHA")
def test_terminal_cas(opts):
    from mojo.apps.edge.services import deploy

    deploy.clear_status()
    deploy.arm_status(SHA_A)
    th.assert_true(not deploy.set_status(deploy.STATUS_DEPLOYING, SHA_B),
                   "a terminal write for a SHA that is not armed must be ignored")
    status = deploy.get_status()
    th.assert_eq(status["state"], deploy.STATUS_MIGRATING,
                 f"the ignored write must not change the state, got {status!r}")

    th.assert_true(deploy.set_status(deploy.STATUS_DEPLOYING, SHA_A, detail="ok"),
                   "the armed deploy's own terminal write must apply")
    status = deploy.get_status()
    th.assert_eq(status["state"], deploy.STATUS_DEPLOYING,
                 f"terminal state must land, got {status!r}")

    # A superseded deploy (chain re-arm) makes the OLD sha's writer a ghost.
    deploy.arm_status(SHA_B, force=True)
    th.assert_true(not deploy.set_status(deploy.STATUS_FAILED, SHA_A),
                   "a ghost writer from a superseded deploy must be ignored")
    status = deploy.get_status()
    th.assert_eq(status["sha"], SHA_B,
                 f"the superseding deploy's status must survive, got {status!r}")


@th.django_unit_test("set_status refuses a non-terminal state")
def test_terminal_states_only(opts):
    from mojo.apps.edge.services import deploy

    with th.assert_raises(ValueError):
        deploy.set_status(deploy.STATUS_MIGRATING, SHA_A)


@th.django_unit_test("an expired status key does not wedge the next deploy")
def test_expiry_unwedges(opts):
    from mojo.apps.edge.services import deploy

    deploy.clear_status()
    deploy.arm_status(SHA_A)
    # Simulate the TTL firing (the canary died hard, nobody cleared it).
    deploy.get_client().delete(deploy.STATUS_KEY)
    th.assert_true(deploy.arm_status(SHA_B),
                   "after expiry the next deploy must arm cleanly")
    deploy.clear_status()


@th.django_unit_test("request_deploy: uniform receiver rule — one orchestrate per armed deploy")
def test_request_deploy_rule(opts):
    from mojo.apps.edge.services import deploy
    from mojo.apps.jobs.models import Job

    deploy.get_client().delete(deploy.TARGET_KEY, deploy.STATUS_KEY)
    Job.objects.filter(func=deploy.DEPLOY_ORCHESTRATE_JOB).delete()
    declare_edge_runner()

    th.assert_true(deploy.request_deploy(SHA_A, actor="github:one"),
                   "the first request must start a deploy")
    th.assert_true(not deploy.request_deploy(SHA_B, actor="github:two"),
                   "a request mid-deploy must record the target and start nothing")

    target = deploy.get_target()
    th.assert_eq(target["sha"], SHA_B,
                 f"the mid-deploy push must overwrite the target (last wins), got {target!r}")
    status = deploy.get_status()
    th.assert_eq(status["sha"], SHA_A,
                 f"the armed deploy must still be the first one, got {status!r}")

    rows = Job.objects.filter(func=deploy.DEPLOY_ORCHESTRATE_JOB)
    th.assert_eq(rows.count(), 1,
                 f"exactly one orchestrate job may exist, got {rows.count()}")
    job = rows.first()
    th.assert_eq(job.max_retries, 0,
                 f"the orchestrate job must publish max_retries=0, got {job.max_retries}")
    th.assert_true(job.expires_at is not None,
                   "the orchestrate job must carry an expiry")
    deploy.clear_status(status["deployment"])


@th.django_unit_test("deploy_status command: set applies, get reads back, superseded exits 3")
def test_deploy_status_command(opts):
    import io

    from django.core.management import call_command
    from mojo.apps.edge.services import deploy, platform_deploy

    deploy.get_client().delete(deploy.TARGET_KEY, deploy.STATUS_KEY)
    # The row is owned by THIS runner, so the command's evidence write lands
    # for real instead of behind a process-global patch of
    # platform_deploy.evidence (item #2558).
    from mojo.apps.edge.models import PlatformDeployment
    row = PlatformDeployment.objects.create(
        sha=SHA_C, actor="test", source="test", request_key=str(uuid.uuid4()),
        frozen_roster=[deploy.local_runner_id()], transitions=[])
    deploy.set_target(SHA_C, actor="test", deployment_id=row.pk)
    deploy.arm_status(SHA_C, deployment_id=row.pk)

    with_setting(
        "EDGE_NODE_ID", "edge-deploy-status-test",
        lambda: call_command(
            "deploy_status", "set", "deploying", sha=SHA_C,
            deployment=str(row.pk)))
    out = io.StringIO()
    call_command("deploy_status", "get", stdout=out)
    state = json.loads(out.getvalue())
    th.assert_eq(state["status"]["state"], deploy.STATUS_DEPLOYING,
                 f"get must read back the applied state, got {state!r}")
    th.assert_eq(state["target"]["sha"], SHA_C,
                 f"get must include the target, got {state!r}")

    # Supersede the deploy; the old SHA's writer must exit 3, not 0 or 1.
    other, _ = platform_deploy.create(SHA_B, actor="test", source="test")
    deploy.arm_status(SHA_B, force=True, deployment_id=other.pk)
    try:
        call_command(
            "deploy_status", "set", "failed", sha=SHA_C,
            deployment=str(row.pk))
        raise AssertionError("a superseded set must not exit 0")
    except SystemExit as err:
        th.assert_eq(err.code, 3,
                     f"superseded set must exit 3 (distinct from usage errors), got {err.code}")
    deploy.clear_status(other.pk)


@th.django_unit_test(
    "deploy_status command: one legacy empty-UUID lease can finish the upgrade")
def test_deploy_status_legacy_upgrade_bridge(opts):
    import uuid

    from django.core.management import call_command
    from django.core.management.base import CommandError
    from mojo.apps.edge.models import PlatformDeployment
    from mojo.apps.edge.services import deploy

    deploy.get_client().delete(deploy.TARGET_KEY, deploy.STATUS_KEY)
    deploy.arm_status(SHA_A)

    call_command("deploy_status", "set", "deploying", sha=SHA_A)
    status = deploy.get_status()
    th.assert_eq(
        status["state"], deploy.STATUS_DEPLOYING,
        f"the 1.9 updater's empty-UUID lease must finish once, got {status!r}")
    th.assert_eq(
        status["deployment"], "",
        f"the compatibility write must not invent UUID ownership, got {status!r}")

    # Contract v1 already carried UUID argv, but wrote the legacy identity
    # pair only AFTER this callback. The new command may release a multi-node
    # canary, but it must not turn that stale observation into proof.
    deploy.get_client().delete(deploy.STATUS_KEY)
    runner = deploy.local_runner_id()
    row = PlatformDeployment.objects.create(
        sha=SHA_C, actor="test", source="test", request_key=str(uuid.uuid4()),
        frozen_roster=[runner], transitions=[])
    deploy.arm_status(SHA_C, deployment_id=row.pk)
    call_command(
        "deploy_status", "set", "deploying", sha=SHA_C,
        deployment=str(row.pk))
    row.refresh_from_db()
    th.assert_eq(row.status, "fleet",
                 f"a one-runner v1 bridge must await restarted proof, got {row.status}")
    entry = row.node_evidence[-1]
    th.assert_eq(entry.get("state"), "identity_pending",
                 f"the predecessor callback lost its bridge state: {entry!r}")
    th.assert_eq(entry.get("proof"), {},
                 f"a pre-identity v1 observation became false proof: {entry!r}")
    th.assert_eq((entry.get("detail") or {}).get("reason"),
                 "legacy_script_identity_order",
                 f"the bridge reason was not durable: {entry!r}")
    deploy.clear_status(row.pk)

    deployment_id = "11111111-1111-4111-8111-111111111111"
    deploy.arm_status(SHA_B, force=True, deployment_id=deployment_id)
    with th.assert_raises(CommandError):
        call_command("deploy_status", "set", "deploying", sha=SHA_B)
    status = deploy.get_status()
    th.assert_eq(
        status["state"], deploy.STATUS_MIGRATING,
        f"a UUID-owned lease must still refuse an unstamped callback, got {status!r}")
    deploy.clear_status(deployment_id)


@th.django_unit_test("framework version resolution: success, garbage, and failure fail loudly")
def test_resolve_framework_version(opts):
    """The UNSET branch of the hold — still the common case, and still the one
    that fails loudly rather than letting each node resolve latest on its own.

    `framework_version_pin` is mocked rather than written to its row: the row
    is process-global and the Admin write-path tests own it in another package,
    so mocking is what keeps "unset takes the PyPI path" deterministic. The
    unset SYNONYMS are asserted here against the real validator, and the pinned
    branches live in 26_deploy_contract.py."""
    import requests as requests_lib

    from mojo.apps.edge import settings_validators
    from mojo.apps.edge.services import deploy

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    for word in ("", "  ", "latest", "LATEST", "none", "auto"):
        th.assert_eq(
            settings_validators.framework_pin("EDGE_FRAMEWORK_VERSION", word), "",
            f"{word!r} must resolve to an UNSET hold, i.e. the PyPI path")

    with mock.patch.object(deploy, "framework_version_pin", return_value=""), \
         mock.patch.object(deploy.requests, "get",
                           return_value=FakeResponse({"info": {"version": "1.2.63"}})):
        th.assert_eq(deploy.resolve_framework_version(), "1.2.63",
                     "the PyPI info.version must be returned verbatim")

    with mock.patch.object(deploy, "framework_version_pin", return_value=""), \
         mock.patch.object(deploy.requests, "get",
                           return_value=FakeResponse({"info": {"version": "1.0; rm -rf /"}})):
        with th.assert_raises(ValueError):
            deploy.resolve_framework_version()

    with mock.patch.object(deploy, "framework_version_pin", return_value=""), \
         mock.patch.object(deploy.requests, "get",
                           side_effect=requests_lib.ConnectionError("pypi down")):
        with th.assert_raises(requests_lib.RequestException):
            deploy.resolve_framework_version()


@th.django_unit_test("sha validation refuses the zero sha and non-hex input")
def test_sha_validation(opts):
    from mojo.apps.edge.services import deploy

    th.assert_true(deploy.is_valid_sha(SHA_A), "a full hex sha must validate")
    th.assert_true(deploy.is_valid_sha("abc1234"), "a short sha must validate")
    th.assert_true(not deploy.is_valid_sha("0" * 40),
                   "the branch-deletion zero sha must be refused")
    th.assert_true(not deploy.is_valid_sha("main"),
                   "a branch name is not a sha and must be refused")
    th.assert_true(not deploy.is_valid_sha("abc123; rm -rf /"),
                   "shell metacharacters must be refused")


# ---------------------------------------------------------------------------
# the node job's handoff (maestro item #2246)
# ---------------------------------------------------------------------------
#
# The script is about to stop the engine that is running this deployment's
# `deploy_node` job. Whatever the command reports about the DEPLOY, the JOB is
# over — and nothing else is coming to say so, because the thing that would
# have is exactly the process being killed.


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


@th.django_unit_test("a `deploying` report closes this node's deploy job")
def test_deploy_status_deploying_closes_the_node_job(opts):
    from django.core.management import call_command
    from mojo.apps.edge.services import deploy, platform_deploy

    row, job = _armed_attempt()
    try:
        # The attempt is owned by this runner, so the command's evidence
        # write lands for real — no patch of platform_deploy.evidence
        # (item #2558).
        call_command("deploy_status", "set", "deploying", sha=SHA_C,
                     deployment=str(row.pk))

        job.refresh_from_db()
        th.assert_eq(job.status, "completed",
                     f"the node job must be closed by the report that "
                     f"precedes the engine's death, got {job.status!r}")
        th.assert_eq((job.metadata or {}).get("handoff"), True,
                     f"the closure must be marked as a handoff, got "
                     f"{job.metadata!r}")
        th.assert_true(not _inflight(job.channel, job.id),
                       "the in-flight lease must not outlive the engine")
    finally:
        deploy.clear_status(row.pk)


@th.django_unit_test("a superseded (exit 3) report still closes this node's job")
def test_deploy_status_superseded_closes_the_node_job(opts):
    """Exit 3 says the REPORT is stale. The run that produced it is still
    over, and its engine is still about to be killed — leaving the job row
    `running` behind a lease nobody owns."""
    from django.core.management import call_command
    from mojo.apps.edge.models import PlatformDeployment
    from mojo.apps.edge.services import deploy

    row, job = _armed_attempt()
    other = PlatformDeployment.objects.create(
        sha=SHA_B, actor="test", source="test", request_key=str(uuid.uuid4()),
        frozen_roster=[deploy.local_runner_id()], transitions=[])
    deploy.arm_status(SHA_B, force=True, deployment_id=other.pk)
    try:
        call_command("deploy_status", "set", "deploying", sha=SHA_C,
                     deployment=str(row.pk))
        raise AssertionError("a superseded set must exit 3")
    except SystemExit as err:
        th.assert_eq(err.code, 3,
                     f"a superseded set must exit 3, got {err.code}")
    finally:
        deploy.clear_status(other.pk)

    job.refresh_from_db()
    th.assert_eq(job.status, "completed",
                 f"a superseded report still ends this node's job, got "
                 f"{job.status!r}")
    th.assert_true(not _inflight(job.channel, job.id),
                   "a superseded report still releases the lease")


@th.django_unit_test("the handoff verb closes the job and touches nothing else")
def test_deploy_status_handoff_verb(opts):
    """The tail of every successful update, including the fleet runs that
    deliberately write no status at all."""
    from django.core.management import call_command
    from mojo.apps.edge.services import deploy

    row, job = _armed_attempt()
    try:
        before = deploy.get_status()
        call_command("deploy_status", "handoff", deployment=str(row.pk))

        job.refresh_from_db()
        th.assert_eq(job.status, "completed",
                     f"the handoff verb must close the node job, got "
                     f"{job.status!r}")
        th.assert_true(not _inflight(job.channel, job.id),
                       "the handoff verb must release the in-flight lease")
        th.assert_eq(deploy.get_status(), before,
                     "the handoff verb must not touch the coordination lease "
                     "— a fleet run reaches it having reported nothing, and "
                     "must keep reporting nothing")
        row.refresh_from_db()
        th.assert_eq(list(row.node_evidence or []), [],
                     f"the handoff verb writes no evidence, got "
                     f"{row.node_evidence!r}")
        th.assert_eq(list(row.transitions or []), [],
                     f"the handoff verb records no transition, got "
                     f"{row.transitions!r}")
    finally:
        deploy.clear_status(row.pk)


@th.django_unit_test("deploy_status set carries the node's phase timings, or none")
def test_deploy_status_phases(opts):
    """`--phases` is optional in both directions: a node script that predates
    it passes nothing, and this command must keep working — that is why
    update.sh probes for the flag before passing it."""
    import os

    from django.conf import settings as django_settings
    from django.core.management import call_command
    from mojo.apps.edge.services import deploy, platform_deploy

    var_root = os.path.join(django_settings.PROJECT_ROOT, "var", "deploy")
    os.makedirs(var_root, exist_ok=True)
    path = os.path.join(var_root, "phase_timings")
    with open(path, "w") as stream:
        stream.write("deploy git_sync 1200 ms\n")
        stream.write("deploy post_deploy 41230 ms\n")
        stream.write("deploy nonsense not-a-number ms\n")

    row, _job = _armed_attempt()
    try:
        call_command("deploy_status", "set", "deploying", sha=SHA_C,
                     deployment=str(row.pk),
                     phases="var/deploy/phase_timings")
        row.refresh_from_db()
        th.assert_eq(
            [item["phase"] for item in (row.detail.get("phases") or [])],
            ["git_sync", "post_deploy"],
            f"the node's timings must reach the durable row, and the "
            f"malformed line must not, got {row.detail!r}")
    finally:
        deploy.clear_status(row.pk)

    second, _second_job = _armed_attempt()
    try:
        call_command("deploy_status", "set", "deploying", sha=SHA_C,
                     deployment=str(second.pk))
        second.refresh_from_db()
        th.assert_true("phases" not in (second.detail or {}),
                       f"an older node script passes no --phases and must "
                       f"still report normally, got {second.detail!r}")
    finally:
        deploy.clear_status(second.pk)
        os.unlink(path)

    third, _third_job = _armed_attempt()
    try:
        call_command("deploy_status", "set", "deploying", sha=SHA_C,
                     deployment=str(third.pk),
                     phases="var/deploy/does-not-exist")
        third.refresh_from_db()
        th.assert_true("phases" not in (third.detail or {}),
                       f"an unreadable timings file is no timings, never a "
                       f"failed callback, got {third.detail!r}")
    finally:
        deploy.clear_status(third.pk)


@th.django_unit_test("a timed-out update terminates and reaps its process group")
def test_update_timeout_reaps_process_group(opts):
    import signal
    import subprocess

    from mojo.apps.edge.services import deploy

    process = mock.Mock(pid=4321, returncode=-signal.SIGKILL)
    process.communicate.side_effect = [
        subprocess.TimeoutExpired(["update"], 1, stderr="first"),
        subprocess.TimeoutExpired(["update"], 1, stderr="second"),
        ("out", "err"),
    ]
    called = {}
    killed = []

    def popen(argv, **kwargs):
        called.update(kwargs)
        return process

    with th.assert_raises(subprocess.TimeoutExpired):
        deploy._run(
            ["update"], popen=popen,
            kill_group=lambda pid, sig: killed.append((pid, sig)), timeout=1)

    kwargs = called
    th.assert_eq(kwargs.get("start_new_session"), True,
                 "the update must own a process group")
    th.assert_eq(kwargs["env"].get("MOJO_DEPLOY_PARENT_STATUS"), "1",
                 "new parents must suppress the predecessor callback bridge")
    th.assert_eq(
        process.communicate.call_args_list[1].kwargs.get("timeout"),
        deploy.ROLLBACK_GRACE_SECONDS,
        "TERM-triggered rollback needs a realistic bounded recovery window")
    th.assert_true(deploy.ROLLBACK_GRACE_SECONDS >= 600,
                   "dependency restore and service recovery can exceed five minutes")
    th.assert_eq(
        killed,
        [(4321, signal.SIGTERM), (4321, signal.SIGKILL)],
        "timeout must TERM, grace, KILL, then reap the same group")
