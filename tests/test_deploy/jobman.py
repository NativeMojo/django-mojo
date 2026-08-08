"""mojo.deploy.jobman — the node's foreground engine/scheduler control.

Two layers, for two different reasons.

`status_lines()` is a pure function, so the ten semantic cases are ten cheap
assertions on exact line lists. They are a 1:1 port of the shell harness that
pinned this behaviour in django-mojo-skeleton (`tests/deploy/test_jobman_sh.sh`,
maestro item #1600), including the regression that started it: on a healthy node
the pidfile's own PID is always in the pgrep result, and reporting it as an
"extra instance" made every healthy node look like it had a duplicate engine.

The end-to-end tests then spawn the real `python3 -m mojo.deploy.jobman` with
`pgrep` and `ps` stubbed on PATH — every process state is a file rather than a
real fork. They own what the pure tests cannot see: dispatch, component order,
the return code, and an EMPTY STDERR. mverify's `check_node.py` runs
`jobman status 2>&1` and greps the first lines, so a stray warning on stderr
would land in the middle of what it parses.
"""

import os
import shutil
import subprocess
import sys
import tempfile

from testit import helpers as th

PIDFILE = "/opt/api/var/pids/job_engine.pid"

# Ported verbatim from the shell harness. jobman's pattern names the component,
# so the pgrep stub can dispatch on argv; real pgrep exits 1 with no output when
# nothing matches, and so does this.
PGREP_STUB = """#!/bin/bash
case "$*" in
  *scheduler*) f="$STUBCTL/pgrep_scheduler.txt" ;;
  *engine*)    f="$STUBCTL/pgrep_engine.txt" ;;
  *)           exit 1 ;;
esac
[ -s "$f" ] || exit 1
cat "$f"
"""

# `ps -p <pid>` succeeds only for a PID the fixture declared alive.
PS_STUB = """#!/bin/bash
pid=""
while [ $# -gt 0 ]; do
  if [ "$1" = "-p" ]; then pid="${2:-}"; break; fi
  shift
done
[ -n "$pid" ] || exit 1
grep -qxF "$pid" "$STUBCTL/alive.txt" 2>/dev/null
"""


def _repo_root():
    import mojo
    return os.path.dirname(os.path.dirname(os.path.abspath(mojo.__file__)))


def _write_pids(path, pids):
    with open(path, "w") as handle:
        for pid in pids:
            handle.write("%s\n" % pid)


def _fixture():
    """A throwaway project root plus PATH stubs. Returns (base, root, stubs, ctl);
    the caller removes `base`."""
    base = tempfile.mkdtemp(prefix="testit_jobman.")
    root = os.path.join(base, "proj")
    stubs = os.path.join(base, "stubs")
    ctl = os.path.join(base, "ctl")
    os.makedirs(os.path.join(root, "var", "pids"))
    os.makedirs(stubs)
    os.makedirs(ctl)

    for name, body in (("pgrep", PGREP_STUB), ("ps", PS_STUB)):
        path = os.path.join(stubs, name)
        with open(path, "w") as handle:
            handle.write(body)
        os.chmod(path, 0o755)

    # Default fixture: nothing running anywhere, no pidfiles.
    _write_pids(os.path.join(ctl, "alive.txt"), [])
    _write_pids(os.path.join(ctl, "pgrep_engine.txt"), [])
    _write_pids(os.path.join(ctl, "pgrep_scheduler.txt"), [])
    return base, root, stubs, ctl


def _set_alive(ctl, pids):
    _write_pids(os.path.join(ctl, "alive.txt"), pids)


def _set_pgrep(ctl, comp, pids):
    _write_pids(os.path.join(ctl, "pgrep_%s.txt" % comp), pids)


def _write_pidfile(root, comp, pid):
    path = os.path.join(root, "var", "pids", "job_%s.pid" % comp)
    with open(path, "w") as handle:
        handle.write("%s\n" % pid)
    return path


