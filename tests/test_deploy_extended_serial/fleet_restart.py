"""Automatic fleet configuration restart retains cron launch authority."""

import os
import signal
import tempfile
from unittest import mock

from testit import helpers as th


@th.django_unit_test()
def test_worker_only_config_change_retires_jobs_without_starting_asgi(opts):
    from mojo.deploy import config_sync

    jobs = mock.Mock(return_value=True)
    run = mock.Mock()
    result = config_sync.restart_app(
        {}, False, run_cmd=run, sleep=mock.Mock(),
        request_service_loader=lambda: False, jobs_restart=jobs)

    th.assert_true(result, "a jobs-only node must request config activation")
    th.assert_eq(jobs.call_count, 1,
                 "sealed request-service false must still retire engine and scheduler")
    th.assert_eq(run.call_count, 0,
                 "a worker-only config change must never resurrect ASGI")


@th.django_unit_test()
def test_request_node_config_change_restarts_both_process_planes(opts):
    from mojo.deploy import config_sync

    events = []

    def jobs():
        events.append("jobs")
        return True

    def run(argv, **kwargs):
        events.append(argv)
        return mock.Mock(returncode=0)

    result = config_sync.restart_app(
        {}, False, run_cmd=run, sleep=lambda delay: events.append("jitter"),
        request_service_loader=lambda: True, jobs_restart=jobs)

    th.assert_true(result, "both process planes must accept the restart")
    th.assert_eq(events, ["jitter", "jobs", [
        "systemctl", "--no-block", "restart", "mojo-asgi.service"]],
        "hostname jitter must precede the job retirement and request-service restart")


@th.django_unit_test()
def test_jobs_restart_failure_is_not_reported_as_config_activated(opts):
    from mojo.deploy import config_sync

    run = mock.Mock()
    result = config_sync.restart_app(
        {}, False, run_cmd=run, sleep=mock.Mock(),
        request_service_loader=lambda: False,
        jobs_restart=lambda: False)

    th.assert_true(not result,
                   "a failed job preflight must fail config activation on workers")
    th.assert_eq(run.call_count, 0, "failure must never enable ASGI")


@th.django_unit_test()
def test_custom_service_preserves_restart_and_retires_jobs(opts):
    from mojo.deploy import config_sync

    jobs = mock.Mock(return_value=True)
    run = mock.Mock(return_value=mock.Mock(returncode=0))
    result = config_sync.restart_app(
        {"CONFIG_SYNC_SERVICE": "custom.service"}, False,
        run_cmd=run, sleep=mock.Mock(), jobs_restart=jobs,
        request_service_loader=mock.Mock(side_effect=AssertionError(
            "custom service must retain its independent authority")))
    th.assert_true(result, "custom request services must retain compatibility")
    th.assert_eq(jobs.call_count, 1, "custom service nodes also retire jobs")
    th.assert_eq(run.call_args.args[0][-1], "custom.service",
                 "the selected custom unit must remain unchanged")


@th.django_unit_test()
def test_dry_run_never_retires_jobs_for_any_role(opts):
    from mojo.deploy import config_sync

    for selected in (True, False):
        jobs = mock.Mock()
        run = mock.Mock()
        result = config_sync.restart_app(
            {}, True, run_cmd=run, sleep=mock.Mock(), jobs_restart=jobs,
            request_service_loader=lambda: selected)
        th.assert_true(result, "dry-run must report a safe preview")
        th.assert_eq(jobs.call_count, 0, "dry-run must never signal job processes")
        th.assert_eq(run.call_count, 0, "dry-run must never restart a service")


@th.django_unit_test()
def test_restart_uses_cron_preflight_and_sigterm_without_wait_or_spawn(opts):
    from mojo.deploy import jobman

    events = []

    def run(argv, **kwargs):
        events.append(argv)
        return mock.Mock(returncode=0)

    with mock.patch.object(jobman, "installed_cron", return_value="appu"), \
            mock.patch.object(jobman.os, "geteuid", return_value=0), \
            mock.patch.object(jobman, "cmd_repair", return_value=0) as repair, \
            mock.patch.object(jobman, "exact_processes",
                              side_effect=[["2345"], ["3456"]]) as scan, \
            mock.patch.object(jobman, "_verify_restart_pid", return_value=True), \
            mock.patch.object(jobman.os, "kill") as kill, \
            mock.patch.object(jobman, "wait_gone") as wait, \
            mock.patch.object(jobman.subprocess, "Popen") as spawn:
        result = jobman.request_restart(run_cmd=run)

    th.assert_true(result, "a preflighted fleet must accept graceful retirement")
    repair.assert_called_once_with("/opt/api", candidate="appu", cron_path=None)
    th.assert_eq(scan.call_count, 2, "engine and scheduler must both be inventoried")
    th.assert_eq(kill.call_args_list,
                 [mock.call(2345, signal.SIGTERM), mock.call(3456, signal.SIGTERM)],
                 "both components receive SIGTERM only, never SIGKILL")
    th.assert_eq(wait.call_count, 0, "config-sync cannot wait on its invoking job")
    th.assert_eq(spawn.call_count, 0, "cron must own every replacement launch")
    preflight = events[1]
    th.assert_eq(preflight[1:7], ["-n", "-H", "-u", "appu", "--", "/usr/bin/python3"],
                 "preflight must use the exact declared account and fixed Python")
    th.assert_in("preflight", preflight, "demotion must prove readiness without spawning")
    th.assert_in("-P", preflight, "preflight must use cron's safe module search path")


