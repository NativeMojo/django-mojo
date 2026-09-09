"""Refresh state machine tests use private files and an injected system boundary."""

import datetime
import json
import os
import tempfile

from testit import helpers as th


class Clock:
    value = 100

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class Host:
    boot_id = "test-boot"

    def __init__(self, mode="success", current=False, active=True):
        self.mode, self.active = mode, active
        self.loaded = "2" if current else "1"
        self.generation = 100
        self.requests = 0
        self.job = []
        self.cancellations = 0
        self.budgets = []

    def version(self, timeout):
        self.budgets.append(timeout)
        return "2"

    def observe(self, timeout):
        self.budgets.append(timeout)
        return {"active": self.active, "generation": [self.boot_id, 42, self.generation],
                "framework_version": self.loaded, "proved": self.loaded == "2",
                "legacy": self.mode == "legacy" and self.requests > 0}

    def jobs(self, timeout):
        self.budgets.append(timeout)
        return self.job[:]

    def restart(self, timeout):
        self.budgets.append(timeout)
        self.requests += 1
        if self.mode == "refused":
            raise RuntimeError("restart refused")
        if self.mode == "stop":
            self.active = False
        elif self.mode in ("pending", "uncancellable"):
            self.job = [91]
        elif self.mode != "stale":
            self.generation = 200  # Same PID, different kernel generation.
            if self.mode != "legacy":
                self.loaded = "2"
        return self.job[:]

    def cancel(self, jobs, timeout):
        self.budgets.append(timeout)
        self.cancellations += 1
        if self.mode != "uncancellable":
            self.job = []


def execute(root, host, clock=None, deployment="deploy-1", direction="candidate", **kwargs):
    from mojo.deploy.mojosec_refresh import Refresh

    clock = clock or Clock()
    return Refresh(os.path.join(root, "state.json"), host=host, clock=clock,
                   sleep=clock.sleep, owner_uid=os.getuid(), **kwargs).run(deployment, direction, "2")


@th.django_unit_test()
def test_current_and_inactive_services_are_not_restarted(opts):
    for current, active, outcome in ((True, True, "current"), (False, False, "skipped")):
        with tempfile.TemporaryDirectory() as root:
            host = Host(current=current, active=active)
            result = execute(root, host)
            assert result["outcome"] == outcome, "current and inactive services must take their explicit no-op paths"
            assert host.requests == 0, "no-op refresh must never submit a restart"


@th.django_unit_test()
def test_pid_reuse_is_a_replacement_and_duplicate_success_does_not_restart(opts):
    with tempfile.TemporaryDirectory() as root:
        host = Host()
        first = execute(root, host)
        second = execute(root, host)
        assert first["outcome"] == second["outcome"] == "refreshed", "same PID with new ticks must prove replacement"
        assert first["before"] != first["after"], "kernel generation must change across replacement"
        assert host.requests == 1, "updater and activation hooks must share the restart"


@th.django_unit_test()
def test_failed_and_pending_attempts_are_never_restarted_by_duplicate_hooks(opts):
    for mode in ("refused", "stale", "pending", "uncancellable", "stop", "legacy"):
        with tempfile.TemporaryDirectory() as root:
            host, clock = Host(mode), Clock()
            first = execute(root, host, clock)
            second = execute(root, host, clock)
            assert first["outcome"] == second["outcome"] == "degraded", f"{mode} must remain non-vetoing degraded evidence"
            assert host.requests == 1, f"{mode} duplicate hooks must not issue another restart"
            assert max(host.budgets) <= 5, f"{mode} subprocess queries must be capped at five seconds"
            assert clock.value <= 165, f"{mode} must share a sixty-second deadline plus bounded later reconciliation"


@th.django_unit_test()
def test_rollback_reconciles_uncancellable_candidate_job_before_any_new_restart(opts):
    with tempfile.TemporaryDirectory() as root:
        host, clock = Host("uncancellable"), Clock()
        first = execute(root, host, clock)
        rollback = execute(root, host, clock, direction="rollback")
        assert first["pending"] is True, "an unresolved late job must remain durable"
        assert rollback["outcome"] == "degraded", "rollback must report unresolved candidate jobs"
        assert host.requests == 1, "rollback must not race a pending candidate restart"
        host.job = []
        duplicate = execute(root, host, clock, direction="rollback")
        assert duplicate["outcome"] == "degraded", "failed rollback hooks must remain consumed after late completion"
        assert host.requests == 1, "late completion must not renew a consumed attempt"