def _run(args, stubs=None, ctl=None):
    """Spawn the real module. Bytes, not text — `stderr == b""` is a contract."""
    env = dict(os.environ)
    env.pop("DJANGO_SETTINGS_MODULE", None)
    env["PYTHONPATH"] = _repo_root()
    if stubs:
        env["PATH"] = stubs + os.pathsep + env.get("PATH", "")
    if ctl:
        env["STUBCTL"] = ctl
    return subprocess.run(
        [sys.executable, "-m", "mojo.deploy.jobman"] + args,
        env=env, capture_output=True, timeout=120)


def _lines(done):
    return done.stdout.decode("utf-8", "replace").splitlines()


# ---------------------------------------------------------------------------
# status_lines — the ten semantic cases, one per shell-harness case
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_status_healthy_engine_is_not_an_extra_instance_of_itself(opts):
    """THE regression. The managed PID is always in the pgrep result on a
    healthy node; reporting it as an extra makes every node look duplicated."""
    from mojo.deploy import jobman as jm

    lines = jm.status_lines("Engine", PIDFILE, True, "1000", ["1000"])

    th.assert_eq(lines, ["Engine running (PID 1000)"],
                 f"a healthy engine must print exactly one line and must never "
                 f"be reported as an extra instance of itself, got: {lines}")


@th.django_unit_test()
def test_status_reports_one_genuine_duplicate_without_the_managed_pid(opts):
    from mojo.deploy import jobman as jm

    lines = jm.status_lines("Engine", PIDFILE, True, "1000", ["1000", "2000"])

    th.assert_eq(lines, ["Engine running (PID 1000)",
                         "Engine extra instances detected: 2000"],
                 f"only the duplicate belongs on the extra line — the managed "
                 f"PID is subtracted first, got: {lines}")


@th.django_unit_test()
def test_status_puts_two_duplicates_on_one_line(opts):
    from mojo.deploy import jobman as jm

    lines = jm.status_lines("Engine", PIDFILE, True, "1000",
                            ["1000", "2000", "3000"])

    th.assert_eq(lines, ["Engine running (PID 1000)",
                         "Engine extra instances detected: 2000 3000"],
                 f"both duplicates must be space-joined onto a single line, "
                 f"got: {lines}")


@th.django_unit_test()
def test_status_stale_pidfile_with_nothing_running_is_silent(opts):
    from mojo.deploy import jobman as jm

    lines = jm.status_lines("Engine", PIDFILE, True, "", [])

    th.assert_eq(lines, ["Engine not running (stale PID file: %s)" % PIDFILE],
                 f"a stale pidfile with nothing running is one line naming the "
                 f"file, and no extra/unmanaged line, got: {lines}")


@th.django_unit_test()
def test_status_stale_pidfile_plus_live_process_is_unmanaged(opts):
    from mojo.deploy import jobman as jm

    lines = jm.status_lines("Engine", PIDFILE, True, "", ["2000"])

    th.assert_eq(lines, ["Engine not running (stale PID file: %s)" % PIDFILE,
                         "Engine unmanaged instances detected: 2000"],
                 f"with no live pidfile the running process is UNMANAGED, not "
                 f"extra — nothing is 'extra' without a managed instance, "
                 f"got: {lines}")


@th.django_unit_test()
def test_status_puts_two_unmanaged_pids_on_one_line(opts):
    from mojo.deploy import jobman as jm

    lines = jm.status_lines("Engine", PIDFILE, True, "", ["2000", "3000"])

    th.assert_eq(lines, ["Engine not running (stale PID file: %s)" % PIDFILE,
                         "Engine unmanaged instances detected: 2000 3000"],
                 f"both unmanaged PIDs must be space-joined onto a single "
                 f"line, got: {lines}")


@th.django_unit_test()
def test_status_nothing_running_prints_one_line(opts):
    from mojo.deploy import jobman as jm

    lines = jm.status_lines("Engine", PIDFILE, False, "", [])

    th.assert_eq(lines, ["Engine not running"],
                 f"with no pidfile and nothing running the output is exactly "
                 f"one bare not-running line, got: {lines}")


