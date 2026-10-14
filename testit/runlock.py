"""The testit run lock and the report it guards (item #8052).

One rule: the run that holds the lock owns ``var/test_failures.json`` for as
long as it lives. A launch that cannot get the lock touches nothing.

The lock is a kernel ``flock`` on ``var/testit.lock``. The kernel drops it when
the holding process exits, however it exits, so there is no stale lock to
detect and no pid to test. The file itself is never unlinked: a second run
that recreated it would lock a different inode and both runs would go ahead.

While it holds the lock, a run first marks the report ``refused`` /
``not_completed``, stamped with its run identity. It then replaces that marker
with a named refusal or with its real report. A run that dies part-way leaves
the marker, so an older report can never be read as the current one.

Standard library only, and importable by file path: ``testit.runner`` cannot be
imported until ``mojo.helpers.paths`` is configured, and the race test loads
this module in bare subprocesses.
"""
import fcntl
import json
import os
import tempfile
import time
import uuid

LOCK_NAME = "testit.lock"
REPORT_NAME = "test_failures.json"

# Process-wide claims, keyed by the resolved var root. Re-entrant: a launcher
# claims before Django starts, then runner.main finds and reuses that claim.
_CLAIMS = {}


class ClaimReleased(RuntimeError):
    """A write was attempted through a claim that no longer holds the lock."""


class Claim:
    """The lock this process holds over one var root, and the run it names."""

    def __init__(self, var_root, fd, identity, previous_durations):
        self.var_root = var_root
        self.fd = fd
        self.identity = identity
        self.previous_durations = previous_durations
        self.released = False

    @property
    def lock_path(self):
        return os.path.join(self.var_root, LOCK_NAME)

    @property
    def report_path(self):
        return os.path.join(self.var_root, REPORT_NAME)


def _key(var_root):
    return os.path.realpath(str(var_root))


def lock_path(var_root):
    return os.path.join(str(var_root), LOCK_NAME)


def report_path(var_root):
    return os.path.join(str(var_root), REPORT_NAME)


def current(var_root):
    """The live claim this process holds over `var_root`, or None."""
    claim = _CLAIMS.get(_key(var_root))
    if claim is None or claim.released:
        return None
    return claim


def read_holder(var_root):
    """Whatever the holder wrote into the lock file; {} when unreadable."""
    try:
        with open(lock_path(var_root), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _read_durations(var_root):
    """Per-module durations from the report on disk, or {}."""
    try:
        with open(report_path(var_root), "r", encoding="utf-8") as fh:
            modules = (json.load(fh) or {}).get("modules") or {}
        return {name: float(row.get("duration", 0) or 0)
                for name, row in modules.items()}
    except Exception:
        return {}


def acquire(var_root):
    """Take the run lock over `var_root`.

    Returns ``(claim, None)`` on success, or ``(None, holder)`` when another
    process holds it, where ``holder`` is what that process wrote into the lock
    file. A refused call writes, renames and deletes nothing.
    """
    existing = current(var_root)
    if existing is not None:
        return existing, None

    var_root = str(var_root)
    os.makedirs(var_root, exist_ok=True)
    fd = os.open(lock_path(var_root), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None, read_holder(var_root)
    except BaseException:
        os.close(fd)
        raise

    # Before the marker replaces it: the last report's durations order the
    # next run's parallel modules longest-first.
    previous = _read_durations(var_root)
    now = time.time()
    identity = {
        "run_id": str(uuid.uuid4()),
        "pid": os.getpid(),
        "started": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
        "started_at": now,
        "user": os.environ.get("USER", "unknown"),
        "token": os.environ.get("TESTIT_RUN_TOKEN") or None,
    }
    os.ftruncate(fd, 0)
    os.pwrite(fd, json.dumps(identity).encode("utf-8"), 0)
    claim = Claim(var_root, fd, identity, previous)
    _CLAIMS[_key(var_root)] = claim
    return claim, None


def release(claim):
    """Clear the lock file's content and unlock. Never unlinks the file."""
    if claim is None or claim.released:
        return
    claim.released = True
    if _CLAIMS.get(_key(claim.var_root)) is claim:
        del _CLAIMS[_key(claim.var_root)]
    try:
        os.ftruncate(claim.fd, 0)
    except OSError:
        pass
    try:
        fcntl.flock(claim.fd, fcntl.LOCK_UN)
    finally:
        os.close(claim.fd)


def write_report(claim, report):
    """Stamp `report` with the claim's run identity and put it in place
    atomically. Refuses once the claim is released: by then another run may
    own the report."""
    if claim is None or claim.released:
        raise ClaimReleased("the run lock is no longer held; refusing to write the report")
    report["run"] = dict(claim.identity)
    fd, tmp = tempfile.mkstemp(prefix=".test_failures.", suffix=".tmp",
                               dir=claim.var_root)
    try:
        # mkstemp creates 0600; the report has always been world-readable.
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, default=str)
        os.replace(tmp, claim.report_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return report


def refusal_record(identity, code, message):
    """The report a refused run leaves: status ``refused``, one reason, and no
    test results. Carries every top-level key a completed run writes, so a
    reader never meets a missing key."""
    identity = identity or {}
    return {
        "status": "refused",
        "reasons": [{"code": code, "message": message}],
        "preset": None,
        "total": 0,
        "passed": 0,
        "failed": 0,
        "skipped": 0,
        "ran": {"total": 0, "passed": 0, "failed": 0, "skipped": 0},
        "duration": 0,
        "started_at": identity.get("started_at"),
        "modules": {},
        "slowest": [],
        "failures": [],
        "conf_drift": [],
        "budget_violations": [],
        "slow_stacks": [],
        "run": dict(identity) if identity else None,
    }


NOT_COMPLETED_MESSAGE = "  The run did not finish; it may have partly run."


def mark_pending(claim):
    """Mark the report refused / not_completed until the run replaces it."""
    return write_report(claim, refusal_record(
        claim.identity, "not_completed", NOT_COMPLETED_MESSAGE))


def refuse(claim, code, message):
    """Write a named refusal record, then release the lock."""
    try:
        return write_report(claim, refusal_record(claim.identity, code, message))
    finally:
        release(claim)
