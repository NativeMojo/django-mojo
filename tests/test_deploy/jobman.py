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
import time
import uuid

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

@th.tier("extended")
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


@th.tier("extended")
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


@th.tier("extended")
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


# ---------------------------------------------------------------------------
# real processes — stop must not return while the engine is still alive
# ---------------------------------------------------------------------------
#
# Everything above stubs `ps`/`pgrep`, which is right for the output contract
# and useless for the lifecycle: whether `stop` actually WAITED can only be
# answered by a real process that really ignores SIGTERM. update.sh now starts
# the engine again right after stopping it, so a `stop` that returns early
# leaves `cmd_start` looking at a live process and printing "already running"
# — a restart that reports success and starts nothing.

# A stand-in for bin/jobs.py that survives SIGTERM. `time.sleep` in a loop
# rather than `signal.pause()`: the point is a process that is genuinely
# running, not one parked in a syscall.
IGNORES_SIGTERM = """#!/usr/bin/env python3
import signal
import time

signal.signal(signal.SIGTERM, signal.SIG_IGN)
while True:
    time.sleep(0.2)
"""

# Forks, lets the child exit, and never reaps it: the child is a real zombie
# for as long as this process lives. Prints the corpse's PID.
MAKES_A_ZOMBIE = """import os
import sys
import time

pid = os.fork()
if pid == 0:
    os._exit(0)
sys.stdout.write(str(pid) + "\\n")
sys.stdout.flush()
time.sleep(30)
"""


def _install_runner(root, body=IGNORES_SIGTERM):
    """Install a fake runner under a name unique to this test, and return the
    `--runner` value naming it.

    The uniqueness is load-bearing: `pattern_for` builds the pgrep pattern from
    the runner's path RELATIVE to the root, so every test using the default
    bin/jobs.py shares ONE pattern no matter how private its tempdir is — and
    testit runs these concurrently. One test's live fixture engine then reads
    as another's "already running".
    """
    name = "jobs_%s.py" % uuid.uuid4().hex[:10]
    path = os.path.join(root, "bin", name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        handle.write(body)
    os.chmod(path, 0o755)
    return os.path.join("bin", name)


def _kill_runner(root, runner):
    """SIGKILL, not pkill's default SIGTERM — the fixture ignores SIGTERM,
    which is the entire point of it."""
    if not runner:
        return
    subprocess.run(["pkill", "-9", "-f", os.path.join(root, runner)],
                   capture_output=True)


def _process_alive(pid):
    """Real `ps`, zombie-aware — the question the test asks is 'is there still
    a process', and a corpse is not one."""
    done = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                          capture_output=True, text=True)
    return done.returncode == 0 and not (done.stdout or "").strip().startswith("Z")


def _started_pid(root, comp="engine"):
    with open(os.path.join(root, "var", "pids", "job_%s.pid" % comp)) as handle:
        return int(handle.read().strip())


def _reap(pid):
    try:
        os.kill(pid, 9)
    except OSError:
        pass


@th.tier("extended")
@th.django_unit_test()
def test_stop_returns_only_once_the_engine_is_actually_dead(opts):
    """The defect: `stop` signalled SIGKILL and fell through without waiting,
    so it returned while the process was still dying — and kept the pidfile,
    because `ps -p` reports a corpse as alive. The next `start` then found a
    'live' PID and refused to start anything."""
    from mojo.deploy import jobman  # noqa: F401 — the module under test

    base, root, _stubs, _ctl = _fixture()
    pid = None
    runner = None
    try:
        runner = _install_runner(root)
        started = _run(["start", "engine", "--root", root, "--runner", runner])
        th.assert_eq(started.returncode, 0,
                     f"the fixture engine must start, got {started.returncode}: "
                     f"{started.stderr!r}")
        pid = _started_pid(root)
        th.assert_true(_process_alive(pid),
                       f"the fixture engine (PID {pid}) must be running before "
                       f"the stop under test")

        done = _run(["stop", "engine", "--root", root, "--runner", runner,
                     "--grace", "2"])

        th.assert_eq(done.returncode, 0,
                     f"stop must exit 0, got {done.returncode}: {done.stderr!r}")
        th.assert_true(
            not _process_alive(pid),
            f"stop returned while PID {pid} was still alive — the SIGKILL was "
            f"sent but never waited on, so the caller's next `start` races a "
            f"dying engine")
        th.assert_true(
            not os.path.isfile(os.path.join(root, "var", "pids",
                                            "job_engine.pid")),
            "a stop that killed the process must remove its pidfile — a "
            "surviving pidfile makes the next `start` print 'already running' "
            "and start nothing")
    finally:
        if pid:
            _reap(pid)
        _kill_runner(root, runner)
        shutil.rmtree(base, ignore_errors=True)


