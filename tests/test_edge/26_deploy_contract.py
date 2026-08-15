"""
The framework version hold and the node-script contract (maestro item #1998).

Two guards that both answer "what exactly is this deploy about to install, and
can the node it lands on even run it":

- **`EDGE_FRAMEWORK_VERSION`** — the operator's hold. Unset keeps the old
  behavior (newest published release); a version pins it verbatim with NO PyPI
  request; `hold` freezes the fleet at the last CONVERGED framework version.
  An unusable hold refuses the deploy rather than silently taking latest —
  latest is precisely what the operator asked not to have.
- **the node-script contract** — `deploy_node` READS the script it is about to
  exec and refuses only what is provably behind: a fork that declares an older
  contract, or one that parses argv without this contract's flags. Everything
  it cannot read confidently proceeds; the guard exists to catch a stale fork,
  not to invent a new way for a deploy to fail.

Seams follow 15_deploy_orchestrate: real Redis, real job rows drained on a
private channel, `deploy._run` and `mojo.apps.jobs` mocked, publishes captured
with a predicate so parallel modules' traffic flows through untouched.
"""
import os
import tempfile
from unittest import mock
import uuid

from testit import helpers as th

# The deploy plane's declared test channel (JOBS_ALLOWED_CHANNELS refuses any
# other). Shared with 15_deploy_orchestrate, which is safe because test_edge is
# a serial package and its files execute one at a time, in filename order.
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


# A fork taken before the deployment UUID existed: it parses argv, so it will
# happily accept --sha and --framework and then reject or ignore --deployment.
# That is the failure this contract guard exists for — the deploy dies later as
# "the canary never reported", with nothing naming the cause.
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

FORK_WITH_EVERY_FLAG = FORK_MISSING_DEPLOYMENT.replace(
    "        --migrate)",
    "        --deployment) DEPLOYMENT=\"$2\"; shift 2 ;;\n        --migrate)")


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


@th.django_unit_test("the hold normalizes its accepted forms and refuses everything else")
def test_pin_validation(opts):
    from mojo.apps.edge import settings_validators

    validate = settings_validators.framework_pin
    for value in ("", "  ", "latest", "LATEST", "none", "auto", None):
        th.assert_eq(validate(FRAMEWORK_KEY, value), "",
                     f"{value!r} means 'newest published release', i.e. unset")
    for value in ("hold", "HOLD", " Hold "):
        th.assert_eq(validate(FRAMEWORK_KEY, value), "hold",
                     f"{value!r} must normalize to the hold sentinel")
    th.assert_eq(validate(FRAMEWORK_KEY, " 1.11.6 "), "1.11.6",
                 "a published version must be stored stripped and verbatim")
    th.assert_eq(validate(FRAMEWORK_KEY, "1.0.0RC1"), "1.0.0rc1",
                 "PEP 440 comparison is case-insensitive; storage normalizes")
    for value in ("stable", "v1.2.3", "1.0; rm -rf /", "latest-1", 7, {"a": 1}):
        error = None
        try:
            validate(FRAMEWORK_KEY, value)
        except ValueError as err:
            error = err
        th.assert_true(error is not None,
                       f"{value!r} is not a version, a hold, or unset — it must be refused")
        if isinstance(value, str):
            th.assert_in("'hold'", str(error),
                         f"the refusal must name the accepted forms, got {error!r}")


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


@th.django_unit_test(
    "REGRESSION: a stale fork of the update script is refused BEFORE it is run")
