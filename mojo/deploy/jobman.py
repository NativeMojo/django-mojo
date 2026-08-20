#!/usr/bin/env python3
"""Start, stop and inspect a node's FOREGROUND job engine and scheduler.

    <root>/bin/jobs.py engine    foreground   ->  <root>/var/pids/job_engine.pid
    <root>/bin/jobs.py scheduler foreground   ->  <root>/var/pids/job_scheduler.pid

    python3 -m mojo.deploy.jobman status          # both components
    python3 -m mojo.deploy.jobman start engine
    python3 -m mojo.deploy.jobman stop  --root /opt/api
    python3 -m mojo.deploy.jobman stop engine --grace 2   # shorter TERM wait

`stop` DOES NOT RETURN UNTIL THE PROCESSES ARE GONE — SIGTERM, then SIGKILL,
then a bounded wait for the corpse. update.sh stops and immediately restarts
this engine, and a `stop` that returned early made the following `start` print
"already running" and start nothing, leaving the node with no engine until the
next cron minute.

WHAT THIS MANAGES, AND WHAT IT DELIBERATELY DOES NOT. There are two job process
planes on a node and they are disjoint:

    this module            `python -m mojo.apps.jobs.cli`
    ------------------     -----------------------------------
    foreground mode        daemon mode
    <root>/var/pids/*.pid  /tmp/job-engine-<runner_id>.pid
    spawned by cron        started by an operator
    needs no settings      validates Redis + Postgres on every command

Nothing is being migrated: they are different mechanisms with different owners.
`jobman stop` will not touch a daemon-mode engine, and the jobs CLI will not see
anything jobman started.

WHY THIS IS NOT PART OF THE JOBS APP. `mojo/apps/jobs/cli.py` runs
`validate_environment()` on every command path including `status` — it pings
Redis, opens a database cursor and counts rows. A node-status tool has to answer
on a box where nothing is configured yet. On top of that, importing anything
under `mojo.apps.jobs` with no `DJANGO_SETTINGS_MODULE` raises AttributeError
from the `mojo.helpers.logit` chain, so the package cannot even be loaded here.

THE CONTRACT `status` HAS TO KEEP. mverify's `check_node.py` runs
`jobman status 2>&1` and matches lines with `startswith("Engine running")` /
`"Scheduler running"`. So: output goes to STDOUT ONLY, stderr stays empty,
the component's status line comes first, both components print on a bare run,
and the exit code is always 0 (a non-zero rc reads as "jobman unavailable").

    Engine running (PID 1234)
    Engine not running (stale PID file: /opt/api/var/pids/job_engine.pid)
    Engine not running
    Engine extra instances detected: 2000 3000       (more than the managed one)
    Engine unmanaged instances detected: 2000        (running, but no live pidfile)

"extra" means strictly MORE than the one instance jobman manages. On a healthy
node the pidfile's own PID always turns up in the pgrep result, so it has to be
subtracted before deciding which line prints.

EVERY PID IS A STRING. A pidfile holding junk goes straight to `ps -p` and
reports not-running, exactly as the shell version did. Nothing here calls
`int()` on a pidfile.

PROCESS MATCHING shells out to `pgrep -f` / `ps -p` rather than re-deriving the
scan in Python, so the deployed matching semantics carry over unchanged. The
pattern is the runner's path RELATIVE to the root:

    bin/jobs\\.py engine foreground

which matches both a relative spawn (`./bin/jobs.py engine foreground`) and an
absolute one, so old and new processes co-match during a rollout.

This replaces `django-mojo-skeleton/bin/jobman`, which becomes a shim:

    #!/usr/bin/env bash
    set -euo pipefail
    ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
    cd "$ROOT"
    export DJANGO_SETTINGS_MODULE=settings
    export OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES
    exec python3 -m mojo.deploy.jobman --root "$ROOT" "$@"

Output is byte-identical to that script with ONE deliberate exception: a missing
or non-executable runner is one stderr line and exit 1 here, where bash printed
a success line and returned 0.

See `docs/django_developer/deploy/README.md`.
"""

import argparse
import functools
import os
import re
import signal
import subprocess
import sys
import time

# component -> the name every output line uses. Insertion order IS the order a
# bare `start`/`stop`/`status` walks them in.
COMPONENTS = {"engine": "Engine", "scheduler": "Scheduler"}

