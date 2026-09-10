"""Split out of tests/test_deploy/jobman.py (maestro #1839).

`resolve_root` reads $MOJO_PROJECT_ROOT at call time and takes no injectable
environment, so this test mutates os.environ — process-global, and unsafe
under the parallel default tier.
"""

import os
import shutil
import sys
import tempfile
from unittest import mock

from testit import helpers as th


def _start_fixture():
    base = tempfile.mkdtemp(prefix="testit_jobman_python.")
    root = os.path.join(base, "proj")
    os.makedirs(os.path.join(root, "bin"))
    os.makedirs(os.path.join(root, "var", "logs"))
    os.makedirs(os.path.join(root, "var", "pids"))
    runner = os.path.join(root, "bin", "jobs.py")
    with open(runner, "w") as handle:
        handle.write("#!/usr/bin/env python3\n")
    os.chmod(runner, 0o755)
    return base, root, runner


@th.django_unit_test()
def test_start_launches_runner_through_the_current_absolute_python(opts):
    """The runner's env shebang adds an ambiguous exec chain to Audit.  Jobman
    must invoke the already-running absolute Python directly so the long-lived
    engine has one authoritative executable generation."""
    from mojo.deploy import jobman as jm

    base, root, runner = _start_fixture()
    process = mock.Mock(pid=43210)

    try:
        with mock.patch.object(jm, "probe",
                               return_value=(jm.pidfile(root, "engine"),
                                             False, None, [])), \
                mock.patch.object(jm.subprocess, "Popen",
                                  return_value=process) as popen:
            result = jm.cmd_start(root, runner, "engine")

        th.assert_eq(result, 0, "a valid runner must start successfully")
        th.assert_eq(
            popen.call_args.args[0],
            [sys.executable, runner, "engine", "foreground"],
            f"jobman must launch through its absolute current interpreter, "
            f"not execute the env-shebang runner directly; got "
            f"{popen.call_args.args[0]!r}")
    finally:
        shutil.rmtree(base, ignore_errors=True)


@th.django_unit_test()
def test_start_refuses_a_non_absolute_python_before_spawn(opts):
    """A PATH-resolved interpreter would recreate the exec ambiguity this
    launch contract exists to remove, so fail closed before creating a child."""
    from mojo.deploy import jobman as jm

    base, root, runner = _start_fixture()

    try:
        with mock.patch.object(jm, "probe",
                               return_value=(jm.pidfile(root, "engine"),
                                             False, None, [])), \
                mock.patch.object(jm.sys, "executable", "python3"), \
                mock.patch.object(jm.subprocess, "Popen") as popen:
            result = jm.cmd_start(root, runner, "engine")

        th.assert_eq(result, 1,
                     "a PATH-resolved interpreter must be refused")
        th.assert_true(not popen.called,
                       "jobman must validate Python before spawning anything")
    finally:
        shutil.rmtree(base, ignore_errors=True)


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
