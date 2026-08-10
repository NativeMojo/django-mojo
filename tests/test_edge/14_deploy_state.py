"""
The deploy coordination state (maestro item #1458, D3).

These run against the checkout's REAL Redis — the NX arm, the Lua
compare-and-set and the TTLs are exactly the properties under test, and a
mocked Redis would prove nothing about them. Keys are cleared in setup and
per-test where armed state matters; the database and Redis are long-lived.
"""
import json
from unittest import mock

from testit import helpers as th
from tests.test_edge._helpers import with_setting

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
    row, _ = platform_deploy.create(SHA_C, actor="test", source="test")
    deploy.set_target(SHA_C, actor="test", deployment_id=row.pk)
    deploy.arm_status(SHA_C, deployment_id=row.pk)

    with mock.patch.object(platform_deploy, "evidence"):
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


@th.django_unit_test("framework version resolution: success, garbage, and failure fail loudly")
def test_resolve_framework_version(opts):
    import requests as requests_lib

    from mojo.apps.edge.services import deploy

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    with mock.patch.object(deploy.requests, "get",
                           return_value=FakeResponse({"info": {"version": "1.2.63"}})):
        th.assert_eq(deploy.resolve_framework_version(), "1.2.63",
                     "the PyPI info.version must be returned verbatim")

    with mock.patch.object(deploy.requests, "get",
                           return_value=FakeResponse({"info": {"version": "1.0; rm -rf /"}})):
        with th.assert_raises(ValueError):
            deploy.resolve_framework_version()

    with mock.patch.object(deploy.requests, "get",
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