VAR_LOGS = ("var", "logs")
VAR_PIDS = ("var", "pids")

# How long `stop` waits for a SIGTERM to land before escalating: ten one-second
# polls, matching the shell loop it replaces. `--grace` overrides the total for
# one call; the product below stays the default.
TERM_POLLS = 10
TERM_POLL_SECONDS = 1

# And how long it waits AFTER SIGKILL for the corpse to actually go. SIGKILL is
# not synchronous: the process is gone when the kernel says so, not when kill(2)
# returns. Short polls because this is normally over in milliseconds — but it is
# waited on, because `stop` returning early is `start` finding a live PID and
# printing "already running" while nothing runs.
KILL_WAIT_SECONDS = 2
KILL_POLL_SECONDS = 0.1

# Logging: the repo's standard graceful-fallback idiom. On a node the except
# branch ALWAYS fires — mojo.helpers.logit reads paths.LOG_ROOT at module level
# and that attribute only exists once Django settings have configured the paths.
# See mojo/deploy/__init__.py for the full contract.
#
# NOTE the logger is for diagnostics only. Nothing on the status path may log,
# because check_node.py merges stderr into the output it greps.
try:
    from mojo.helpers import logit
    log = logit.get_logger("jobman", "jobman.log")
except Exception:
    import logging
    log = logging.getLogger("jobman")


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------

def resolve_root(explicit=None):
    """--root, else MOJO_PROJECT_ROOT, else the working directory.

    Always absolute: the stale-pidfile status line prints this path, and a
    relative root would emit a line that means something different depending on
    where the reader's shell happened to be. There is no upward walk — the shim
    cds and passes --root.
    """
    return os.path.abspath(
        explicit or os.environ.get("MOJO_PROJECT_ROOT") or os.getcwd())


def log_dir(root):
    return os.path.join(root, *VAR_LOGS)


def pid_dir(root):
    return os.path.join(root, *VAR_PIDS)


def pidfile(root, comp):
    return os.path.join(pid_dir(root), "job_%s.pid" % comp)


def logfile(root, comp):
    return os.path.join(log_dir(root), "job_%s.log" % comp)


def resolve_runner(root, runner=None):
    """Absolute path to the jobs runner. A relative --runner is relative to the
    ROOT, not to the caller's cwd, so a cron entry and an interactive run
    resolve the same way."""
    if not runner:
        return os.path.join(root, "bin", "jobs.py")
    if not os.path.isabs(runner):
        runner = os.path.join(root, runner)
    return os.path.normpath(runner)


def is_outside_root(root, runner_path):
    """A runner outside the root is refused, not tolerated.

    The pgrep pattern is built from the runner's path RELATIVE to the root, so a
    runner outside it produces a `../`-shaped pattern that can never match a
    spawned command line. `status` would then report not-running forever while
    the every-minute cron happily spawned another duplicate.
    """
    rel = os.path.relpath(runner_path, root)
    return rel == os.pardir or rel.startswith(os.pardir + os.sep)


def pattern_for(root, runner_path, comp):
    """The `pgrep -f` pattern for one component.

    Only the runner path is escaped. Escaping the whole string would break it:
    Python's re.escape escapes spaces, and the deployed pattern has plain ones.
    The default is byte-identical to the shell version's `bin/jobs\\.py <comp>
    foreground`.
    """
    return re.escape(os.path.relpath(runner_path, root)) + " " + comp + " foreground"


def ensure_dirs(root):
    """mkdir -p var/logs var/pids. Returns the OSError if it could not be done.

    Start needs the log directory, but `status` must still answer (exit 0, empty
    stderr) on a box where var/ is not writable — so the caller decides what a
    failure means rather than this raising.
    """
    problem = None
    for path in (log_dir(root), pid_dir(root)):
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as err:
            problem = err
    return problem


# ---------------------------------------------------------------------------
# status — the pure core
# ---------------------------------------------------------------------------

