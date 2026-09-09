"""Retainable, stdlib-only deployment observer. Never decides app success.

The durable intent precedes systemd submission. An interrupted submission is
therefore reconciled, never retried, even when its reply/job id was lost.
"""

import argparse
import datetime
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time

EVIDENCE_PATH = "/etc/mojosec/runtime-refresh.json"
STATUS_PATH = "/run/mojosec/status.json"
UNIT = "mojosec.service"
MAX_BYTES = 262144
IDENTITY_FIELDS = ("framework_version", "pid", "boot_id", "process_start_ticks",
                   "process_started_at")


def read_bytes(path, limit=MAX_BYTES, owner_uid=0, private=False):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                info.st_uid != owner_uid or info.st_mode & (0o077 if private else 0o022)):
            raise ValueError("unsafe protected file")
        data = os.read(fd, limit + 1)
        if len(data) > limit:
            raise ValueError("protected file exceeds bound")
        return data
    finally:
        os.close(fd)


def read_json(path, owner_uid=0, private=False):
    value = json.loads(read_bytes(path, owner_uid=owner_uid, private=private))
    if not isinstance(value, dict):
        raise ValueError("expected object")
    return value


def durable_write(path, data, mode=0o600, owner_uid=0, sync=None):
    """File then directory fsync; callers only use protected local directories."""
    parent = os.path.dirname(path)
    sync = sync or os.fsync
    info = os.lstat(parent)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != owner_uid or info.st_mode & 0o022:
        raise ValueError("unsafe protected directory")
    fd, temporary = tempfile.mkstemp(prefix=".refresh-", dir=parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(data)
            handle.flush()
            sync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            sync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def kernel_identity(pid=None, proc_root="/proc"):
    pid = os.getpid() if pid is None else int(pid)
    if pid <= 0:
        raise ValueError("missing process")
    # procfs reports zero sizes: read fixed bounds rather than trusting st_size.
    with open(f"{proc_root}/sys/kernel/random/boot_id") as handle:
        boot = handle.read(128).strip()
    with open(f"{proc_root}/{pid}/stat") as handle:
        fields = handle.read(8192).rsplit(")", 1)[1].split()
    ticks = int(fields[19])
    with open(f"{proc_root}/stat") as handle:
        boot_time = next(int(line.split()[1]) for line in handle if line.startswith("btime "))
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", boot) or ticks <= 0:
        raise ValueError("invalid kernel generation")
    started = boot_time + ticks / os.sysconf("SC_CLK_TCK")
    return {"pid": pid, "boot_id": boot, "process_start_ticks": ticks,
            "process_started_at": datetime.datetime.fromtimestamp(
                started, datetime.timezone.utc).isoformat()}


def timestamp(value):
    parsed = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.utcoffset() != datetime.timedelta(0):
        raise ValueError("timestamp must be UTC")
    return parsed.timestamp()


def proves(status, identity, now=None):
    """A fresh running envelope belongs to this exact kernel generation."""
    now = time.time() if now is None else now
    try:
        updated = timestamp(status["updated_at"])
        started = timestamp(status["process_started_at"])
        kernel_started = timestamp(identity["process_started_at"])
        return bool(status.get("running") is True and
                    isinstance(status.get("framework_version"), str) and
                    0 < len(status["framework_version"]) <= 64 and
                    all(status.get(key) == identity[key] for key in
                        ("pid", "boot_id", "process_start_ticks")) and
                    abs(started - kernel_started) < 1 and
                    kernel_started <= updated <= now and now - updated <= 120)
    except (KeyError, ValueError, TypeError, OverflowError):
        return False


class Host:
    def __init__(self, runner=None, status_path=STATUS_PATH, proc_root="/proc"):
        self.runner = runner or subprocess.run
        self.status_path = status_path
        self.proc_root = proc_root
        self.boot_id = kernel_identity(proc_root=proc_root)["boot_id"]

    def command(self, argv, timeout, include_stderr=False):
        result = self.runner(argv, capture_output=True, text=True,
                             timeout=timeout, check=False)
        if result.returncode:
            raise RuntimeError("command failed: " + argv[0])
        return result.stdout + (result.stderr if include_stderr else "")

    def version(self, timeout):
        return self.command(["/usr/bin/python3", "-E", "-P", "-c",
                             "from importlib.metadata import version; print(version('django-mojo'))"], timeout).strip()

    def service(self, timeout):
        raw = self.command(["systemctl", "show", UNIT, "--property=ActiveState,MainPID,LoadState,Job"], timeout)
        return dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)

    def observe(self, timeout):
        # Each command gets its share of this query budget.
        before = self.service(timeout / 2)
        if before.get("ActiveState") != "active":
            return {"active": False, "state": before.get("ActiveState", "unknown"),
                    "generation": None, "proved": False}
        try:
            identity = kernel_identity(before["MainPID"], self.proc_root)
            try:
                status = read_json(self.status_path)
            except (OSError, ValueError):
                status = {}
            after_identity = kernel_identity(before["MainPID"], self.proc_root)
            after = self.service(timeout / 2)
            stable = before == after and identity == after_identity
            legacy = False
            if stable and not all(k in status for k in IDENTITY_FIELDS):
                try:
                    updated = timestamp(status["updated_at"])
                    legacy = (timestamp(identity["process_started_at"]) <= updated <= time.time()
                              and time.time() - updated <= 120)
                except (KeyError, ValueError, TypeError):
                    pass
            return {"active": after.get("ActiveState") == "active",
                    "state": after.get("ActiveState", "unknown"),
                    "generation": [identity[k] for k in ("boot_id", "pid", "process_start_ticks")],
                    "proved": stable and proves(status, identity),
                    "identity": identity,
                    "framework_version": str(status.get("framework_version", ""))[:64],
                    "legacy": legacy}
        except (OSError, ValueError, KeyError, IndexError):
            return {"active": True, "generation": None, "proved": False}

    def jobs(self, timeout):
        raw = self.command(["systemctl", "list-jobs", "--no-legend", "--no-pager"], timeout)
        return [int(parts[0]) for line in raw.splitlines()
                if len(parts := line.split()) >= 2 and parts[1] == UNIT and parts[0].isdigit()]

    def restart(self, timeout):
        reply = self.command(["systemctl", "try-restart", "--no-block",
                              "--job-mode=fail", "--show-transaction", UNIT],
                             timeout, include_stderr=True)
        # systemd 252 reports the anchor's id on stderr. Never infer ownership
        # from a later unit Job: an operator may have submitted a stop meanwhile.
        return [int(match) for match in re.findall(
            r"Enqueued anchor job (\d+) mojosec\.service/[^\s]+", reply)]

    def cancel(self, jobs, timeout):
        if jobs:
            self.command(["systemctl", "cancel", *map(str, jobs)], timeout)