@th.tier("extended")
@th.django_unit_test()
def test_stop_then_start_actually_restarts_the_engine(opts):
    """The whole point of the fix, end to end: update.sh stops the engine and
    immediately starts it again. A `start` that prints 'already running' after
    a `stop` is a restart that silently did nothing."""
    from mojo.deploy import jobman  # noqa: F401 — the module under test

    base, root, _stubs, _ctl = _fixture()
    first = second = runner = None
    try:
        runner = _install_runner(root)
        _run(["start", "engine", "--root", root, "--runner", runner])
        first = _started_pid(root)
        _run(["stop", "engine", "--root", root, "--runner", runner,
              "--grace", "2"])

        done = _run(["start", "engine", "--root", root, "--runner", runner])
        text = done.stdout.decode("utf-8", "replace")

        th.assert_eq(done.returncode, 0,
                     f"the restart must exit 0, got {done.returncode}: "
                     f"{done.stderr!r}")
        th.assert_true("already running" not in text,
                       f"the engine was stopped, so the following start must "
                       f"actually start one, got: {text!r}")
        second = _started_pid(root)
        th.assert_true(second != first,
                       f"the restarted engine must be a NEW process, got the "
                       f"same PID {second} twice")
        th.assert_true(_process_alive(second),
                       f"the restarted engine (PID {second}) must be running")
    finally:
        for pid in (first, second):
            if pid:
                _reap(pid)
        _kill_runner(root, runner)
        shutil.rmtree(base, ignore_errors=True)


@th.tier("extended")
@th.django_unit_test()
def test_grace_shortens_the_term_wait(opts):
    """update.sh passes `--grace 2`: the deploy already proved the release, and
    ten seconds of polite waiting per component is ten seconds the node spends
    with no engine. The default stays 10."""
    from mojo.deploy import jobman as jm

    th.assert_eq(jm.TERM_POLLS * jm.TERM_POLL_SECONDS, 10,
                 "the default grace must remain the ten one-second polls the "
                 "shell version used")

    base, root, _stubs, _ctl = _fixture()
    pid = None
    runner = None
    try:
        runner = _install_runner(root)
        _run(["start", "engine", "--root", root, "--runner", runner])
        pid = _started_pid(root)

        began = time.monotonic()
        done = _run(["stop", "engine", "--root", root, "--runner", runner,
                     "--grace", "1"])
        elapsed = time.monotonic() - began

        th.assert_eq(done.returncode, 0,
                     f"a graced stop must exit 0, got {done.returncode}: "
                     f"{done.stderr!r}")
        th.assert_true(elapsed < 5,
                       f"--grace 1 must escalate to SIGKILL after ~1s, not the "
                       f"default 10 — the stop took {elapsed:.1f}s")
        th.assert_true(not _process_alive(pid),
                       f"a graced stop still has to leave PID {pid} dead")
    finally:
        if pid:
            _reap(pid)
        _kill_runner(root, runner)
        shutil.rmtree(base, ignore_errors=True)


@th.tier("extended")
@th.django_unit_test()
def test_a_zombie_pid_is_dead(opts):
    """`ps -p` reports a zombie as alive. That is what kept the pidfile after a
    SIGKILL, and what would make a post-kill wait loop never terminate."""
    from mojo.deploy import jobman as jm

    proc = subprocess.Popen([sys.executable, "-c", MAKES_A_ZOMBIE],
                            stdout=subprocess.PIPE)
    try:
        corpse = proc.stdout.readline().decode("utf-8", "replace").strip()
        th.assert_true(corpse.isdigit(),
                       f"the fixture must report the corpse's PID, got {corpse!r}")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = subprocess.run(["ps", "-o", "stat=", "-p", corpse],
                                   capture_output=True, text=True)
            if (state.stdout or "").strip().startswith("Z"):
                break
            time.sleep(0.1)
        th.assert_true((state.stdout or "").strip().startswith("Z"),
                       f"the fixture must actually produce a zombie, ps says "
                       f"{state.stdout!r}")
        th.assert_eq(subprocess.run(["ps", "-p", corpse],
                                    capture_output=True).returncode, 0,
                     "the premise of the bug: plain `ps -p` reports a zombie as "
                     "alive, which is why pid_alive had to learn about state")

        th.assert_true(not jm.pid_alive(corpse),
                       f"a zombie must read as DEAD (PID {corpse}) — treating a "
                       f"corpse as alive keeps a stale pidfile forever and makes "
                       f"the post-SIGKILL wait spin out its whole budget")
    finally:
        proc.kill()
        proc.wait(timeout=10)