def status_lines(name, pid_path, pidfile_present, managed, matches):
    """The status output for one component, as a list of lines. Prints nothing.

    `managed` is the pidfile's PID when that process is alive, else "". This
    function alone carries the semantics `check_node.py` depends on, which is
    why it takes plain values instead of going and looking at the box.
    """
    if managed:
        lines = ["%s running (PID %s)" % (name, managed)]
    elif pidfile_present:
        lines = ["%s not running (stale PID file: %s)" % (name, pid_path)]
    else:
        lines = ["%s not running" % name]

    # The managed PID is always in `matches` on a healthy node. Subtracting it
    # is the whole point: without this, jobman reports the engine as an extra
    # instance of itself.
    others = [pid for pid in matches if pid != managed]
    if others:
        kind = "extra" if managed else "unmanaged"
        lines.append("%s %s instances detected: %s"
                     % (name, kind, " ".join(others)))
    return lines


# ---------------------------------------------------------------------------
# process probing
# ---------------------------------------------------------------------------

def pgrep_matches(pattern):
    """PIDs whose command line matches `pattern`, in pgrep's own order.

    A non-zero return code means "nothing matched" — that is pgrep's normal way
    of saying no, not an error. A box with no pgrep at all reports nothing
    rather than raising.
    """
    try:
        done = subprocess.run(["pgrep", "-f", "--", pattern],
                              capture_output=True, text=True)
    except OSError as err:
        log.debug("pgrep unavailable (%s) — treating as no matches", err)
        return []
    return [line.strip() for line in done.stdout.splitlines() if line.strip()]


def pid_alive(pid):
    """`ps` still lists a RUNNABLE process. Takes a STRING; garbage reports dead.

    A ZOMBIE IS DEAD. `ps -p <pid>` succeeds for a corpse whose parent has not
    reaped it, which is how the old probe read one: as a live process. That is
    not a philosophical distinction here — it kept the pidfile of a
    just-SIGKILLed engine ("its PID is still alive, so the file is information,
    not litter"), and the following `cmd_start` then saw a managed PID and
    refused to start anything. The state column is asked for with an empty
    header (`stat=`) so nothing but the state itself is printed; a `ps` that
    prints nothing at all still counts as alive, which keeps every stubbed
    `ps -p` in the test suite meaning what it always meant.
    """
    if not pid:
        return False
    try:
        done = subprocess.run(["ps", "-o", "stat=", "-p", pid],
                              capture_output=True, text=True)
    except OSError as err:
        log.debug("ps unavailable (%s) — treating %r as dead", err, pid)
        return False
    if done.returncode != 0:
        return False
    return not (done.stdout or "").strip().startswith("Z")


def wait_gone(pids, seconds, interval):
    """Poll until every PID is gone, or `seconds` elapse. Returns the survivors."""
    deadline = time.monotonic() + max(0, seconds)
    while True:
        remaining = [pid for pid in pids if pid_alive(pid)]
        if not remaining or time.monotonic() >= deadline:
            return remaining
        time.sleep(interval)


def read_pidfile(path):
    """(present, pid-string). Never int() — a pidfile holding junk has to reach
    `ps -p` and report not-running, exactly as `ps -p "$(cat ...)"` did."""
    try:
        with open(path) as handle:
            return True, handle.read().strip()
    except FileNotFoundError:
        return False, ""
    except OSError as err:
        log.debug("cannot read %s: %s", path, err)
        return True, ""


def probe(root, runner_path, comp):
    """(pidfile path, pidfile present, managed PID or "", pgrep matches)."""
    path = pidfile(root, comp)
    present, pid = read_pidfile(path)
    managed = pid if (pid and pid_alive(pid)) else ""
    matches = pgrep_matches(pattern_for(root, runner_path, comp))
    return path, present, managed, matches


def signal_pids(pids, sig):
    """kill -<sig>, tolerating a PID that has already gone — the Python spelling
    of `kill -TERM $pids 2>/dev/null || true`. Every PID reaching here came from
    pgrep or from a live `ps -p`, so it is numeric."""
    for pid in pids:
        try:
            os.kill(int(pid), sig)
        except (ValueError, OSError) as err:
            log.debug("cannot signal %r: %s", pid, err)


def remove_file(path):
    try:
        os.remove(path)
    except OSError as err:
        log.debug("cannot remove %s: %s", path, err)


# ---------------------------------------------------------------------------
# verbs
# ---------------------------------------------------------------------------