@th.django_unit_test()
def test_status_no_pidfile_but_live_process_is_unmanaged(opts):
    from mojo.deploy import jobman as jm

    lines = jm.status_lines("Engine", PIDFILE, False, "", ["4000"])

    th.assert_eq(lines, ["Engine not running",
                         "Engine unmanaged instances detected: 4000"],
                 f"a jobs process with no pidfile at all must still be "
                 f"reported, got: {lines}")


@th.django_unit_test()
def test_status_bare_run_prints_one_line_per_component(opts):
    from mojo.deploy import jobman as jm

    lines = (jm.status_lines("Engine", PIDFILE, True, "1000", ["1000"])
             + jm.status_lines("Scheduler", PIDFILE, True, "1100", ["1100"]))

    th.assert_eq(lines, ["Engine running (PID 1000)",
                         "Scheduler running (PID 1100)"],
                 f"a healthy node composes to exactly two lines, Engine first "
                 f"— check_node.py greps them positionally, got: {lines}")


@th.django_unit_test()
def test_status_scheduler_gets_the_same_subtraction(opts):
    from mojo.deploy import jobman as jm

    lines = jm.status_lines("Scheduler", PIDFILE, True, "1100",
                            ["1100", "2200"])

    th.assert_eq(lines, ["Scheduler running (PID 1100)",
                         "Scheduler extra instances detected: 2200"],
                 f"the scheduler must subtract its managed PID exactly as the "
                 f"engine does, got: {lines}")


# ---------------------------------------------------------------------------
# paths and the pgrep pattern
# ---------------------------------------------------------------------------

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


@th.django_unit_test()
def test_default_pgrep_pattern_is_byte_identical_to_the_shell_version(opts):
    from mojo.deploy import jobman as jm

    runner = jm.resolve_runner("/opt/api")
    th.assert_eq(runner, "/opt/api/bin/jobs.py",
                 f"the default runner is <root>/bin/jobs.py, got: {runner!r}")

    pattern = jm.pattern_for("/opt/api", runner, "engine")
    th.assert_eq(pattern, "bin/jobs\\.py engine foreground",
                 f"the pattern must match bin/jobman:31 byte for byte, or "
                 f"processes started by the old script stop being found during "
                 f"a rollout. Only the path is re.escape'd — escaping the whole "
                 f"string would escape the spaces too. Got: {pattern!r}")
    th.assert_eq(jm.pattern_for("/opt/api", runner, "scheduler"),
                 "bin/jobs\\.py scheduler foreground",
                 "the scheduler pattern is built the same way")

    th.assert_eq(jm.pidfile("/opt/api", "engine"),
                 "/opt/api/var/pids/job_engine.pid",
                 "pidfiles live at <root>/var/pids/job_<comp>.pid")
    th.assert_eq(jm.logfile("/opt/api", "scheduler"),
                 "/opt/api/var/logs/job_scheduler.log",
                 "logs live at <root>/var/logs/job_<comp>.log")