@th.tier("extended")
@th.django_unit_test()
def test_start_refuses_loudly_when_the_pidfile_cannot_be_written(opts):
    """A root-started engine leaves root-owned files behind; the next start as
    the app user cannot write them. The log open was already guarded — the
    pidfile write was not, and it raised AFTER the spawn, leaving an engine
    nothing manages."""
    from mojo.deploy import jobman  # noqa: F401 — the module under test

    base, root, _stubs, _ctl = _fixture()
    path = os.path.join(root, "var", "pids", "job_engine.pid")
    runner = None
    try:
        runner = _install_runner(root)
        with open(path, "w") as handle:
            handle.write("")
        os.chmod(path, 0o000)

        done = _run(["start", "engine", "--root", root, "--runner", runner])
        text = done.stdout.decode("utf-8", "replace")

        th.assert_eq(done.returncode, 1,
                     f"an unwritable pidfile must fail loudly, got "
                     f"{done.returncode}: {done.stdout!r} {done.stderr!r}")
        th.assert_in("pid", done.stderr.decode("utf-8", "replace").lower(),
                     f"the refusal must name the pidfile, got: {done.stderr!r}")
        th.assert_true("PID:" not in text,
                       f"a failed start must not report a PID it did not keep, "
                       f"got: {text!r}")

        status = _run(["status", "engine", "--root", root, "--runner", runner])
        lines = status.stdout.decode("utf-8", "replace")
        th.assert_true("unmanaged" not in lines,
                       f"a refused start must leave NO engine behind — an "
                       f"unmanaged instance is exactly the state nothing can "
                       f"stop again, got: {lines!r}")
    finally:
        os.chmod(path, 0o644)
        _kill_runner(root, runner)
        shutil.rmtree(base, ignore_errors=True)


@th.tier("extended")
@th.django_unit_test()
def test_start_refuses_loudly_when_the_logfile_cannot_be_opened(opts):
    """The same ownership trap on the log side, which was already guarded —
    pinned here so the pidfile guard cannot be 'simplified' into replacing it."""
    from mojo.deploy import jobman  # noqa: F401 — the module under test

    base, root, _stubs, _ctl = _fixture()
    os.makedirs(os.path.join(root, "var", "logs"), exist_ok=True)
    path = os.path.join(root, "var", "logs", "job_engine.log")
    runner = None
    try:
        runner = _install_runner(root)
        with open(path, "w") as handle:
            handle.write("")
        os.chmod(path, 0o000)

        done = _run(["start", "engine", "--root", root, "--runner", runner])

        th.assert_eq(done.returncode, 1,
                     f"an unopenable log must fail loudly, got "
                     f"{done.returncode}: {done.stderr!r}")
        status = _run(["status", "engine", "--root", root, "--runner", runner])
        lines = status.stdout.decode("utf-8", "replace")
        th.assert_true("unmanaged" not in lines,
                       f"a refused start must leave no engine behind, got: "
                       f"{lines!r}")
    finally:
        os.chmod(path, 0o644)
        _kill_runner(root, runner)
        shutil.rmtree(base, ignore_errors=True)


@th.tier("extended")
@th.django_unit_test()
def test_status_output_is_unchanged_by_the_stop_and_start_work(opts):
    """check_node.check_jobs greps `jobman status` stdout and reads a non-zero
    rc as 'jobman unavailable'. Nothing in this item may move that byte."""
    from mojo.deploy import jobman  # noqa: F401 — the module under test

    base, root, stubs, ctl = _fixture()
    try:
        _write_pidfile(root, "engine", 1000)
        _write_pidfile(root, "scheduler", 1100)
        _set_alive(ctl, [1000, 1100, 2000])
        _set_pgrep(ctl, "engine", [1000, 2000])
        _set_pgrep(ctl, "scheduler", [1100])

        done = _run(["status", "--root", root], stubs, ctl)

        th.assert_eq(done.returncode, 0,
                     f"status must always exit 0, got {done.returncode}")
        th.assert_eq(done.stderr, b"",
                     f"status stderr must stay empty, got {done.stderr!r}")
        th.assert_eq(
            done.stdout,
            b"Engine running (PID 1000)\n"
            b"Engine extra instances detected: 2000\n"
            b"Scheduler running (PID 1100)\n",
            f"the status bytes are check_node's contract, got {done.stdout!r}")
    finally:
        shutil.rmtree(base, ignore_errors=True)


@th.tier("extended")
@th.django_unit_test()
def test_grace_is_a_stop_only_flag(opts):
    """`--grace` on anything but `stop` is a usage error, not a silently
    ignored flag — and a usage error is exit 2, which is what update.sh's
    fallback branch keys on when a rollback restores a jobman without it."""
    from mojo.deploy import jobman  # noqa: F401 — the module under test

    base, root, stubs, ctl = _fixture()
    try:
        done = _run(["start", "engine", "--root", root, "--grace", "2"],
                    stubs, ctl)
        th.assert_eq(done.returncode, 2,
                     f"--grace outside `stop` must be an argparse usage error "
                     f"(exit 2), got {done.returncode}: {done.stderr!r}")
    finally:
        shutil.rmtree(base, ignore_errors=True)