def cmd_status(root, runner_path, comp):
    """Always exits 0. check_node.py reads a non-zero rc as "jobman unavailable"
    and stops looking at the output entirely."""
    path, present, managed, matches = probe(root, runner_path, comp)
    for line in status_lines(COMPONENTS[comp], path, present, managed, matches):
        print(line)
    return 0


def cmd_start(root, runner_path, comp):
    name = COMPONENTS[comp]
    path, _present, managed, matches = probe(root, runner_path, comp)

    if managed:
        print("%s already running (PID %s)" % (name, managed))
        return 0

    if matches:
        print("%s appears to be running already (detected PIDs: %s). "
              "Refusing to start another." % (name, " ".join(matches)))
        print("If this is stale, run './bin/jobman stop %s' first." % comp)
        return 0

    # THE ONE PARITY EXCEPTION. Bash printed a success line and returned 0 with
    # no runner on disk, so the cron reported a healthy start every minute while
    # nothing ran. Refusing loudly is strictly better.
    if not os.path.isfile(runner_path) or not os.access(runner_path, os.X_OK):
        print("jobman: %s is missing or not executable" % runner_path,
              file=sys.stderr)
        return 1

    log_path = logfile(root, comp)
    try:
        handle = open(log_path, "a")
    except OSError as err:
        print("jobman: cannot open %s: %s" % (log_path, err), file=sys.stderr)
        return 1

    # THE PIDFILE IS OPENED BEFORE THE SPAWN, for the same reason the log is.
    # An engine started as root (update.sh runs under sudo) leaves both files
    # root-owned, and the next start as the app user cannot write either. The
    # log open already refused loudly; the pidfile write did not — it raised
    # AFTER Popen, leaving a running engine with no pidfile, which nothing can
    # ever stop again and which makes every later start refuse as well.
    try:
        pid_handle = open(path, "w")
    except OSError as err:
        handle.close()
        print("jobman: cannot write the pidfile %s: %s" % (path, err),
              file=sys.stderr)
        return 1

    print("Starting %s (foreground mode, backgrounded by shell)..." % comp)
    child_env = dict(os.environ)
    # DJANGO_SETTINGS_MODULE is deliberately NOT set here — bin/jobs.py
    # setdefaults it, and the shim exports it.
    if sys.platform == "darwin":
        child_env.setdefault("OBJC_DISABLE_INITIALIZE_FORK_SAFETY", "YES")

    try:
        # start_new_session detaches from this process group, so the child
        # survives the cron shell exiting — what `nohup ... &` bought, without
        # depending on nohup being installed.
        proc = subprocess.Popen(
            [runner_path, comp, "foreground"],
            cwd=root, stdin=subprocess.DEVNULL, stdout=handle,
            stderr=subprocess.STDOUT, start_new_session=True, env=child_env)
    except OSError as err:
        pid_handle.close()
        remove_file(path)
        print("jobman: cannot start %s: %s" % (runner_path, err),
              file=sys.stderr)
        return 1
    finally:
        handle.close()

    try:
        pid_handle.write("%d\n" % proc.pid)
        pid_handle.close()
    except OSError as err:
        # The child is already running. An unrecorded engine is worse than no
        # engine — cron starts a fresh one within the minute, but nothing will
        # ever stop this one — so take it back down before failing.
        signal_pids([str(proc.pid)], signal.SIGTERM)
        if wait_gone([str(proc.pid)], KILL_WAIT_SECONDS, KILL_POLL_SECONDS):
            signal_pids([str(proc.pid)], signal.SIGKILL)
            wait_gone([str(proc.pid)], KILL_WAIT_SECONDS, KILL_POLL_SECONDS)
        remove_file(path)
        print("jobman: cannot write the pidfile %s: %s" % (path, err),
              file=sys.stderr)
        return 1
    print("%s PID: %d, log: %s" % (name, proc.pid, log_path))
    return 0


