"""Split out of tests/test_edge/26_deploy_contract.py (maestro #1839).

These tests write the protected EDGE_FRAMEWORK_VERSION Setting row (via
`_set_pin`) and patch the shared mojo.apps.incident.reporter — process-global,
so unsafe under the parallel default tier. Seams follow the source module:
real Redis, real job rows drained on the private deploy channel, publishes
captured with a predicate.
"""
import os
import tempfile
from unittest import mock
import uuid

from testit import helpers as th

# The deploy plane's declared test channel (JOBS_ALLOWED_CHANNELS refuses any
# other). Shared with the source module, which is safe because both packages
# are serial and execute one module at a time.
CHANNEL = "testit_edge_deploy"


SHA_A = "a" * 40


SHA_B = "b" * 40


PIN_VERSION = "1.11.6"


CANARY_ID = "0000-contract-canary"


FLEET_ID = "zzzz-contract-fleet"


FRAMEWORK_KEY = "EDGE_FRAMEWORK_VERSION"


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


def _node_calls(calls):
    from mojo.apps.edge.services import deploy

    return [c for c in calls if c.get("func") == deploy.DEPLOY_NODE_JOB]


def _drain(opts):
    return th.run_pending_jobs(channel=CHANNEL)


def _set_pin(value):
    """Write (or remove) the protected hold row directly.

    `Setting.save()` refuses every protected key, so this uses the same
    `bulk_create` primitive `system_settings.set_value` does — and deliberately
    skips the validator, which is what lets a test represent a row written
    before the validator existed. The REST write path is covered in
    tests/test_account/test_admin_platform.py.
    """
    from mojo.apps.account.models import Setting

    Setting.objects.filter(key=FRAMEWORK_KEY).delete()
    if value is None:
        return
    row = Setting(key=FRAMEWORK_KEY, group=None, is_secret=False)
    row.set_value(value)
    Setting.objects.bulk_create([row])


def _no_pypi():
    """A requests.get that fails the test if the deploy plane calls it."""
    return mock.Mock(side_effect=AssertionError(
        "PyPI was contacted for a deploy whose framework version was pinned"))


def _arm(sha, roster, actor="test"):
    from mojo.apps.edge.models import PlatformDeployment
    from mojo.apps.edge.services import deploy

    row = PlatformDeployment.objects.create(
        sha=sha, actor=actor, source="test", request_key=str(uuid.uuid4()),
        frozen_roster=list(roster), transitions=[])
    deploy.set_target(sha, actor=actor, deployment_id=row.pk)
    deploy.arm_status(sha, deployment_id=row.pk)
    return row


def _publish_orchestrate(deployment):
    from mojo.apps import jobs
    from mojo.apps.edge.services import deploy

    return jobs.publish(
        func=deploy.DEPLOY_ORCHESTRATE_JOB,
        payload=dict(sha=deployment.sha, deployment=str(deployment.pk)),
        channel=CHANNEL)


def _publish_node(deployment, framework, migrate=True):
    from mojo.apps import jobs
    from mojo.apps.edge.services import deploy

    return jobs.publish(
        func=deploy.DEPLOY_NODE_JOB,
        payload=dict(sha=deployment.sha, framework=framework,
                     migrate=bool(migrate), deployment=str(deployment.pk)),
        channel=CHANNEL)


def _script(directory, name, body, mode=0o755):
    path = os.path.join(directory, name)
    with open(path, "w") as handle:
        handle.write(body)
    os.chmod(path, mode)
    return path


FORK_MISSING_DEPLOYMENT = """#!/bin/bash
# aws/update.sh (forked copy)
while [ $# -gt 0 ]; do
    case "$1" in
        --sha)       SHA="$2"; shift 2 ;;
        --framework) FRAMEWORK="$2"; shift 2 ;;
        --migrate)   MIGRATE=1; shift ;;
        *)           echo "usage" >&2; exit 2 ;;
    esac
done
"""


def _converged(framework, minutes_ago, sha=SHA_B, status=None):
    """One historical deployment row with a controlled `created` ordering."""
    from datetime import timedelta

    from django.utils import timezone

    from mojo.apps.edge.models import PlatformDeployment

    row = PlatformDeployment.objects.create(
        sha=sha, actor="test", source="test", request_key=str(uuid.uuid4()),
        framework_version=framework, frozen_roster=[], transitions=[],
        status=status or PlatformDeployment.STATUS_CONVERGED)
    # `created` is auto_now_add, so the ordering under test has to be written
    # after the fact rather than hoped for from insertion order.
    PlatformDeployment.objects.filter(pk=row.pk).update(
        created=timezone.now() - timedelta(minutes=minutes_ago))
    return row