def test_stale_fork_refused_before_run(opts):
    """The shipped `update.sh` moves with every `pip install`, but a project
    that FORKED it does not — and a fork frozen before `--deployment` existed
    fails in the worst possible way: the framework execs it, it refuses the
    argv (or silently drops the flag), and the deploy dies minutes later as
    "the canary did not report" with nothing anywhere naming the real cause.
    `deploy_node` now READS the script first and refuses by name."""
    import mojo.apps.incident.reporter as reporter_module
    from mojo.apps.edge.services import deploy
    from mojo.apps.jobs.models import Job

    with tempfile.TemporaryDirectory() as tmp:
        script = _script(tmp, "update.sh", FORK_MISSING_DEPLOYMENT)
        deployment = _arm(SHA_A, [opts.me])
        job_id = _publish_node(deployment, PIN_VERSION, migrate=True)
        ran = []
        incidents = mock.Mock(return_value=mock.Mock(pk=1998))
        with th.capture_publishes(_deploy_publish), \
             mock.patch.object(deploy, "deploy_script_argv", return_value=[script]), \
             mock.patch.object(deploy, "_run",
                               side_effect=lambda argv: ran.append(list(argv))), \
             mock.patch.object(reporter_module, "report_event", incidents):
            _drain(opts)

        th.assert_eq(ran, [],
                     f"a stale fork must never be executed, got {ran!r}")
        th.assert_eq(Job.objects.get(id=job_id).status, "failed",
                     "the refused deploy must fail the node job, not pass quietly")
        th.assert_true(incidents.called,
                       "the refusal must be an incident, not a silent skip")
        th.assert_eq(incidents.call_args.kwargs.get("title"),
                     "Edge deploy node script contract mismatch",
                     f"the contract refusal keeps its own title, got {incidents.call_args!r}")
        th.assert_eq(incidents.call_args.kwargs.get("level"), 7,
                     f"a refused deploy is a level-7 incident, got {incidents.call_args!r}")
        message = incidents.call_args.args[0]
        th.assert_in("EDGE_DEPLOY_SCRIPT", message,
                     f"the incident must name the setting that points at the fork, got {message!r}")
        th.assert_in("stale fork", message,
                     f"the incident must say WHAT is wrong, got {message!r}")
        deploy.clear_status(deployment.pk)


@th.django_unit_test("the contract ladder reads a script and refuses only what is provably behind")
def test_script_contract_ladder(opts):
    from mojo.apps.edge.services import deploy

    with tempfile.TemporaryDirectory() as tmp:
        cases = {
            # An explicit declaration is the top rung and settles it outright.
            "declared.sh": (
                "#!/bin/bash\n# mojo-deploy-contract: 1\necho hi\n",
                ("declared", 1, "")),
            "declared_old.sh": (
                "#!/bin/bash\n# mojo-deploy-contract: 0\necho hi\n",
                ("declared", 0, "")),
            # A shim delegates to the packaged body, so it CANNOT drift: the
            # contract is whatever the installed framework ships.
            "shim_locate.sh": (
                '#!/bin/bash\ntarget="$(python3 -m mojo.deploy locate update.sh)"\n'
                'exec bash "$target" "$@"\n',
                ("shim", 1, "")),
            "shim_import.py": (
                "#!/usr/bin/env python3\nfrom mojo.deploy.certbot_sync import main\n",
                ("shim", 1, "")),
            # A wrapper that forwards argv wholesale never inspects the flags,
            # so a --sha in its own usage comment proves nothing about it.
            "forwarder.sh": (
                '#!/bin/bash\n# usage: update.sh --sha <hex> --framework <v>\n'
                'exec /opt/api/aws/real_update.sh "$@"\n',
                ("unknown", None, "forwarder")),
            "fork_current.sh": (FORK_WITH_EVERY_FLAG, ("inferred", 1, "")),
            "fork_stale.sh": (FORK_MISSING_DEPLOYMENT, ("stale", 0, "--deployment")),
            "wrapper.sh": (
                "#!/bin/bash\nsudo systemctl restart api\n",
                ("unknown", None, "not_argv_literal")),
        }
        for name, (body, expected) in cases.items():
            path = _script(tmp, name, body)
            result = deploy.script_contract([path])
            th.assert_eq(result, expected,
                         f"{name} must read as {expected!r}, got {result!r}")

        refused = ("declared_old.sh", "fork_stale.sh")
        for name, (body, expected) in cases.items():
            allowed = deploy.contract_ok(expected[0], expected[1])
            th.assert_eq(allowed, name not in refused,
                         f"{name} ({expected[0]}) must "
                         f"{'proceed' if name not in refused else 'be refused'}")

        missing = deploy.script_contract([os.path.join(tmp, "gone.sh")])
        th.assert_eq(missing, ("unknown", None, "unresolved_path"),
                     f"a path that is not a file must proceed, got {missing!r}")
        th.assert_true(deploy.contract_ok(*missing[:2]),
                       "an unresolvable path must never be the reason a deploy is refused")

        # A sudo-shaped argv resolves the LAST element that is a real file.
        sudo_argv = ["sudo", "-n", os.path.join(tmp, "declared.sh")]
        th.assert_eq(deploy.script_contract(sudo_argv), ("declared", 1, ""),
                     "the documented sudo argv must still resolve its script")

        unreadable = _script(tmp, "locked.sh", "#!/bin/bash\n# mojo-deploy-contract: 1\n")
        os.chmod(unreadable, 0o000)
        if not os.access(unreadable, os.R_OK):  # a root-run suite can read it anyway
            result = deploy.script_contract([unreadable])
            th.assert_eq(result, ("unknown", None, "unreadable"),
                         f"an unreadable script must proceed, not refuse, got {result!r}")
            th.assert_true(deploy.contract_ok(*result[:2]),
                           "a script this guard cannot read must never fail a deploy")
        os.chmod(unreadable, 0o644)