@th.django_unit_test()
def test_runner_outside_root_is_refused(opts):
    """A `../`-shaped pattern can never match a spawned command line, so status
    would report not-running forever while the every-minute cron spawned a fresh
    duplicate. Refuse instead."""
    from mojo.deploy import jobman as jm

    th.assert_true(jm.is_outside_root("/opt/api", "/opt/other/jobs.py"),
                   "a runner outside the root must be detected")
    th.assert_true(not jm.is_outside_root("/opt/api", "/opt/api/bin/jobs.py"),
                   "the default runner is inside the root and must be allowed")

    base = tempfile.mkdtemp(prefix="testit_jobman.")
    try:
        done = _run(["status", "engine", "--root", base,
                     "--runner", "/usr/bin/env"])
        th.assert_eq(done.returncode, 1,
                     f"a runner outside --root must exit 1, got "
                     f"{done.returncode}: {done.stderr!r}")
        th.assert_eq(done.stdout, b"",
                     f"the refusal must not print a status line anyone could "
                     f"mistake for a reading, got: {done.stdout!r}")
        th.assert_in("outside", done.stderr.decode("utf-8", "replace"),
                     f"the refusal must say why, got: {done.stderr!r}")
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ---------------------------------------------------------------------------
# end to end — dispatch, order, return code, and an empty stderr
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_cli_healthy_engine_prints_exactly_one_line(opts):
    from mojo.deploy import jobman  # noqa: F401 — the module under test

    base, root, stubs, ctl = _fixture()
    try:
        _write_pidfile(root, "engine", 1000)
        _set_alive(ctl, [1000])
        _set_pgrep(ctl, "engine", [1000])

        done = _run(["status", "engine", "--root", root], stubs, ctl)

        th.assert_eq(done.returncode, 0,
                     f"status must always exit 0 — check_node.py reads a "
                     f"non-zero rc as 'jobman unavailable'. stderr: "
                     f"{done.stderr!r}")
        th.assert_eq(done.stderr, b"",
                     f"stderr must stay empty on the status path; check_node.py "
                     f"runs `jobman status 2>&1` and greps the result, got: "
                     f"{done.stderr!r}")
        th.assert_eq(_lines(done), ["Engine running (PID 1000)"],
                     f"a healthy engine prints exactly one line, got: "
                     f"{done.stdout!r}")
    finally:
        shutil.rmtree(base, ignore_errors=True)


@th.django_unit_test()
def test_cli_stale_pidfile_names_the_absolute_path(opts):
    from mojo.deploy import jobman  # noqa: F401 — the module under test

    base, root, stubs, ctl = _fixture()
    try:
        _write_pidfile(root, "engine", 1000)
        _set_alive(ctl, [2000])
        _set_pgrep(ctl, "engine", [2000])

        done = _run(["status", "engine", "--root", root], stubs, ctl)
        expected = os.path.join(root, "var", "pids", "job_engine.pid")

        th.assert_eq(done.returncode, 0,
                     f"a stale pidfile is a reading, not a failure — status "
                     f"still exits 0. stderr: {done.stderr!r}")
        th.assert_eq(done.stderr, b"",
                     f"stderr must stay empty on the status path, got: "
                     f"{done.stderr!r}")
        th.assert_eq(_lines(done),
                     ["Engine not running (stale PID file: %s)" % expected,
                      "Engine unmanaged instances detected: 2000"],
                     f"the stale line must carry the ABSOLUTE pidfile path and "
                     f"the live process must be reported as unmanaged, got: "
                     f"{done.stdout!r}")
    finally:
        shutil.rmtree(base, ignore_errors=True)


@th.django_unit_test()
def test_cli_bare_status_prints_both_components_in_order(opts):
    from mojo.deploy import jobman  # noqa: F401 — the module under test

    base, root, stubs, ctl = _fixture()
    try:
        _write_pidfile(root, "engine", 1000)
        _write_pidfile(root, "scheduler", 1100)
        _set_alive(ctl, [1000, 1100])
        _set_pgrep(ctl, "engine", [1000])
        _set_pgrep(ctl, "scheduler", [1100])

        done = _run(["status", "--root", root], stubs, ctl)

        th.assert_eq(done.returncode, 0,
                     f"a bare status must exit 0, got {done.returncode}: "
                     f"{done.stderr!r}")
        th.assert_eq(done.stderr, b"",
                     f"stderr must stay empty on the status path, got: "
                     f"{done.stderr!r}")
        th.assert_eq(_lines(done), ["Engine running (PID 1000)",
                                    "Scheduler running (PID 1100)"],
                     f"a healthy node prints exactly two lines, Engine first — "
                     f"check_node.py greps both components positionally, got: "
                     f"{done.stdout!r}")
    finally:
        shutil.rmtree(base, ignore_errors=True)