@th.django_unit_test()
def test_timeout_cancels_jobs_and_retains_failure_history(opts):
    with tempfile.TemporaryDirectory() as root:
        host, clock = Host("pending"), Clock()
        result = execute(root, host, clock)
        assert result["pending"] is False and host.cancellations == 1, "deadline must reconcile and cancel the captured job"
        host.mode = "success"
        success = execute(root, host, clock, deployment="deploy-2")
        with open(os.path.join(root, "state.json")) as handle:
            state = json.load(handle)
        assert success["outcome"] == "refreshed", "a later deployment may repair the sensor"
        assert state["failures"], "later success must retain bounded earlier failure evidence"
        assert os.stat(os.path.join(root, "state.json")).st_mode & 0o777 == 0o600, "evidence must be owner-only"


@th.django_unit_test()
def test_reconciliation_never_cancels_an_operators_replacement_stop_job(opts):
    from mojo.deploy.mojosec_refresh import Refresh

    with tempfile.TemporaryDirectory() as root:
        host, clock = Host("uncancellable"), Clock()
        refresh = Refresh(os.path.join(root, "state.json"), host=host,
                          clock=clock, sleep=clock.sleep, owner_uid=os.getuid())
        refresh.deadline = clock() + 60
        host.job = [92]
        attempt = {"pending": True, "owned_jobs": [91], "boot_id": host.boot_id}
        resolved = refresh.reconcile(attempt)
        assert resolved is False, "an unrelated outstanding job must remain unresolved"
        assert host.cancellations == 0, "only the captured restart job may be cancelled, never an operator stop"


@th.django_unit_test()
def test_cancelled_job_remains_pending_until_service_transition_settles(opts):
    from mojo.deploy.mojosec_refresh import Refresh

    class Transitioning(Host):
        def observe(self, timeout):
            return {"active": False, "state": "activating", "proved": False}

    with tempfile.TemporaryDirectory() as root:
        host, clock = Transitioning(), Clock()
        host.job = [91]
        refresh = Refresh(os.path.join(root, "state.json"), host=host,
                          clock=clock, sleep=clock.sleep, owner_uid=os.getuid())
        refresh.deadline = clock() + 60
        attempt = {"pending": True, "owned_jobs": [91], "boot_id": host.boot_id}
        assert refresh.reconcile(attempt) is False, "job removal cannot prove an in-progress service transition has stopped"
        assert attempt["pending"] is True and not attempt["jobs"], "pending transition must survive after job cancellation"


@th.django_unit_test()
def test_evidence_write_failure_prevents_restart_without_raising(opts):
    def broken_writer(*args, **kwargs):
        raise OSError("disk full")

    with tempfile.TemporaryDirectory() as root:
        host = Host()
        result = execute(root, host, writer=broken_writer)
        assert result["evidence_failed"] is True, "unwritable evidence must be reported explicitly"
        assert host.requests == 0, "no side effect is permitted before durable intent"


@th.django_unit_test()
def test_installed_target_disagreement_and_malformed_state_never_restart(opts):
    from mojo.deploy.mojosec_refresh import Refresh

    with tempfile.TemporaryDirectory() as root:
        host = Host()
        path = os.path.join(root, "state.json")
        result = Refresh(path, host=host, owner_uid=os.getuid()).run("deploy", "candidate", "wrong")
        assert result["outcome"] == "degraded", "installed metadata disagreement must not be called successful proof"
        assert host.requests == 0, "unknown/mismatched installed target must not restart a service"
        with open(path, "w") as handle:
            handle.write("{malformed")
        result = Refresh(path, host=host, owner_uid=os.getuid()).run("deploy", "candidate", "2")
        assert result["outcome"] == "degraded", "malformed durable evidence must be loud and non-vetoing"
        assert host.requests == 0, "unreadable attempt history must not risk a duplicate restart"


@th.django_unit_test()
def test_lost_restart_reply_keeps_intent_and_duplicate_never_resubmits(opts):
    class LostReply(Host):
        def restart(self, timeout):
            self.requests += 1
            self.job = [91]
            raise TimeoutError("reply lost after submission")

    with tempfile.TemporaryDirectory() as root:
        host = LostReply()
        first = execute(root, host)
        duplicate = execute(root, host)
        assert first["pending"] and duplicate["outcome"] == "degraded", "submission uncertainty must survive the helper process"
        assert host.requests == 1, "a lost systemd reply must never cause another restart"
        assert host.cancellations == 0, "an unowned job cannot be cancelled from a guessed identity"


