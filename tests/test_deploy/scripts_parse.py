"""`bash -n` parse gates on the packaged deploy scripts.

The cheapest possible gate against shipping a wheel whose deploy scripts do
not even parse. The full shell harnesses that exercise these scripts
end-to-end live in tests/test_deploy_scripts/ (opt-in `slow`; maestro #2789).
"""

import os
import subprocess

from testit import helpers as th


def _repo_root():
    import mojo
    return os.path.dirname(os.path.dirname(os.path.abspath(mojo.__file__)))


@th.django_unit_test()
def test_packaged_scripts_parse(opts):
    """`bash -n` on every packaged script — the cheapest possible gate against
    shipping a wheel whose deploy scripts do not even parse."""
    root = _repo_root()
    for rel in ("mojo/deploy/project_scripts/update.sh",
                "mojo/deploy/project_scripts/post_deploy.sh",
                "mojo/deploy/provision/scripts/stage1.sh"):
        path = os.path.join(root, rel)
        th.assert_true(os.path.isfile(path),
                       f"{rel} must ship inside the package")
        done = subprocess.run(["bash", "-n", path],
                              capture_output=True, text=True, timeout=30)
        th.assert_eq(done.returncode, 0,
                     f"bash -n must accept {rel}: {done.stderr}")


@th.django_unit_test()
def test_post_deploy_sweeps_stale_cron_and_retires_the_scheduler_in_order(opts):
    """Structural guard for the #9757 activation steps; the behavioural harness
    lives in tests/test_deploy_scripts (slow tier). The sweep must run after the
    rendered cron.d is installed and before the explicit retirements, and the
    scheduler must be retired only after the API probe accepted the candidate."""
    path = os.path.join(_repo_root(), "mojo/deploy/project_scripts/post_deploy.sh")
    with open(path) as handle:
        source = handle.read()
    th.assert_in("sweep_stale_cron() {", source,
                 "post_deploy.sh must define the stale project cron sweep")
    th.assert_in("retire_scheduler() {", source,
                 "post_deploy.sh must define the scheduler retirement")
    th.assert_in("stop scheduler --grace 2", source,
                 "retirement must stop only the scheduler, with jobman's 2s grace")

    start = source.index("activate_api() {")
    body = source[start:source.index("\n}\n", start)]
    install = body.index('install_directory "$PROJ_PATH/var/deploy/cron.d" "$CRON_ETC"')
    sweep = body.index("\n    sweep_stale_cron\n")
    retired = body.index("\n    remove_retired\n")
    th.assert_true(install < sweep < retired,
                   "the sweep must run after the rendered cron.d is installed "
                   "(so shipped names are exempt) and before remove_retired")
    probe = body.index('probe_api || die')
    retire = body.index("\n    retire_scheduler")
    th.assert_true(probe < retire,
                   "the running scheduler is retired only once the probe accepted "
                   "the candidate — a failed activation keeps the old scheduler")
    th.assert_eq(body.rstrip().splitlines()[-1].strip(), "retire_scheduler",
                 "retirement is the last activation step")