def cmd_stop(root, runner_path, comp, grace=None):
    name = COMPONENTS[comp]
    path, present, managed, matches = probe(root, runner_path, comp)
    if grace is None:
        grace = TERM_POLLS * TERM_POLL_SECONDS

    # Union of the managed PID and everything pgrep found. sorted(set(...)) is a
    # LEXICAL sort over strings, which is what `sort -u` did.
    pids = [managed] if managed else []
    if matches:
        pids = sorted(set(pids + matches))

    if not pids:
        if present:
            print("%s not running (stale PID file), cleaning up" % name)
            remove_file(path)
        else:
            print("%s not running" % name)
        return 0

    print("Stopping %s (PIDs: %s)..." % (name, " ".join(pids)))
    signal_pids(pids, signal.SIGTERM)

    remaining = wait_gone(pids, grace, TERM_POLL_SECONDS)
    if remaining:
        print("Force killing %s (PIDs: %s)..." % (name, " ".join(remaining)))
        signal_pids(remaining, signal.SIGKILL)
        # WAIT FOR THE KILL. Signalling is not stopping: this used to fall
        # straight through, so `stop` returned with the process still dying,
        # the pidfile still naming it, and the caller's next `start` reporting
        # "already running" and starting nothing. update.sh now restarts the
        # engine itself, which makes that failure a node with no engine.
        survivors = wait_gone(remaining, KILL_WAIT_SECONDS, KILL_POLL_SECONDS)
        if survivors:
            print("%s did not exit after SIGKILL (PIDs: %s)"
                  % (name, " ".join(survivors)))

    # Only remove the pidfile once ITS pid is gone. A pidfile naming a process
    # that survived the kill is information, not litter.
    if os.path.isfile(path):
        _, current = read_pidfile(path)
        if not current or not pid_alive(current):
            remove_file(path)
    print("%s stopped" % name)
    return 0


VERBS = {"start": cmd_start, "stop": cmd_stop, "status": cmd_status}


# ---------------------------------------------------------------------------
# entry
# ---------------------------------------------------------------------------

def main(argv):
    # prog is set explicitly: under `-m` argparse derives it from sys.argv[0]
    # and prints "jobman.py", which is not an invocation an operator can copy.
    parser = argparse.ArgumentParser(
        prog="python3 -m mojo.deploy.jobman",
        description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=tuple(VERBS),
                        help="what to do")
    parser.add_argument("component", nargs="?", default=None,
                        help="engine or scheduler (default: both, engine first)")
    parser.add_argument("--root", default=None,
                        help="project root holding bin/ and var/ "
                             "(default: $MOJO_PROJECT_ROOT, else cwd)")
    parser.add_argument("--runner", default=None,
                        help="the jobs runner (default: <root>/bin/jobs.py)")
    parser.add_argument("--grace", type=float, default=None,
                        help="stop only: seconds to wait for SIGTERM before "
                             "escalating to SIGKILL (default: %d)"
                             % (TERM_POLLS * TERM_POLL_SECONDS))
    parser.add_argument("--verbose", action="store_true",
                        help="log diagnostics to stderr")
    args = parser.parse_args(argv)
    if args.grace is not None:
        # A usage error, not a silently ignored flag. It is also exit 2, which
        # is what update.sh's fallback branch keys on when a rollback restores
        # a jobman that predates --grace entirely.
        if args.command != "stop":
            parser.error("--grace applies to `stop` only")
        if args.grace < 0:
            parser.error("--grace must not be negative")

    import logging
    # WARNING, not INFO: `status` is grepped through `2>&1`, so a routine log
    # line on stderr would land in the middle of the output a consumer parses.
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s [jobman] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S")

    if args.component is not None and args.component not in COMPONENTS:
        print("Unknown component: %s" % args.component, file=sys.stderr)
        return 1

    root = resolve_root(args.root)
    runner_path = resolve_runner(root, args.runner)
    if is_outside_root(root, runner_path):
        print("jobman: --runner %s is outside --root %s — the pgrep pattern "
              "would never match a running process" % (runner_path, root),
              file=sys.stderr)
        return 1

    problem = ensure_dirs(root)
    if problem is not None and args.command != "status":
        print("jobman: cannot create %s: %s" % (os.path.join(root, "var"), problem),
              file=sys.stderr)
        return 1

    # Every verb dispatches uniformly as handler(root, runner_path, comp); the
    # partial is how --grace reaches cmd_stop without giving the other two a
    # parameter they have no use for.
    handler = VERBS[args.command]
    if args.command == "stop" and args.grace is not None:
        handler = functools.partial(cmd_stop, grace=args.grace)
    components = [args.component] if args.component else list(COMPONENTS)
    result = 0
    for comp in components:
        result = handler(root, runner_path, comp) or result
    return result


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
