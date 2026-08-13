from testit import helpers as th
import os
import tempfile
from unittest import mock


def _record(serial, kind, **values):
    record = {
        "_BOOT_ID": "a" * 32,
        "__MONOTONIC_TIMESTAMP": str(values.pop("monotonic", 100)),
        "MESSAGE": f"audit(1780000000.123:{serial}): type={kind}",
        "_AUDIT_TYPE_NAME": kind,
    }
    record.update(values)
    return record


@th.unit_test("audit compounds assemble across polls and terminate only at EOE")
def test_compound_assembly(opts):
    from mojo.mojosec.lineage import CompoundAssembler

    assembler = CompoundAssembler()
    first = assembler.ingest([
        _record(77, "EXECVE", argc="2", a0="/usr/bin/sudo", a1="-n"),
        _record(77, "SYSCALL", pid="42", ppid="10", auid="1000", uid="1000",
                euid="0", ses="9", tty="pts0", exe="/usr/bin/sudo"),
    ])
    th.assert_eq(first["complete"], [], "a compound without EOE must remain pending")
    second = assembler.ingest([_record(77, "EOE")])
    th.assert_eq(len(second["complete"]), 1, "EOE must complete the pending compound")
    node = second["complete"][0]
    th.assert_eq(node["pid"], 42, "the syscall PID must survive compound assembly")
    th.assert_eq(node["argv"], ["/usr/bin/sudo", "-n"],
                 "EXECVE arguments must be reconstructed by numeric index")


@th.unit_test("audit compounds reject conflicting duplicate fields")
def test_compound_conflict_is_ambiguous(opts):
    from mojo.mojosec.lineage import CompoundAssembler

    assembler = CompoundAssembler()
    result = assembler.ingest([
        _record(88, "SYSCALL", pid="42", ppid="10", exe="/usr/bin/sudo"),
        _record(88, "SYSCALL", pid="43", ppid="10", exe="/usr/bin/sudo"),
        _record(88, "EOE"),
    ])
    th.assert_true(result["complete"][0]["ambiguous"],
                   "conflicting rows in one Audit serial must fail ambiguous")


@th.unit_test("audit compound timeout emits an incomplete fail-open generation")
def test_compound_timeout_is_incomplete(opts):
    from mojo.mojosec import lineage

    assembler = lineage.CompoundAssembler()
    with mock.patch.object(lineage.time, "time", return_value=100.0):
        assembler.ingest([_record(44, "SYSCALL", pid="8", ppid="1")])
    with mock.patch.object(lineage.time, "time", return_value=102.1):
        result = assembler.ingest([])
    th.assert_true(result["complete"][0]["incomplete"] and
                   result["complete"][0]["ambiguous"],
                   "a compound without EOE must close after two seconds but never prove lineage")


@th.unit_test("event lineage projection is bounded")
def test_project_ancestors_is_bounded(opts):
    from mojo.mojosec.lineage import project_ancestors

    ancestors = [{"pid": i, "exe": f"/bin/p{i}"} for i in range(20)]
    projected = project_ancestors(ancestors)
    th.assert_eq(len(projected), 8, "central Event evidence may carry at most eight ancestors")


@th.unit_test("proven firewall operation resolves local-only and incomplete proof fails open")
def test_pending_firewall_resolution(opts):
    from mojo.mojosec.events import observation
    from mojo.mojosec.store import Store

    aggregation = {"window_seconds": 60, "flush_count": 10,
                   "max_aggregates": 100, "critical_reserve_aggregates": 10}
    delivery = {"max_spool_events": 100, "critical_reserve_events": 10,
                "retry_min_seconds": 1, "retry_max_seconds": 60}
    boot = "a" * 32
    session = 9
    candidate = observation(
        "auth.sudo_command", "high", "Privileged sudo command executed",
        attributes={
            "actor": "ec2-user", "target_user": "root", "boot_id": boot,
            "audit_session": session, "attribution_provenance": "none",
            "producer_pid": 20, "monotonic": 1_000_000,
            "command": "/usr/local/sbin/mojo-firewall-broker",
            "command_path": "/usr/local/sbin/mojo-firewall-broker",
            "command_sha256": "b" * 64,
        }, fingerprint_values=("candidate",), aggregate=False,
        recommendation="review", observed_at="2026-08-13T20:00:00Z")
    health = {
        "schema": "mojosec.audit-health", "version": 1, "boot_id": boot,
        "generation": "c" * 64, "rules_sha256": "d" * 64,
        "sequence": 1, "enabled": 1, "failure": 1, "rate_limit": 0,
        "backlog_limit": 8192, "backlog": 0, "lost": 0,
        "updated_at": 1.0, "healthy": True, "reason": "",
    }
    begin = {
        "operation_id": "e" * 32, "kind": "begin", "boot_id": boot,
        "audit_session": session, "broker_pid": 22, "broker_start_ticks": 220,
        "execution_id": "f" * 32, "job_id": "1" * 32,
        "function": "mojo.apps.incident.asyncjobs.broadcast_block_ip",
        "operation": "rule.insert", "semantic": "-I INPUT source DROP",
        "argv_digest": "2" * 64, "stdin_digest": "3" * 64,
        "stdin_length": 0, "count": 0, "target_exe": "/sbin/iptables",
        "monotonic_ns": 1_000_000_000,
    }
    result = dict(begin, kind="result", target_pid=23, target_start_ticks=230,
                  monotonic_ns=1_100_000_000, ok=True)
    nodes = [
        {"boot_id": boot, "audit_serial": 5, "pid": 19, "ppid": 1,
         "audit_session": session, "exe": "/usr/sbin/crond", "argv": ["/usr/sbin/crond"]},
        {"boot_id": boot, "audit_serial": 6, "pid": 18, "ppid": 19,
         "audit_session": session, "exe": "/usr/bin/python3",
         "argv": ["python3", "-m", "mojo.deploy.jobman", "start"]},
        {"boot_id": boot, "audit_serial": 1, "pid": 20, "ppid": 21,
         "audit_session": session, "exe": "/usr/bin/sudo", "argv": ["/usr/bin/sudo"]},
        {"boot_id": boot, "audit_serial": 2, "pid": 21, "ppid": 18,
         "audit_session": session, "exe": "/usr/bin/python3",
         "argv": ["/opt/api/bin/jobs.py", "engine", "foreground"], "pinned": True},
        {"boot_id": boot, "audit_serial": 3, "pid": 22, "ppid": 20,
         "audit_session": session, "start_ticks": 220, "exe": "/usr/bin/python3", "argv": []},
        {"boot_id": boot, "audit_serial": 4, "pid": 23, "ppid": 22,
         "audit_session": session, "start_ticks": 230, "exe": "/sbin/iptables", "argv": []},
    ]
    with tempfile.TemporaryDirectory() as root:
        os.chmod(root, 0o700)
        store = Store(root, "sensor", aggregation, delivery,
                      local_only_diagnostic_path=os.path.join(root, "missing"))
        store.ingest([candidate], audit_health=health, process_nodes=nodes,
                     firewall_receipts=[begin, result])
        th.assert_eq(store.stats()["local_only_suppressed"], 1,
                     "complete healthy process and receipt proof should suppress centrally")
        th.assert_eq(store.pending_batch(10, 65536), [],
                     "proven expected automation must not create an ordinary Event")
        store.close()
