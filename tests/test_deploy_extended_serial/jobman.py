"""Split out of tests/test_deploy/jobman.py (maestro #1839).

`resolve_root` reads $MOJO_PROJECT_ROOT at call time and takes no injectable
environment, so this test mutates os.environ — process-global, and unsafe
under the parallel default tier.
"""

import os
import shutil
import stat
import sys
import tempfile
from types import SimpleNamespace
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
def test_repair_verb_hands_jobman_files_to_the_cron_account_without_starting(opts):
    """The deploy handoff must repair root-poisoned pid/log files without
    spawning a replacement in the retiring engine's Audit session."""
    from mojo.deploy import jobman

    root = "/opt/api"
    existing = {
        os.path.join(root, "var", "pids", "job_engine.pid"),
        os.path.join(root, "var", "pids", "job_scheduler.pid"),
        os.path.join(root, "var", "logs", "job_engine.log"),
        os.path.join(root, "var", "logs", "job_scheduler.log"),
        os.path.join(root, "var", "logs", "jobman.log"),
    }
    owners = {path: 0 for path in existing}

    def fake_lstat(path):
        if path not in owners:
            raise FileNotFoundError(path)
        return SimpleNamespace(
            st_mode=stat.S_IFREG | 0o644, st_nlink=1,
            st_uid=owners[path], st_gid=77)

    def fake_lchown(path, uid, gid):
        th.assert_eq(gid, 77, "repair must preserve the existing group")
        owners[path] = uid

    cron_path = "/etc/cron.d/3_mojo_jobs"
    with mock.patch.object(jobman.os, "geteuid", return_value=0), \
            mock.patch.object(jobman.app_user, "cron_app_user",
                              return_value="appu"), \
            mock.patch.object(jobman.app_user, "resolve_app_user",
                              return_value="appu") as resolve, \
            mock.patch.object(jobman.pwd, "getpwnam",
                              return_value=SimpleNamespace(pw_uid=1234)), \
            mock.patch.object(jobman.os, "lstat", side_effect=fake_lstat), \
            mock.patch.object(jobman.os, "lchown", side_effect=fake_lchown), \
            mock.patch.object(jobman, "cmd_start") as start:
        result = jobman.main([
            "repair", "--root", root, "--app-user", "appu",
            "--cron-path", cron_path])

    th.assert_eq(result, 0, "a complete ownership repair must succeed")
    th.assert_eq(set(owners.values()), {1234},
                 "every jobman-owned pid/log file must return to cron")
    th.assert_true(not start.called,
                   "repair must never spawn a deploy-session replacement")
    resolve.assert_called_once_with(
        root, candidate="appu", cron_path=cron_path)


@th.django_unit_test()
def test_stop_reports_failure_when_a_process_survives_sigkill(opts):
    """The detached logger must not report success after EPERM or a stubborn
    process leaves a root-owned component running."""
    from mojo.deploy import jobman

    with mock.patch.object(
            jobman, "probe",
            return_value=("/opt/api/var/pids/job_engine.pid",
                          True, "4321", ["4321"])), \
            mock.patch.object(jobman, "signal_pids"), \
            mock.patch.object(jobman, "wait_gone",
                              side_effect=[["4321"], ["4321"]]):
        result = jobman.cmd_stop(
            "/opt/api", "/opt/api/bin/jobs.py", "engine", grace=0)

    th.assert_eq(result, 1,
                 "a surviving process must make the bounded stop fail")


@th.django_unit_test()
def test_stop_never_signals_a_live_pidfile_target_without_command_proof(opts):
    """Pidfiles are application-writable. Root stop must not let one redirect
    TERM/KILL to PID 1 or an unrelated privileged daemon."""
    from mojo.deploy import jobman

    with mock.patch.object(
            jobman, "probe",
            return_value=("/opt/api/var/pids/job_engine.pid",
                          True, "1", [])), \
            mock.patch.object(jobman, "signal_pids") as signal_pids:
        result = jobman.cmd_stop(
            "/opt/api", "/opt/api/bin/jobs.py", "engine", grace=0)

    th.assert_eq(result, 0,
                 "an unmatched pidfile is stale state, not a signal target")
    th.assert_true(not signal_pids.called,
                   "an unproven pidfile PID must never receive a signal")


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
