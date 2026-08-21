"""Split out of tests/test_deploy/jobman.py (maestro #1839).

`resolve_root` reads $MOJO_PROJECT_ROOT at call time and takes no injectable
environment, so this test mutates os.environ — process-global, and unsafe
under the parallel default tier.
"""

import os

from testit import helpers as th


@th.django_unit_test()
def test_root_resolution_prefers_flag_then_env_then_cwd(opts):
    from mojo.deploy import jobman as jm

    original = os.environ.get("MOJO_PROJECT_ROOT")
    try:
        os.environ["MOJO_PROJECT_ROOT"] = "/opt/from-env"
        th.assert_eq(jm.resolve_root("/opt/from-flag"), "/opt/from-flag",
                     "--root must win over $MOJO_PROJECT_ROOT")
        th.assert_eq(jm.resolve_root(None), "/opt/from-env",
                     "$MOJO_PROJECT_ROOT must be used when --root is absent")

        os.environ.pop("MOJO_PROJECT_ROOT")
        th.assert_eq(jm.resolve_root(None), os.getcwd(),
                     "with neither --root nor $MOJO_PROJECT_ROOT the working "
                     "directory is the root")
        th.assert_eq(jm.resolve_root("."), os.getcwd(),
                     "the resolved root must be made absolute — the stale-PID "
                     "status line prints this path, and a relative one means "
                     "something different to every reader")
    finally:
        os.environ.pop("MOJO_PROJECT_ROOT", None)
        if original is not None:
            os.environ["MOJO_PROJECT_ROOT"] = original