class Refresh:
    def __init__(self, path=EVIDENCE_PATH, *, host=None, clock=None, sleep=None,
                 owner_uid=0, writer=None):
        self.path = path
        self.host = host
        self.clock = clock or time.monotonic
        self.sleep = sleep or time.sleep
        self.owner_uid = owner_uid
        self.writer = writer or durable_write
        self.state = {"schema": 1, "attempts": {}, "failures": []}
        self.attempt = None

    def remaining(self):
        return max(0, self.deadline - self.clock())

    def call(self, method, *args):
        budget = min(5, self.remaining())
        if budget <= 0:
            raise TimeoutError("refresh deadline exhausted")
        return method(*args, timeout=budget)

    def save(self):
        self.state["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.writer(self.path, (json.dumps(self.state, separators=(",", ":")) + "\n").encode(),
                    owner_uid=self.owner_uid)

    def finish(self, outcome, reason=""):
        self.attempt.update(outcome=outcome, reason=reason[:160])
        self.state["latest"] = dict(self.attempt)
        if outcome == "degraded":
            self.state["failures"] = (self.state.get("failures", []) + [dict(self.attempt)])[-8:]
            print("mojosec refresh degraded: " + reason[:160], file=sys.stderr)
        self.save()
        return dict(self.attempt)

    def reconcile(self, attempt):
        """Never submit while any earlier job may still affect this service."""
        if not attempt.get("pending"):
            return True
        if attempt.get("boot_id") != self.host.boot_id:
            attempt.update(pending=False, jobs=[], reconciled=True)
            self.save()
            return True
        jobs = self.call(self.host.jobs)
        attempt["jobs"] = jobs
        owned = [job for job in jobs if job in attempt.get("owned_jobs", [])]
        if owned and self.remaining() > 0:
            self.call(self.host.cancel, owned)
            jobs = self.call(self.host.jobs)
        transitional = False
        if not jobs:
            # Cancelling a job does not necessarily abort an already-running
            # service transition. Retain uncertainty until the unit settles.
            observed = self.call(self.host.observe)
            transitional = observed.get("state") in ("activating", "deactivating", "reloading")
        attempt["pending"] = bool(jobs) or transitional
        attempt["jobs"] = jobs
        attempt["reconciled"] = not attempt["pending"]
        self.save()
        return not attempt["pending"]

    def run(self, deployment, direction, target=""):
        self.deadline = self.clock() + 60
        lock = None
        locked = False
        evidence_loaded = False
        try:
            if direction not in ("candidate", "rollback") or not deployment or len(deployment) > 160:
                raise ValueError("invalid deployment context")
            parent = os.path.dirname(self.path)
            if not os.path.exists(parent):
                os.makedirs(parent, mode=0o700)
            info = os.lstat(parent)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != self.owner_uid or info.st_mode & 0o022:
                raise ValueError("unsafe evidence directory")
            lock = os.open(self.path + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            info = os.fstat(lock)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != self.owner_uid or info.st_mode & 0o077 or info.st_nlink != 1:
                raise ValueError("unsafe evidence lock")
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
            try:
                self.state = read_json(self.path, self.owner_uid, private=True)
            except FileNotFoundError:
                pass
            if self.state.get("schema") != 1 or not isinstance(self.state.get("attempts"), dict):
                raise ValueError("malformed refresh evidence")
            evidence_loaded = True
            self.host = self.host or Host()
            installed = self.call(self.host.version)
            target = target or installed
            if not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z._!+-]{0,63}", target):
                raise ValueError("installed framework version unavailable")
            key = hashlib.sha256(json.dumps([deployment, direction, target]).encode()).hexdigest()
            attempts = self.state["attempts"]
            existing = attempts.get(key)
            # A duplicate shares the original budget on this boot, never a new restart.
            if existing and existing.get("boot_id") == self.host.boot_id:
                self.deadline = min(self.deadline, existing["deadline"])
            self.attempt = existing or {"deployment": deployment, "direction": direction,
                                       "target": target, "boot_id": self.host.boot_id,
                                       "deadline": self.deadline, "outcome": "pending",
                                       "pending": False, "jobs": []}
            if not existing:
                # Keep recent completed transactions and every unresolved job.
                recent = list(attempts)[-32:]
                attempts = {k: v for k, v in attempts.items() if k in recent or
                            v.get("deployment") == deployment or v.get("pending")}
                self.state["attempts"] = attempts
                if len(attempts) >= 64:
                    raise ValueError("too many unresolved refresh attempts")
                attempts[key] = self.attempt
            self.save()
            # Reconciliation after an expired attempt gets a small read/cancel
            # window but never renews that attempt's restart/proof deadline.
            original_deadline = self.deadline
            if self.remaining() <= 0:
                self.deadline = self.clock() + 5
            for prior in attempts.values():
                if not self.reconcile(prior):
                    return self.finish("degraded", "unresolved systemd job")
            self.deadline = original_deadline
            if installed != target:
                return self.finish("degraded", "installed version differs from deployment target")
            if existing:
                if existing.get("outcome") in ("current", "refreshed", "pending"):
                    # Recovery may verify a completed generation after the
                    # attempt expired, but cannot renew its restart budget.
                    if self.remaining() <= 0:
                        self.deadline = self.clock() + 5
                    observed = self.call(self.host.observe)
                    if not (observed.get("proved") and observed.get("framework_version") == installed):
                        return self.finish("degraded", "duplicate hook found runtime drift; restart already consumed")
                    if existing.get("outcome") == "pending":
                        return self.finish("refreshed", "interrupted attempt reconciled")
                elif existing.get("outcome") == "degraded":
                    print("mojosec refresh degraded: " + existing.get("reason", "previous attempt failed"), file=sys.stderr)
                return dict(existing)
            before = self.call(self.host.observe)
            self.attempt["before"] = before.get("generation")
            if not before["active"]:
                return self.finish("skipped", "service inactive or absent")
            if before.get("proved") and before.get("framework_version") == installed:
                return self.finish("current")
            # Also reconcile externally queued jobs before submitting our own.
            if self.call(self.host.jobs):
                return self.finish("degraded", "service already has a systemd job")
            self.attempt.update(pending=True, restart_requested=True)
            self.save()  # Durable uncertainty BEFORE the side effect.
            self.attempt["owned_jobs"] = self.call(self.host.restart) or []
            self.save()
            self.attempt["jobs"] = self.call(self.host.jobs)
            self.save()
            while self.remaining() > 5:
                observed = self.call(self.host.observe)
                jobs = self.call(self.host.jobs)
                transitional = observed.get("state") in ("activating", "deactivating", "reloading")
                self.attempt.update(jobs=jobs, pending=bool(jobs) or transitional, after=observed.get("generation"))
                replacement = observed.get("generation") and observed["generation"] != before.get("generation")
                if not jobs and replacement and observed.get("proved") and observed.get("framework_version") == installed:
                    return self.finish("refreshed")
                if not jobs and not observed["active"] and not transitional:
                    return self.finish("degraded", "service stopped; no implicit start")
                if not jobs and replacement and observed.get("legacy"):
                    return self.finish("degraded", "replacement observed; loaded-version proof unavailable")
                self.sleep(min(0.5, max(0, self.remaining() - 5)))
            self.attempt["pending"] = True
            self.reconcile(self.attempt)
            return self.finish("degraded", "refresh deadline exhausted; loaded-version proof unavailable")
        except Exception as error:
            reason = type(error).__name__ + ": " + str(error)[:120]
            print("mojosec refresh degraded: " + reason, file=sys.stderr)
            if not locked or not evidence_loaded:
                # A competing writer may own an unresolved restart. Never
                # replace its evidence without the lock, even on an error path.
                try:
                    self.writer(self.path + ".error", json.dumps({
                        "outcome": "degraded", "reason": reason}).encode(),
                        owner_uid=self.owner_uid)
                except Exception:
                    print("mojosec refresh: error evidence write failed", file=sys.stderr)
                return {"outcome": "degraded", "reason": reason, "evidence_failed": True}
            if self.attempt is None:
                self.attempt = {"deployment": str(deployment)[:160], "direction": str(direction)[:16],
                                "target": str(target)[:64], "pending": False}
            try:
                return self.finish("degraded", reason)
            except Exception:
                print("mojosec refresh: evidence write failed", file=sys.stderr)
                return {"outcome": "degraded", "reason": reason, "evidence_failed": True}
        finally:
            if lock is not None:
                os.close(lock)


