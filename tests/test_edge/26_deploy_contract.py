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


@th.django_unit_setup()
def setup_contract(opts):
    from mojo.apps.edge.models import PlatformDeployment
    from mojo.apps.edge.services import deploy
    from mojo.apps.jobs.models import Job

    # Long-lived database: delete what this module creates BEFORE creating it.
    PlatformDeployment.objects.all().delete()
    Job.objects.filter(channel=CHANNEL).delete()
    deploy.get_client().delete(deploy.TARGET_KEY, deploy.STATUS_KEY)
    opts.me = deploy.local_runner_id()


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
                ("shim", deploy.DEPLOY_CONTRACT, "")),
            "shim_import.py": (
                "#!/usr/bin/env python3\nfrom mojo.deploy.certbot_sync import main\n",
                ("shim", deploy.DEPLOY_CONTRACT, "")),
            # A wrapper that forwards argv wholesale never inspects the flags,
            # so a --sha in its own usage comment proves nothing about it.
            "forwarder.sh": (
                '#!/bin/bash\n# usage: update.sh --sha <hex> --framework <v>\n'
                'exec /opt/api/aws/real_update.sh "$@"\n',
                ("unknown", None, "forwarder")),
            "fork_current.sh": (
                FORK_WITH_EVERY_FLAG, ("inferred", deploy.DEPLOY_CONTRACT, "")),
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

        refused = ("declared.sh", "declared_old.sh", "fork_stale.sh")
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
    th.assert_eq(deploy.DEPLOY_CONTRACT, 2,
                 "atomic identity readiness is node-script contract v2")