@th.django_unit_test()
def test_status_proof_rejects_missing_future_old_and_wrong_generation(opts):
    from mojo.deploy.mojosec_refresh import proves

    identity = {"pid": 42, "boot_id": "a" * 36, "process_start_ticks": 10,
                "process_started_at": "2026-01-01T00:00:00+00:00"}
    now = datetime.datetime(2026, 1, 1, 0, 1, tzinfo=datetime.timezone.utc).timestamp()
    good = dict(identity, framework_version="2", running=True, updated_at="2026-01-01T00:00:30+00:00")
    assert proves(good, identity, now), "fresh identity from the kernel generation must pass"
    for change in ({"pid": 43}, {"boot_id": "other"}, {"process_start_ticks": 11},
                   {"updated_at": "2026-01-02T00:00:00+00:00"},
                   {"updated_at": "2025-12-31T23:59:59+00:00"},
                   {"running": False}, {"framework_version": ""},
                   {"process_started_at": "2026-01-01T00:00:00"}):
        assert not proves(dict(good, **change), identity, now), f"unsafe identity change must fail proof: {change}"
    assert not proves({}, identity, now), "legacy missing fields cannot prove loaded version"
    assert not proves(good, identity, now + 121), "stale status cannot prove a live generation"


@th.django_unit_test()
def test_descriptor_reader_refuses_symlinks_nonregular_and_oversized_files(opts):
    from mojo.deploy.mojosec_refresh import read_bytes

    with tempfile.TemporaryDirectory() as root:
        path = os.path.join(root, "file")
        with open(path, "wb") as handle:
            handle.write(b"data")
        os.chmod(path, 0o600)
        link = os.path.join(root, "link")
        os.symlink(path, link)
        for candidate, limit in ((link, 20), (root, 20), (path, 2)):
            try:
                read_bytes(candidate, limit=limit, owner_uid=os.getuid())
            except (OSError, ValueError):
                pass
            else:
                assert False, f"unsafe or unbounded read must be rejected: {candidate}"


@th.django_unit_test()
def test_durable_evidence_syncs_file_before_parent_directory(opts):
    import stat
    from mojo.deploy.mojosec_refresh import durable_write

    synced = []

    def sync(fd):
        synced.append("directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        os.fsync(fd)

    with tempfile.TemporaryDirectory() as root:
        durable_write(os.path.join(root, "evidence"), b"{}", owner_uid=os.getuid(), sync=sync)
        assert synced == ["file", "directory"], "atomic replacement must be preceded by file fsync and followed by parent fsync"


@th.django_unit_test()
def test_host_brackets_process_generation_and_never_enables_or_starts(opts):
    from mojo.deploy.mojosec_refresh import Host as RealHost, durable_write
    from types import SimpleNamespace

    with tempfile.TemporaryDirectory() as root:
        os.makedirs(os.path.join(root, "sys/kernel/random"))
        os.makedirs(os.path.join(root, "42"))
        for name, data in (("sys/kernel/random/boot_id", b"01234567-0123-0123-0123-012345678901"),
                           ("stat", b"btime 1767225600\n"),
                           ("42/stat", ("42 (sensor) " + " ".join(["0"] * 19 + ["100"])).encode())):
            durable_write(os.path.join(root, name), data, owner_uid=os.getuid())
        # Constructor reads its own PID; use an injected object for this fixture.
        host = RealHost.__new__(RealHost)
        host.proc_root, host.status_path = root, os.path.join(root, "missing")
        commands = []

        def runner(argv, **kwargs):
            commands.append(argv)
            return SimpleNamespace(returncode=0, stdout="ActiveState=active\nMainPID=42\nLoadState=loaded\nJob=0\n", stderr="")

        host.runner = runner
        result = host.observe(5)
        host.restart(5)
        assert result["proved"] is False, "missing status cannot prove a running process"
        assert len([c for c in commands if c[1] == "show"]) == 2, "status reads must be bracketed by systemd observations"
        assert commands[-1] == ["systemctl", "try-restart", "--no-block", "--job-mode=fail", "--show-transaction", "mojosec.service"], "restart must preserve concurrent stop jobs and never enable inactive services"
