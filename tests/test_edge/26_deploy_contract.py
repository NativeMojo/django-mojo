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