@th.django_unit_setup()
def setup_contract(opts):
    from mojo.apps.edge.models import PlatformDeployment
    from mojo.apps.edge.services import deploy
    from mojo.apps.jobs.models import Job

    # Long-lived database: delete what this module creates BEFORE creating it.
    _set_pin(None)
    PlatformDeployment.objects.all().delete()
    Job.objects.filter(channel=CHANNEL).delete()
    deploy.get_client().delete(deploy.TARGET_KEY, deploy.STATUS_KEY)
    opts.me = deploy.local_runner_id()


@th.django_unit_test("ordinary nodes use the permanent packaged-script endpoint by default")
def test_default_deploy_endpoint_and_node_type(opts):
    from mojo.apps.edge.services import deploy

    with mock.patch.object(
            deploy.settings, "get_static",
            side_effect=lambda key, default=None, **kwargs: default):
        argv = deploy.deploy_script_argv()
        node_type = deploy.local_node_type()
    th.assert_eq(argv[:4], ["sudo", "-n", "bash", "-c"],
                 "the default transaction must enter through passwordless sudo")
    th.assert_in("mojo.deploy locate update.sh", argv[4],
                 "API projects must not vendor or refresh framework shell")
    th.assert_eq(node_type, "api",
                 "existing nodes remain API until explicitly specialized")


@th.django_unit_test(
    "REGRESSION: an explicit framework pin is installed verbatim, and PyPI is never asked")
def test_pin_is_verbatim_and_never_asks_pypi(opts):
    """Before this, `EDGE_FRAMEWORK_VERSION` did not exist: every deploy
    resolved PyPI's newest release, so an operator could not ship a commit
    without also taking a framework upgrade — and a PyPI outage took the
    deploy plane with it."""
    from mojo.apps.edge.services import deploy

    _set_pin(PIN_VERSION)
    getter = _no_pypi()
    try:
        with mock.patch.object(deploy.requests, "get", getter):
            resolved = deploy.resolve_framework_version()
        th.assert_eq(resolved, PIN_VERSION,
                     f"the pinned version must be installed verbatim, got {resolved!r}")
        th.assert_eq(getter.call_count, 0,
                     "a pinned deploy must make no PyPI request at all")
    finally:
        _set_pin(None)


@th.django_unit_test(
    "a number-shaped pin survives the storage round trip verbatim")
def test_number_shaped_pin_round_trip(opts):
    """`system_settings.get_value` re-parses stored strings as JSON, so a pin
    like "1.12" used to come back as a float the validator refuses — every
    deploy refused while the portal reported the pin as saved. The pin reader
    takes the raw stored string precisely so that cannot happen."""
    from mojo.apps.edge.services import deploy

    for stored, expect in (("1.12", "1.12"), ("2", "2")):
        _set_pin(stored)
        try:
            got = deploy.framework_version_pin()
            th.assert_eq(got, expect,
                         f"stored pin {stored!r} must read back verbatim, got {got!r}")
            getter = _no_pypi()
            with mock.patch.object(deploy.requests, "get", getter):
                resolved = deploy.resolve_framework_version()
            th.assert_eq(resolved, expect,
                         f"a number-shaped pin must deploy verbatim, got {resolved!r}")
        finally:
            _set_pin(None)


@th.django_unit_test(
    "a pre-validator junk hold refuses the deploy, naming the setting and never its value")
def test_junk_pin_refuses_the_deploy(opts):
    """The row can predate the validator (or a future writer), so the read
    path re-validates. What must never happen is the fallback: an operator who
    asked not to take latest silently taking latest."""
    import mojo.apps.incident.reporter as reporter_module
    from mojo.apps.edge.services import deploy

    _set_pin("stable-sentinel")
    try:
        with th.assert_raises(deploy.FrameworkPinError):
            deploy.resolve_framework_version()

        deployment = _arm(SHA_A, [CANARY_ID, opts.me])
        _publish_orchestrate(deployment)
        incidents = mock.Mock(return_value=mock.Mock(pk=1998))
        with th.capture_publishes(_deploy_publish) as calls, \
             mock.patch.object(deploy.requests, "get", _no_pypi()), \
             mock.patch.object(reporter_module, "report_event", incidents):
            _drain(opts)

        th.assert_eq(_node_calls(calls), [],
                     "a deploy with an unusable hold must never reach a node")
        th.assert_true(incidents.called,
                       "an unusable hold must be an incident, not a silent skip")
        message = incidents.call_args.args[0]
        th.assert_in("EDGE_FRAMEWORK_VERSION", message,
                     f"the incident must name the SETTING to fix, got {message!r}")
        th.assert_eq(incidents.call_args.kwargs.get("title"),
                     "Edge deploy framework pin is unusable",
                     f"the pin failure keeps its own title, got {incidents.call_args!r}")
        th.assert_true("stable-sentinel" not in message,
                       "the stored value must never enter an operator-facing incident")
        deployment.refresh_from_db()
        th.assert_true("stable-sentinel" not in str(deployment.transitions),
                       "the stored value must never enter the durable journal")
        th.assert_eq(deployment.status, "failed",
                     f"a refused deploy must close its attempt, got {deployment.status}")
        th.assert_eq(deploy.get_status(), None,
                     "the refused deploy must clear the status for the next push")
    finally:
        _set_pin(None)


