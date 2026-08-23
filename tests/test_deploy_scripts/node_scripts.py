"""The packaged node scripts, exercised by their shell harnesses.

Each harness under tests/test_deploy_scripts/harness/ runs the REAL packaged
script (mojo/deploy/scripts/*.sh, or `python3 -m mojo.deploy.certbot_sync`) in
a throwaway PROJ_PATH with every external command stubbed onto PATH —
orderings, absences and file placement are asserted there, in shell, where the
subject lives. This module is the testit face: one test per harness, surfacing
the harness's own pass/fail tail when it breaks. Opt-in `slow` since maestro
#2789 (~100s of wall clock); the `bash -n` parse gate stayed default-tier in
tests/test_deploy/scripts_parse.py.

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
def test_update_sh_harness(opts):
    """Orderings and absences of the packaged update.sh: report-before-
    rollback, the engine restart last and under the engine's own user, flock
    modes, SANITY_URL default + override."""
    _assert_harness_green(_run_harness("test_update_sh.sh"), "test_update_sh.sh")


@th.django_unit_test()
def test_post_deploy_sh_harness(opts):
    """The packaged post_deploy.sh pipeline: render into var/deploy, install
    substituted, collision/override policy, node_retired.conf, input
    overrides, self-snapshot, die-loudly."""
    _assert_harness_green(_run_harness("test_post_deploy_sh.sh"),
                          "test_post_deploy_sh.sh")


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
