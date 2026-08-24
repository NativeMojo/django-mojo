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