@th.django_unit_test()
def test_restart_refuses_failed_preconditions_before_signalling(opts):
    from mojo.deploy import jobman

    for failure in ("cron", "repair", "preflight", "scan"):
        def run(argv, **kwargs):
            failed = ((failure == "cron" and "is-active" in argv)
                      or (failure == "preflight" and "preflight" in argv))
            return mock.Mock(returncode=1 if failed else 0)

        with mock.patch.object(jobman, "installed_cron", return_value="appu"), \
                mock.patch.object(jobman.os, "geteuid", return_value=0), \
                mock.patch.object(jobman, "cmd_repair",
                                  return_value=1 if failure == "repair" else 0), \
                mock.patch.object(jobman, "exact_processes",
                                  side_effect=ValueError("ambiguous") if failure == "scan"
                                  else None, return_value=["2345"]), \
                mock.patch.object(jobman.os, "kill") as kill:
            result = jobman.request_restart(run_cmd=run)
        th.assert_true(not result, "%s failure must refuse retirement" % failure)
        th.assert_eq(kill.call_count, 0,
                     "%s failure must leave running processes alone" % failure)


@th.django_unit_test()
def test_restart_reports_signal_denied_and_tolerates_already_exited(opts):
    from mojo.deploy import jobman

    for error, expected in ((PermissionError("denied"), False),
                            (ProcessLookupError("gone"), True)):
        with mock.patch.object(jobman, "installed_cron", return_value="appu"), \
                mock.patch.object(jobman.os, "geteuid", return_value=0), \
                mock.patch.object(jobman, "cmd_repair", return_value=0), \
                mock.patch.object(jobman, "exact_processes", return_value=["2345"]), \
                mock.patch.object(jobman, "_verify_restart_pid", return_value=True), \
                mock.patch.object(jobman.os, "kill", side_effect=error):
            result = jobman.request_restart(
                run_cmd=mock.Mock(return_value=mock.Mock(returncode=0)))
        th.assert_eq(result, expected,
                     "vanished targets are safe; denied signals cannot claim success")


@th.django_unit_test()
def test_missing_cron_is_legacy_noop_not_supervision_proof(opts):
    from mojo.deploy import jobman

    with mock.patch.object(jobman, "installed_cron", return_value=None), \
            mock.patch.object(jobman.os, "kill") as kill:
        run = mock.Mock()
        result = jobman.request_restart(run_cmd=run)
    th.assert_true(result, "no-jobs legacy installations retain config-sync compatibility")
    th.assert_eq(run.call_count, 0, "missing cron cannot authorize a process operation")
    th.assert_eq(kill.call_count, 0, "missing cron never authorizes signals")


@th.django_unit_test()
def test_strict_inventory_refuses_failed_or_unverified_scan(opts):
    from mojo.deploy import jobman

    for code, output in ((2, ""), (0, "1"), (0, "not-a-pid"), (0, "2345")):
        with mock.patch.object(jobman, "_root_process_matches", return_value=False), \
                mock.patch.object(jobman.os, "stat", return_value=mock.Mock()):
            try:
                jobman.exact_processes("/opt/api", "engine", run_cmd=mock.Mock(
                    return_value=mock.Mock(returncode=code, stdout=output)))
            except ValueError:
                pass
            else:
                raise AssertionError("failed or ambiguous scans must never become healthy inventory")


@th.django_unit_test()
def test_exact_inventory_checks_both_absolute_and_relative_runner_identity(opts):
    from mojo.deploy import jobman

    argv = [b"/usr/bin/python3", b"./bin/jobs.py", b"engine", b"foreground"]
    with mock.patch("builtins.open", mock.mock_open(read_data=b"\0".join(argv))), \
            mock.patch.object(jobman.os, "readlink", return_value="/opt/other"):
        th.assert_true(not jobman._root_process_matches(
            "2345", "/opt/api", "/opt/api/bin/jobs.py", "engine"),
            "a relative runner in another checkout must never match")
    argv[1] = b"/opt/api/bin/jobs.py"
    with mock.patch("builtins.open", mock.mock_open(read_data=b"\0".join(argv))):
        th.assert_true(jobman._root_process_matches(
            "2345", "/opt/api", "/opt/api/bin/jobs.py", "engine"),
            "an exact absolute foreground runner must match")