@th.django_unit_test("'hold' resolves to the last CONVERGED framework version, or refuses")
def test_hold_uses_last_converged(opts):
    import mojo.apps.incident.reporter as reporter_module
    from mojo.apps.edge.models import PlatformDeployment
    from mojo.apps.edge.services import deploy

    _converged("1.10.4", minutes_ago=90)
    _converged("1.11.2", minutes_ago=30)
    # Newer, but only DISPATCHED to the fleet — never proven on every node.
    _converged("9.9.9", minutes_ago=5, status=PlatformDeployment.STATUS_FLEET)
    _set_pin("HOLD")
    try:
        getter = _no_pypi()
        with mock.patch.object(deploy.requests, "get", getter):
            resolved = deploy.resolve_framework_version()
        th.assert_eq(resolved, "1.11.2",
                     f"hold must take the newest CONVERGED version, got {resolved!r}")
        th.assert_eq(getter.call_count, 0, "a held deploy must not ask PyPI")

        # No converged history at all: refuse, loudly and by name.
        PlatformDeployment.objects.all().delete()
        with th.assert_raises(deploy.FrameworkPinError):
            deploy.resolve_framework_version()

        deployment = _arm(SHA_A, [CANARY_ID, opts.me])
        _publish_orchestrate(deployment)
        incidents = mock.Mock(return_value=mock.Mock(pk=1998))
        with th.capture_publishes(_deploy_publish) as calls, \
             mock.patch.object(deploy.requests, "get", _no_pypi()), \
             mock.patch.object(reporter_module, "report_event", incidents):
            _drain(opts)

        th.assert_eq(_node_calls(calls), [],
                     "a hold with nothing to hold at must never reach a node")
        message = incidents.call_args.args[0]
        th.assert_in("no converged deployment", message,
                     f"the incident must say WHY the hold is unusable, got {message!r}")
        th.assert_in("EDGE_FRAMEWORK_VERSION", message,
                     f"the incident must name the setting to fix, got {message!r}")
    finally:
        _set_pin(None)


@th.django_unit_test("one pinned version per deploy: resolved once, carried in every payload")
def test_pin_resolved_once_and_carried(opts):
    from mojo.apps.edge.services import deploy
    from mojo.apps.jobs.models import Job

    _set_pin(PIN_VERSION)
    try:
        deployment = _arm(SHA_A, [CANARY_ID, opts.me, FLEET_ID])
        deploy.set_status(deploy.STATUS_DEPLOYING, SHA_A,
                          deployment_id=deployment.pk)
        _publish_orchestrate(deployment)
        with th.capture_publishes(_deploy_publish) as calls, \
             mock.patch.object(deploy.requests, "get", _no_pypi()):
            _drain(opts)

        node_calls = _node_calls(calls)
        th.assert_eq([c["channel"] for c in node_calls],
                     [CANARY_ID, FLEET_ID, opts.me],
                     f"canary, fleet, self last — got {node_calls!r}")
        for call in node_calls:
            th.assert_eq(call["payload"]["framework"], PIN_VERSION,
                         f"every node must be told the SAME pinned version, got {call!r}")

        # The node obeys the payload, not its own re-read: a hold changed
        # mid-deploy must not split the fleet across two framework versions.
        _set_pin("2.0.0")
        node_row = _arm(SHA_B, [opts.me])
        job_id = _publish_node(node_row, PIN_VERSION, migrate=False)
        ran = []
        with th.capture_publishes(_deploy_publish), \
             mock.patch.object(deploy, "deploy_script_argv", return_value=["/bin/echo"]), \
             mock.patch.object(deploy, "_run",
                               side_effect=lambda argv: ran.append(list(argv)) or FakeProc(0)):
            _drain(opts)

        th.assert_eq(Job.objects.get(id=job_id).status, "completed",
                     "the node job must complete on the payload's version")
        th.assert_eq(len(ran), 1, f"the update script must have run once, got {ran!r}")
        th.assert_in(PIN_VERSION, ran[0],
                     f"the node must install the version it was TOLD, got {ran[0]!r}")
        th.assert_true("2.0.0" not in ran[0],
                       f"a hold changed mid-deploy must not reach this node, got {ran[0]!r}")
        deploy.clear_status(node_row.pk)
    finally:
        _set_pin(None)