@th.django_unit_test("a contract refusal reports on every surface, exactly like every other refusal")
def test_contract_refusal_is_fully_reported(opts):
    import mojo.apps.incident.reporter as reporter_module
    from mojo.apps.edge.services import deploy

    with tempfile.TemporaryDirectory() as tmp:
        script = _script(tmp, "update.sh", FORK_MISSING_DEPLOYMENT)
        deployment = _arm(SHA_A, [opts.me])
        _publish_node(deployment, PIN_VERSION, migrate=True)
        incidents = mock.Mock(return_value=mock.Mock(pk=1998))
        with th.capture_publishes(_deploy_publish) as calls, \
             mock.patch.object(deploy, "deploy_script_argv", return_value=[script]), \
             mock.patch.object(deploy, "_run", side_effect=AssertionError("script ran")), \
             mock.patch.object(reporter_module, "report_event", incidents):
            _drain(opts)

        message = incidents.call_args.args[0]
        th.assert_in("v0", message,
                     f"the incident must carry the contract the script speaks, got {message!r}")
        th.assert_in(f"v{deploy.DEPLOY_CONTRACT}", message,
                     f"the incident must carry the contract required, got {message!r}")
        th.assert_eq(calls, [],
                     f"a refused node must publish nothing, got {calls!r}")

        deployment.refresh_from_db()
        entries = [item for item in (deployment.node_evidence or [])
                   if (item.get("detail") or {}).get("phase") == "contract_mismatch"]
        th.assert_eq(len(entries), 1,
                     f"the refusal must land as durable evidence, got {deployment.node_evidence!r}")
        detail = entries[0].get("detail") or {}
        th.assert_eq(detail.get("contract"), 0,
                     f"evidence must record the contract the script speaks, got {detail!r}")
        th.assert_eq(detail.get("required"), deploy.DEPLOY_CONTRACT,
                     f"evidence must record the contract required, got {detail!r}")
        th.assert_eq(detail.get("missing"), "--deployment",
                     f"evidence must name the flag the fork lacks, got {detail!r}")
        th.assert_eq(deployment.status, "failed",
                     f"a migrating node's refusal must close the attempt, got {deployment.status}")
        status = deploy.get_status()
        th.assert_eq((status or {}).get("state"), deploy.STATUS_FAILED,
                     f"the migrating node must release the lease as failed, got {status!r}")
        th.assert_eq((status or {}).get("detail"), "contract_mismatch",
                     f"the lease must carry the fixed contract phase, got {status!r}")
        deploy.clear_status(deployment.pk)


@th.django_unit_test("the packaged update.sh declares exactly the contract this framework requires")
def test_packaged_script_declares_current_contract(opts):
    """The marker in the shipped script and `DEPLOY_CONTRACT` are two halves of
    one number. If they ever drift, the framework refuses its OWN script."""
    import mojo

    from mojo.apps.edge.services import deploy

    root = os.path.dirname(os.path.dirname(os.path.abspath(mojo.__file__)))
    path = os.path.join(root, "mojo", "deploy", "scripts", "update.sh")
    th.assert_true(os.path.isfile(path), f"the packaged update.sh must ship: {path}")
    verdict, contract, reason = deploy.script_contract([path])
    th.assert_eq(verdict, "declared",
                 f"the packaged script must DECLARE its contract, got {verdict!r}/{reason!r}")
    th.assert_eq(contract, deploy.DEPLOY_CONTRACT,
                 f"update.sh declares contract {contract}, the framework requires "
                 f"{deploy.DEPLOY_CONTRACT} — bump the marker and the constant together")
    th.assert_true(deploy.contract_ok(verdict, contract),
                   "the framework must never refuse the script it ships")
