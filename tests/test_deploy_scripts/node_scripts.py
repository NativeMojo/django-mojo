"""The packaged node scripts, exercised by their shell harnesses.

The remaining harnesses exercise certificate sync and provisioning in a
throwaway project with external commands stubbed on PATH. The project-owned
deployment scripts have focused coverage in `bootstrap_regression.py`.

The harnesses live INSIDE this package (not a sibling tests/ directory) so
testit never lists a phantom zero-test module for them.
"""

import os
import subprocess
import sys

from testit import helpers as th

HARNESS_TIMEOUT = 300


def _repo_root():
    import mojo
    return os.path.dirname(os.path.dirname(os.path.abspath(mojo.__file__)))


def _run_harness(name):
    root = _repo_root()
    script = os.path.join(root, "tests", "test_deploy_scripts", "harness", name)
    return subprocess.run(
        ["bash", script], cwd=root, capture_output=True, text=True,
        timeout=HARNESS_TIMEOUT)


def _assert_harness_green(done, name):
    tail = "\n".join((done.stdout or "").splitlines()[-25:])
    th.assert_eq(done.returncode, 0,
                 f"{name} must pass — its own report tail:\n{tail}\n"
                 f"stderr: {done.stderr[-2000:]}")


@th.django_unit_test()
def test_certbot_sync_harness(opts):
    """The certificate plane's gating paths under a poisoned boto3: staging
    invariant, role-aware --renew, dormant-when-unconfigured."""
    _assert_harness_green(_run_harness("test_certbot_sync.sh"),
                          "test_certbot_sync.sh")


@th.django_unit_test()
def test_stage1_sh_harness(opts):
    """The packaged stage1.sh, in the order that makes it correct: untar before
    bootstrap, the version pin after it, var/profile before config_sync, the
    CloudWatch agent configured and enabled before the restart, and a re-run
    that skips an already-installed agent."""
    _assert_harness_green(_run_harness("test_stage1_sh.sh"),
                          "test_stage1_sh.sh")