@th.django_unit_test()
def test_installed_cron_rejects_custom_or_unsafe_supervision(opts):
    from mojo.deploy import jobman

    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "3_mojo_jobs")
        for command in ("/opt/api/bin/jobman start", jobman.cron_command("/opt/other")):
            with open(path, "w") as handle:
                handle.write("* * * * * appu %s\n" % command)
            # Test-owned filesystem uses our uid. Ownership checks are exercised
            # separately below; parser checks must reach the command itself.
            original = jobman.os.fstat

            def root_stat(fd):
                found = original(fd)
                values = {name: getattr(found, name) for name in (
                    "st_mode", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")}
                return mock.Mock(st_uid=0, **values)

            with mock.patch.object(jobman.os, "fstat", side_effect=root_stat):
                try:
                    jobman.installed_cron(cron_path=path)
                except ValueError:
                    pass
                else:
                    raise AssertionError("nonstandard cron must never count as managed supervision")
        os.chmod(path, 0o666)
        with mock.patch.object(jobman.os, "fstat", side_effect=root_stat):
            try:
                jobman.installed_cron(cron_path=path)
            except ValueError:
                pass
            else:
                raise AssertionError("writable cron must never authorize restart")


@th.django_unit_test()
def test_installed_cron_accepts_exact_managed_command_and_account(opts):
    from mojo.deploy import jobman

    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "3_mojo_jobs")
        with open(path, "w") as handle:
            handle.write("SHELL=/bin/bash\nPATH=%s\n"
                         "* * * * * appu %s\n" % (
                             jobman.CRON_PATH, jobman.cron_command("/opt/api")))
        original = jobman.os.fstat

        def root_stat(fd):
            found = original(fd)
            values = {name: getattr(found, name) for name in (
                "st_mode", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")}
            return mock.Mock(st_uid=0, **values)

        with mock.patch.object(jobman.os, "fstat", side_effect=root_stat), \
                mock.patch.object(jobman, "_cron_user", return_value="appu") as user:
            th.assert_eq(jobman.installed_cron(cron_path=path), "appu",
                         "the rendered managed cron must prove its declared account")
        user.assert_called_once_with("/opt/api", candidate="appu", cron_path=path)


@th.django_unit_test()
def test_worker_only_retirement_retains_hostname_jitter(opts):
    from mojo.deploy import config_sync
    events = []
    def jobs():
        events.append("jobs")
        return True
    result = config_sync.restart_app({}, False, run_cmd=mock.Mock(),
        sleep=lambda delay: events.append("jitter"),
        request_service_loader=lambda: False, jobs_restart=jobs)
    th.assert_true(result, "Worker activation must accept graceful retirement")
    th.assert_eq(events, ["jitter", "jobs"], "Workers must stagger retirement too")


@th.django_unit_test()
def test_cron_bad_shell_environment_or_newline_cannot_retire_processes(opts):
    from mojo.deploy import jobman

    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "3_mojo_jobs")
        entry = "* * * * * appu %s\n" % jobman.cron_command("/opt/api")
        original = jobman.os.fstat

        def root_stat(fd):
            found = original(fd)
            values = {name: getattr(found, name) for name in (
                "st_mode", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")}
            return mock.Mock(st_uid=0, **values)

        for body in ("SHELL=/bin/false\n" + entry,
                     "SHELL=/bin/bash\nSHELL=/bin/sh\n" + entry,
                     "PATH=/broken\n" + entry,
                     "PATH=%s\nPATH=%s\n%s" % (jobman.CRON_PATH, jobman.CRON_PATH, entry),
                     "SHELL=/bin/bash\r\n" + entry,
                     entry.rstrip("\n")):
            with open(path, "w") as handle:
                handle.write(body)
            with mock.patch.object(jobman.os, "fstat", side_effect=root_stat), \
                    mock.patch.object(jobman.os, "kill") as kill:
                run = mock.Mock()
                th.assert_true(not jobman.request_restart(cron_path=path, run_cmd=run),
                               "cron unable to replace workers must refuse retirement")
            th.assert_eq(kill.call_count, 0,
                         "unsupported cron environment must never signal existing workers")
            th.assert_eq(run.call_count, 0,
                         "unsafe cron must fail before any process operation")