def diagnostic_snapshot():
    """Read-only bounded projection; no configuration, credential or spool reads."""
    host = Host()
    observed = host.observe(5)
    result = {"installed": host.version(5), "runtime": observed}
    try:
        state = read_json(EVIDENCE_PATH, private=True)
        latest = state.get("latest", {})
        fields = ("deployment", "direction", "target", "outcome", "reason", "pending")
        result["refresh"] = {key: value[:160] if isinstance(value, str) else value
                             for key in fields if isinstance(
                                 value := latest.get(key), (str, bool))}
        result["refresh"]["pending"] = any(
            attempt.get("pending") for attempt in state.get("attempts", {}).values())
        result["recent_failures"] = len(state.get("failures", []))
    except FileNotFoundError:
        result["refresh"] = None
    except (OSError, ValueError, AttributeError, TypeError):
        result["refresh"] = {"outcome": "degraded", "reason": "unreadable refresh evidence"}
    try:
        failure = read_json(EVIDENCE_PATH + ".error", private=True)
        result["observer_error"] = str(failure.get("reason", "observer evidence failure"))[:160]
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError):
        result["observer_error"] = "unreadable observer error evidence"
    return result


def record_error(state, reason, owner_uid=0):
    """Preparation failures survive transaction deletion as observer evidence."""
    paths = [EVIDENCE_PATH, os.path.join(state, "mojosec_refresh_error.json")]
    for path in paths:
        lock = None
        try:
            parent = os.path.dirname(path)
            os.makedirs(parent, mode=0o700, exist_ok=True)
            info = os.lstat(parent)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != owner_uid or info.st_mode & 0o022:
                raise ValueError("unsafe preparation evidence directory")
            lock = os.open(path + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            info = os.fstat(lock)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != owner_uid or info.st_mode & 0o077 or info.st_nlink != 1:
                raise ValueError("unsafe preparation evidence lock")
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                value = read_json(path, owner_uid, private=True)
            except FileNotFoundError:
                value = {"schema": 1, "attempts": {}, "failures": []}
            failure = {"outcome": "degraded", "reason": str(reason)[:160]}
            value["latest"] = failure
            value["failures"] = (value.get("failures", []) + [failure])[-8:]
            durable_write(path, json.dumps(value).encode(), owner_uid=owner_uid)
        except Exception:
            print("mojosec refresh: preparation evidence write failed", file=sys.stderr)
        finally:
            if lock is not None:
                os.close(lock)


def retain(state, source=None, owner_uid=0, writer=None):
    """Install helper, original, then wrapper; recovery never saves a wrapper."""
    writer = writer or durable_write
    source = source or __file__
    helper = os.path.join(state, "mojosec_refresh.py")
    if os.path.abspath(source) != os.path.abspath(helper):
        writer(helper, read_bytes(source, owner_uid=owner_uid), owner_uid=owner_uid)
    previous = os.path.join(state, "previous_post.sh")
    original = os.path.join(state, "previous_post.original.sh")
    marker = b"# mojosec-refresh-wrapper-v1\n"
    if not os.path.exists(previous):
        return
    body = read_bytes(previous, owner_uid=owner_uid)
    if os.path.exists(original):
        saved = read_bytes(original, owner_uid=owner_uid)
        if marker in saved:
            raise ValueError("saved original is a wrapper")
    elif marker in body:
        raise ValueError("wrapper original is missing")
    else:
        writer(original, body, mode=0o700, owner_uid=owner_uid)
    wrapper = b'''#!/bin/bash
# mojosec-refresh-wrapper-v1
state="$(cd "$(dirname "$0")" && pwd)"
if ! /usr/bin/python3 -E -s "$state/mojosec_refresh.py" --state "$state" --direction rollback; then
    echo "mojosec refresh degraded: retained rollback helper failed" >&2
fi
exec bash "$state/previous_post.original.sh" "$@"
'''
    writer(previous, wrapper, mode=0o700, owner_uid=owner_uid)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True)
    parser.add_argument("--direction", choices=("candidate", "rollback"), default="candidate")
    parser.add_argument("--prepare", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.prepare:
            retain(args.state)
            return 0
        deployment = read_bytes(os.path.join(args.state, "deployment"), limit=160).decode().strip()
        started = read_bytes(os.path.join(args.state, "started_at"), limit=40).decode().strip()
        deployment = deployment if deployment != "manual" else "manual-" + started
        name = "previous_framework" if args.direction == "rollback" else "candidate_framework"
        try:
            target = read_bytes(os.path.join(args.state, name), limit=80).decode().strip()
        except FileNotFoundError:
            target = ""
        Refresh().run(deployment, args.direction, target)
    except Exception as error:
        print("mojosec refresh preparation degraded: " + str(error)[:160], file=sys.stderr)
        record_error(args.state, str(error))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
