from testit import helpers as th
import hashlib
import json
import os
import tempfile
import time
from unittest import mock


def _record(serial, kind, **values):
    record = {
        "_BOOT_ID": "a" * 32,
        "_AUDIT_ID": str(serial),
        "_TRANSPORT": "audit",
        "__MONOTONIC_TIMESTAMP": str(values.pop("monotonic", 100)),
        "MESSAGE": str(values.pop("message", f"type={kind}")),
        "_AUDIT_TYPE_NAME": kind,
    }
    record.update(values)
    return record


@th.unit_test("audit compounds assemble across polls and terminate only at EOE")
def test_compound_assembly(opts):
    from mojo.mojosec.lineage import CompoundAssembler

    assembler = CompoundAssembler()
    first = assembler.ingest([
        _record(77, "EXECVE", _AUDIT_FIELD_ARGC="3",
                _AUDIT_FIELD_A0="/usr/bin/sudo", _AUDIT_FIELD_A1="-n",
                _AUDIT_FIELD_A2="--"),
        _record(77, "SYSCALL", _AUDIT_FIELD_PID="42", _AUDIT_FIELD_PPID="10",
                _AUDIT_LOGINUID="1000", _UID="1000", _AUDIT_FIELD_EUID="0",
                _AUDIT_SESSION="9", _AUDIT_FIELD_TTY="pts0",
                _AUDIT_FIELD_EXE="/usr/bin/sudo", _AUDIT_FIELD_SUCCESS="yes",
                _AUDIT_FIELD_EXIT="0", _SELINUX_CONTEXT="system_u:system_r:sudo_t:s0"),
    ])
    th.assert_eq(first["complete"], [], "a compound without EOE must remain pending")
    second = assembler.ingest([_record(77, "EOE")])
    th.assert_eq(len(second["complete"]), 1, "EOE must complete the pending compound")
    node = second["complete"][0]
    th.assert_eq(node["pid"], 42, "the syscall PID must survive compound assembly")
    th.assert_eq(node["argv"], ["/usr/bin/sudo", "-n", "--"],
                 "EXECVE arguments must be reconstructed by numeric index")
    th.assert_true(node["success"] and node["eoe"],
                   "suppression proof requires a successful syscall and EOE")


@th.unit_test("production-shaped journald Audit records use boot plus Audit serial")
def test_production_journal_shape(opts):
    from mojo.mojosec.lineage import CompoundAssembler, compound_key

    # Sanitized from systemd's Audit transport JSON shape: _AUDIT_ID is the
    # kernel serial, MESSAGE begins with the Audit type, and trusted kernel
    # fields are also materialized as _AUDIT_FIELD_* by journald.
    syscall = _record(
        9012, "SYSCALL",
        message=('SYSCALL arch=c000003e syscall=59 success=yes exit=0 '
                 'pid=1442 ppid=1439 uid=1000 euid=0 auid=1000 ses=71 '
                 'tty=pts1 exe=/usr/bin/sudo subj=system_u:system_r:sudo_t:s0'),
        _PID="0", _UID="0", _AUDIT_LOGINUID="1000", _AUDIT_SESSION="71")
    execve_one = _record(
        9012, "EXECVE",
        message='EXECVE argc=3 a0=/usr/bin/sudo a1=-n')
    execve_two = _record(
        9012, "EXECVE",
        message='EXECVE argc=3 a2=/usr/bin/id')
    result = CompoundAssembler().ingest(
        [execve_two, syscall, execve_one, _record(9012, "EOE")])
    node = result["complete"][0]
    th.assert_eq(compound_key(syscall), ("a" * 32, "9012"),
                 "the compound identity must use the exact journald Audit serial")
    th.assert_eq(
        (node["pid"], node["ppid"], node["auid"], node["audit_session"],
         node["tty"], node["exe"]),
        (1442, 1439, 1000, 71, "pts1", "/usr/bin/sudo"),
        "production MESSAGE fields must normalize without synthetic audit(...) parsing")
    th.assert_eq(node["argv"], ["/usr/bin/sudo", "-n", "/usr/bin/id"],
                 "split EXECVE rows must merge every bounded argument")
    th.assert_true(node["success"] and node["eoe"] and not node["ambiguous"],
                   "complete successful production-shaped compounds may prove an edge")
    synthetic = dict(syscall, _AUDIT_ID="1780000000.123:9012")
    th.assert_eq(compound_key(synthetic), None,
                 "legacy synthetic timestamp identities must not be accepted")


@th.unit_test("production CROND launch requires exact trusted syslog and PAM halves")
def test_production_crond_launch_shape(opts):
    from mojo.mojosec.lineage import CROND_PAM_GRANTORS, CROND_SELINUX, crond_launch

    boot = "a" * 32
    command = "/opt/api/bin/jobman start >> /opt/api/var/logs/jobman.log 2>&1"
    common = {"_BOOT_ID": boot, "_UID": "0",
              "_AUDIT_LOGINUID": "1000", "_AUDIT_SESSION": "71",
              "_SELINUX_CONTEXT": CROND_SELINUX}
    syslog = dict(common, _TRANSPORT="syslog", _PID="190", _GID="1000",
                  _COMM="crond", _EXE="/usr/sbin/crond", SYSLOG_IDENTIFIER="CROND",
                  _CMDLINE="/usr/sbin/CROND -n",
                  MESSAGE=f"(ec2-user) CMD ({command})",
                  _SYSTEMD_UNIT="session-71.scope",
                  _SYSTEMD_CGROUP="/user.slice/user-1000.slice/session-71.scope",
                  __MONOTONIC_TIMESTAMP="200")
    pam = dict(common, _TRANSPORT="audit", _AUDIT_TYPE_NAME="USER_START",
               __MONOTONIC_TIMESTAMP="100",
               MESSAGE=('USER_START pid=411 uid=0 auid=1000 ses=71 '
                        'msg=\'op=PAM:session_open '
                        'grantors=pam_loginuid,pam_keyinit,pam_limits,pam_systemd '
                        'acct="ec2-user" '
                        'exe="/usr/sbin/crond" hostname=? addr=? terminal=cron '
                        'res=success\''))
    th.assert_eq(crond_launch(syslog, "/opt/api", 1000, 1000)["launch_pid"], 190,
                 "the trusted CROND record must bind the real launch PID")
    th.assert_eq(crond_launch(pam, "/opt/api", 1000, 1000)["half"], "pam",
                 "the matching Audit USER_START record must provide the PAM half")
    for field in ("_CMDLINE", "MESSAGE", "_SYSTEMD_UNIT", "_GID"):
        changed = dict(syslog)
        changed[field] = "mutated"
        th.assert_eq(crond_launch(changed, "/opt/api", 1000, 1000), None,
                     f"a mutated {field} must not attest a CROND launch")
    changed_pam = dict(pam, MESSAGE=pam["MESSAGE"].replace(
        'acct="ec2-user"', 'acct="root"'))
    th.assert_eq(crond_launch(changed_pam, "/opt/api", 1000, 1000), None,
                 "one nested PAM field mutation must invalidate the launch")
    for grantors in ("", "pam_permit"):
        changed = dict(pam, MESSAGE=pam["MESSAGE"].replace(
            "grantors=pam_loginuid,pam_keyinit,pam_limits,pam_systemd ",
            (f"grantors={grantors} " if grantors else "")))
        th.assert_eq(crond_launch(changed, "/opt/api", 1000, 1000), None,
                     "missing or wrong nested grantors must invalidate PAM proof")
    th.assert_true(crond_launch(dict(
        pam, AUDIT_FIELD_GRANTORS=(
            "pam_loginuid,pam_keyinit,pam_limits,pam_systemd")),
        "/opt/api", 1000, 1000),
        "matching optional top-level grantors should be accepted")
    th.assert_eq(crond_launch(dict(pam, AUDIT_FIELD_GRANTORS="pam_permit"),
                              "/opt/api", 1000, 1000), None,
                 "top-level grantors must agree exactly when journald exposes them")
    th.assert_eq(crond_launch(dict(
        pam, AUDIT_FIELD_GRANTORS=CROND_PAM_GRANTORS,
        _AUDIT_FIELD_GRANTORS="pam_permit"), "/opt/api", 1000, 1000), None,
        "real and legacy top-level spellings must not contradict each other")


@th.unit_test("CROND origin requires both ordered halves and conflict is sticky")
def test_crond_origin_missing_order_and_conflict(opts):
    from mojo.mojosec.store import Store

    aggregation = {"window_seconds": 60, "flush_count": 10,
                   "max_aggregates": 100, "critical_reserve_aggregates": 10}
    delivery = {"max_spool_events": 100, "critical_reserve_events": 10,
                "retry_min_seconds": 1, "retry_max_seconds": 60}
    boot = "a" * 32
    nodes = [
        {"boot_id": boot, "audit_id": "1", "pid": 19, "ppid": 1,
         "audit_session": 9, "exe": "/usr/bin/bash", "argv": ["/usr/bin/bash"],
         "monotonic": 300},
        {"boot_id": boot, "audit_id": "2", "pid": 18, "ppid": 19,
         "audit_session": 9, "exe": "/usr/bin/python3",
         "argv": ["python3", "-m", "mojo.deploy.jobman", "start"], "monotonic": 400},
        {"boot_id": boot, "audit_id": "3", "pid": 21, "ppid": 18,
         "audit_session": 9, "exe": "/usr/bin/python3", "start_ticks": 210,
         "argv": ["/opt/api/bin/jobs.py", "engine", "foreground"],
         "monotonic": 500, "pinned": True},
    ]
    for node in nodes:
        node.update(success=True, eoe=True, argv_sha256=hashlib.sha256(
            b"\0".join(part.encode() for part in node["argv"])).hexdigest())
    pam = {"boot_id": boot, "audit_session": 9, "half": "pam",
           "monotonic": 100, "command_sha256": "7" * 64}
    syslog = {"boot_id": boot, "audit_session": 9, "half": "syslog",
              "launch_pid": 19, "monotonic": 200, "command_sha256": "7" * 64}
    cases = (([syslog], False),
             ([pam, dict(syslog, monotonic=50)], False),
             ([pam, syslog, dict(syslog, launch_pid=99)], True))
    for launches, conflict in cases:
        with tempfile.TemporaryDirectory() as root:
            store = Store(root, "sensor", aggregation, delivery,
                          local_only_diagnostic_path=os.path.join(root, "missing"))
            store.ingest([], process_nodes=nodes, crond_launches=launches)
            origin = store.db.execute(
                "SELECT ambiguous FROM origin_sessions WHERE boot_id=? AND audit_session=9",
                (boot,)).fetchone()
            th.assert_eq(origin, None,
                         "missing, misordered, or conflicting launch proof must not anchor")
            if conflict:
                row = store.db.execute(
                    "SELECT ambiguous FROM crond_launches WHERE boot_id=? AND audit_session=9",
                    (boot,)).fetchone()
                th.assert_true(row["ambiguous"],
                               "a duplicate launch mutation must remain sticky conflict")
            store.close()


@th.unit_test("every provenance graph edge uses one complete Audit eligibility contract")
def test_process_edge_eligibility_is_shared(opts):
    from mojo.mojosec.store import Store

    valid = {"success": True, "eoe": True, "ambiguous": False,
             "incomplete": False, "argv": ["/usr/bin/true"]}
    for edge in ("anchor", "bash", "jobman", "engine", "sudo", "broker", "target"):
        th.assert_true(Store._eligible_process_node(dict(valid)),
                       f"the complete {edge} Audit edge should be eligible")
        for field, value in (("success", False), ("eoe", False),
                             ("ambiguous", True), ("incomplete", True),
                             ("argv", [])):
            changed = dict(valid, **{field: value})
            th.assert_true(not Store._eligible_process_node(changed),
                           f"{edge} with invalid {field} must stay ordinary/no-anchor")


@th.unit_test("audit compounds reject conflicting duplicate fields")
def test_compound_conflict_is_ambiguous(opts):
    from mojo.mojosec.lineage import CompoundAssembler

    assembler = CompoundAssembler()
    result = assembler.ingest([
        _record(88, "SYSCALL", _AUDIT_FIELD_PID="42", _AUDIT_FIELD_PPID="10",
                _AUDIT_FIELD_EXE="/usr/bin/sudo"),
        _record(88, "SYSCALL", _AUDIT_FIELD_PID="43", _AUDIT_FIELD_PPID="10",
                _AUDIT_FIELD_EXE="/usr/bin/sudo"),
        _record(88, "EOE"),
    ])
    th.assert_true(result["complete"][0]["ambiguous"],
                   "conflicting rows in one Audit serial must fail ambiguous")


@th.unit_test("audit compound timeout emits an incomplete fail-open generation")
def test_compound_timeout_is_incomplete(opts):
    from mojo.mojosec import lineage

    assembler = lineage.CompoundAssembler()
    with mock.patch.object(lineage.time, "time", return_value=100.0):
        assembler.ingest([_record(44, "SYSCALL", _AUDIT_FIELD_PID="8",
                                  _AUDIT_FIELD_PPID="1")])
    with mock.patch.object(lineage.time, "time", return_value=102.1):
        result = assembler.ingest([])
    th.assert_true(result["complete"][0]["incomplete"] and
                   result["complete"][0]["ambiguous"],
                   "a compound without EOE must close after two seconds but never prove lineage")


@th.unit_test("event lineage projection is bounded")
def test_project_ancestors_is_bounded(opts):
    from mojo.mojosec.lineage import project_ancestors
    from mojo.mojosec.store import Store

    ancestors = [{"pid": i, "exe": f"/bin/p{i}"} for i in range(20)]
    projected = project_ancestors(ancestors)
    th.assert_eq(len(projected), 8, "central Event evidence may carry at most eight ancestors")
    reused = [
        {"pid": 7, "start_ticks": 10, "exe": "/bin/a", "argv": ["/bin/a"],
         "success": True, "eoe": True, "monotonic": 100},
        {"pid": 7, "start_ticks": 10, "exe": "/bin/a", "argv": ["/bin/a"],
         "success": True, "eoe": True, "monotonic": 101},
    ]
    th.assert_eq(Store._one_pid_generation(
        reused, 7, 10, 0, 1_000_000, exe="/bin/a"), None,
        "PID reuse or duplicate generations must never choose an arbitrary proof node")


@th.unit_test("proven firewall operation resolves local-only and incomplete proof fails open")
def test_pending_firewall_resolution(opts):
    from mojo.mojosec.events import observation
    from mojo.mojosec.lineage import CROND_SELINUX, crond_launch
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
            "cwd": "/" + "x" * 511,
            "receipt_semantics": ["s" * 160 for unused in range(8)],
            "lineage_sha256": "d" * 64,
            "lineage": [{"pid": index + 1, "ppid": index,
                         "start_ticks": index + 100,
                         "exe": "/" + "e" * 511,
                         "cgroup": "/" + "c" * 511,
                         "selinux": "z" * 256} for index in range(8)],
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
        "monotonic_ns": 1_000_000_000, "children": [],
    }
    child_digest = hashlib.sha256(b"/sbin/iptables").hexdigest()
    result = dict(begin, kind="result", target_pid=23, target_start_ticks=230,
                  monotonic_ns=1_100_000_000, ok=True, children=[{
                      "pid": 23, "start_ticks": 230, "exe": "/sbin/iptables",
                      "argv_digest": child_digest, "returncode": 0, "ok": True}])
    command = "/opt/api/bin/jobman start >> /opt/api/var/logs/jobman.log 2>&1"
    common = {"_BOOT_ID": boot, "_UID": "0",
              "_AUDIT_LOGINUID": "1000", "_AUDIT_SESSION": str(session),
              "_SELINUX_CONTEXT": CROND_SELINUX}
    launches = [
        crond_launch(dict(common, _TRANSPORT="audit", _AUDIT_TYPE_NAME="USER_START",
                    __MONOTONIC_TIMESTAMP="100",
                    MESSAGE=(f"USER_START pid=411 uid=0 auid=1000 ses={session} "
                             "msg='op=PAM:session_open "
                             "grantors=pam_loginuid,pam_keyinit,pam_limits,pam_systemd "
                             "acct=\"ec2-user\" "
                             "exe=\"/usr/sbin/crond\" hostname=? addr=? terminal=cron "
                             "res=success'")),
                    "/opt/api", 1000, 1000),
        crond_launch(dict(common, _TRANSPORT="syslog", _PID="19", _GID="1000",
                    _COMM="crond", _EXE="/usr/sbin/crond", SYSLOG_IDENTIFIER="CROND",
                    _CMDLINE="/usr/sbin/CROND -n",
                    MESSAGE=f"(ec2-user) CMD ({command})",
                    _SYSTEMD_UNIT=f"session-{session}.scope",
                    _SYSTEMD_CGROUP=(f"/user.slice/user-1000.slice/"
                                    f"session-{session}.scope"),
                    __MONOTONIC_TIMESTAMP="200"), "/opt/api", 1000, 1000),
    ]
    nodes = [
        {"boot_id": boot, "audit_id": "5", "pid": 19, "ppid": 1,
         "audit_session": session, "exe": "/usr/bin/bash", "argv": ["/usr/bin/bash"],
         "monotonic": 300},
        {"boot_id": boot, "audit_id": "6", "pid": 19, "ppid": 1,
         "audit_session": session, "exe": "/usr/bin/python3",
         "argv": ["python3", "-m", "mojo.deploy.jobman", "start"], "monotonic": 400},
        {"boot_id": boot, "audit_id": "1", "pid": 20, "ppid": 21,
         "audit_session": session, "exe": "/usr/bin/sudo", "argv": ["/usr/bin/sudo"]},
        {"boot_id": boot, "audit_id": "2", "pid": 21, "ppid": 19,
         "audit_session": session, "exe": "/usr/bin/python3",
         "argv": ["/opt/api/bin/jobs.py", "engine", "foreground"], "pinned": True,
         "start_ticks": 210, "monotonic": 500},
        {"boot_id": boot, "audit_id": "3", "pid": 22, "ppid": 20,
         "audit_session": session, "start_ticks": 220, "exe": "/usr/bin/python3", "argv": []},
        {"boot_id": boot, "audit_id": "4", "pid": 23, "ppid": 22,
         "audit_session": session, "start_ticks": 230, "exe": "/sbin/iptables", "argv": []},
    ]
    for node in nodes:
        node["success"] = True
        node["eoe"] = True
        if not node["argv"]:
            node["argv"] = [node["exe"]]
        node["argv_sha256"] = hashlib.sha256(
            b"\0".join(part.encode() for part in node["argv"])).hexdigest()
    with tempfile.TemporaryDirectory() as root:
        os.chmod(root, 0o700)
        store = Store(root, "sensor", aggregation, delivery,
                      local_only_diagnostic_path=os.path.join(root, "missing"))
        store.ingest([], audit_health=health, process_nodes=nodes,
                     crond_launches=launches)
        store.close()
        reopened = Store(root, "sensor", aggregation, delivery,
                         local_only_diagnostic_path=os.path.join(root, "missing"))
        with mock.patch("mojo.mojosec.lineage.enrich_process",
                        return_value={"start_ticks": 210}):
            reopened.ingest([candidate], audit_health=health)
            th.assert_eq(reopened.stats()["provenance"]["pending_firewall"], 1,
                         "healthy journal observation should wait for later proof")
            reopened.ingest([], cursor_key="nginx", cursor={"offset": 1})
            th.assert_eq(reopened.stats()["provenance"]["pending_firewall"], 1,
                         "nginx ingest has no Audit authority and must preserve pending")
            reopened.ingest([], audit_health=health,
                            firewall_receipts=[begin, result])
        th.assert_eq(reopened.stats()["local_only_suppressed"], 1,
                     "complete healthy process and receipt proof should suppress centrally")
        th.assert_eq(reopened.pending_batch(10, 65536), [],
                     "proven expected automation must not create an ordinary Event")
        reopened.close()

    unhealthy = dict(health, healthy=False, sequence=2, reason="lost")
    with tempfile.TemporaryDirectory() as root:
        store = Store(root, "sensor", aggregation, delivery,
                      local_only_diagnostic_path=os.path.join(root, "missing"))
        store.ingest([dict(candidate, fingerprint="8" * 64)], audit_health=health)
        store.ingest([], audit_health=unhealthy)
        th.assert_eq(store.stats()["provenance"]["pending_firewall"], 0,
                     "explicit unhealthy journal authority must fail open immediately")
        th.assert_eq(len(store.pending_batch(10, 65536)), 1,
                     "health failure must retain the ordinary sudo Event")
        store.close()

    bad_result = dict(result, children=[dict(result["children"][0],
                                            argv_digest="9" * 64)])
    bad_candidate = dict(candidate, fingerprint="9" * 64)
    with tempfile.TemporaryDirectory() as root:
        store = Store(root, "sensor", aggregation, delivery,
                      local_only_diagnostic_path=os.path.join(root, "missing"))
        store.ingest([bad_candidate], audit_health=health, process_nodes=nodes,
                     firewall_receipts=[begin, bad_result], crond_launches=launches)
        events = store.pending_batch(10, 65536)
        th.assert_eq(len(events), 1,
                     "one child argv mismatch must immediately fail open ordinary")
        th.assert_eq(events[0]["attributes"]["proof_status"], "conflict",
                     "conflicting proof must not erase the available rich ancestry")
        store.close()


@th.unit_test("pending firewall proof survives restart and expires untouched ordinary")
def test_pending_firewall_restart_expiry(opts):
    from mojo.mojosec.events import observation
    from mojo.mojosec.store import Store

    aggregation = {"window_seconds": 60, "flush_count": 10,
                   "max_aggregates": 100, "critical_reserve_aggregates": 10}
    delivery = {"max_spool_events": 100, "critical_reserve_events": 10,
                "retry_min_seconds": 1, "retry_max_seconds": 60}
    boot = "a" * 32
    candidate = observation(
        "auth.sudo_command", "high", "Privileged sudo command executed",
        attributes={
            "actor": "ec2-user", "target_user": "root", "boot_id": boot,
            "audit_session": 91, "attribution_provenance": "none",
            "producer_pid": 220, "monotonic": 1_000_000,
            "command": "/usr/local/sbin/mojo-firewall-broker",
            "command_path": "/usr/local/sbin/mojo-firewall-broker",
            "command_sha256": "b" * 64,
            "cwd": "/" + "x" * 511,
            "receipt_semantics": ["s" * 160 for unused in range(8)],
            "lineage_sha256": "d" * 64,
            "lineage": [{"pid": index + 1, "ppid": index,
                         "start_ticks": index + 100,
                         "exe": "/" + "e" * 511,
                         "cgroup": "/" + "c" * 511,
                         "selinux": "z" * 256} for index in range(8)],
        }, fingerprint_values=("restart-expiry",), aggregate=False,
        recommendation="review", observed_at="2026-08-13T20:00:00Z")
    health = {
        "schema": "mojosec.audit-health", "version": 1, "boot_id": boot,
        "generation": "c" * 64, "rules_sha256": "d" * 64,
        "sequence": 1, "enabled": 1, "failure": 1, "rate_limit": 0,
        "backlog_limit": 8192, "backlog": 0, "lost": 0,
        "updated_at": time.time(), "healthy": True, "reason": "",
    }
    with tempfile.TemporaryDirectory() as root:
        os.chmod(root, 0o700)
        store = Store(root, "sensor", aggregation, delivery,
                      local_only_diagnostic_path=os.path.join(root, "missing"))
        store.ingest([candidate], audit_health=health)
        th.assert_eq(store.stats()["provenance"]["pending_firewall"], 1,
                     "candidate must remain durable while proof can still arrive")
        store.close()

        reopened = Store(root, "sensor", aggregation, delivery,
                         local_only_diagnostic_path=os.path.join(root, "missing"))
        reopened.reconcile_pending_firewall(now=time.time() + 31)
        rows = reopened.db.execute(
            "SELECT payload,delivery_class FROM events ORDER BY created").fetchall()
        th.assert_eq(len(rows), 1,
                     "expired proof must enqueue the original ordinary event before delete")
        event = json.loads(rows[0]["payload"])
        th.assert_eq(rows[0]["delivery_class"], "ordinary",
                     "downgrade/restart reconciliation must never retain a local disposition")
        th.assert_eq(event["attributes"]["command"],
                     "/usr/local/sbin/mojo-firewall-broker",
                     "restart expiry must preserve the untouched admin command evidence")
        th.assert_true(len(json.dumps(event["attributes"], sort_keys=True,
                                      separators=(",", ":")).encode()) <= 8192,
                       "post-enrichment fallback must reapply the wire evidence budget")
        th.assert_eq(event["attributes"].get("lineage_sha256"), "d" * 64,
                     "priority projection must preserve the lineage digest")
        th.assert_eq(reopened.stats()["provenance"]["pending_firewall"], 0,
                     "the durable candidate may be deleted only after ordinary enqueue")
        reopened.close()


@th.unit_test("pending firewall capacity pressure fails open before eviction")
def test_pending_firewall_capacity_fail_open(opts):
    from mojo.mojosec import store as store_module
    from mojo.mojosec.events import observation

    aggregation = {"window_seconds": 60, "flush_count": 10,
                   "max_aggregates": 100, "critical_reserve_aggregates": 10}
    delivery = {"max_spool_events": 100, "critical_reserve_events": 10,
                "retry_min_seconds": 1, "retry_max_seconds": 60}
    candidate = observation(
        "auth.sudo_command", "high", "Privileged sudo command executed",
        attributes={"actor": "ec2-user", "target_user": "root",
                    "command": "/usr/local/sbin/mojo-firewall-broker",
                    "command_path": "/usr/local/sbin/mojo-firewall-broker"},
        fingerprint_values=("capacity",), aggregate=False,
        observed_at="2026-08-13T20:00:00Z")
    with tempfile.TemporaryDirectory() as root:
        store = store_module.Store(root, "sensor", aggregation, delivery,
                                   local_only_diagnostic_path=os.path.join(root, "missing"))
        store.db.execute("BEGIN IMMEDIATE")
        with mock.patch.object(store_module, "PENDING_OPERATION_CAP", 0):
            store.hold_firewall_observation(candidate, now=time.time())
        store.db.execute("COMMIT")
        th.assert_eq(store.db.execute(
            "SELECT COUNT(*) FROM pending_firewall").fetchone()[0], 0,
            "cap pressure may delete a candidate only after fail-open enqueue")
        th.assert_eq(store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1,
                     "cap pressure must retain an ordinary Event")
        th.assert_eq(store.stats()["provenance"]["pending_cap_flush"], 1,
                     "cap fail-open must be visible in pressure telemetry")
        store.close()


@th.unit_test("ordinary legacy sudo keeps rich ancestry origin and central evidence")
def test_ordinary_sudo_rich_fallback(opts):
    from mojo.apps.incident.services.mojosec_evidence import project
    from mojo.mojosec.events import observation
    from mojo.mojosec.store import Store

    aggregation = {"window_seconds": 60, "flush_count": 10,
                   "max_aggregates": 100, "critical_reserve_aggregates": 10}
    delivery = {"max_spool_events": 100, "critical_reserve_events": 10,
                "retry_min_seconds": 1, "retry_max_seconds": 60}
    boot = "a" * 32
    sudo = observation(
        "auth.sudo_command", "high", "Privileged sudo command executed",
        attributes={
            "actor": "deploy", "target_user": "root", "boot_id": boot,
            "audit_session": 71, "attribution_provenance": "audit_session",
            "source_ip": "192.0.2.71", "tty": "pts/1", "producer_pid": 42,
            "command": "/sbin/iptables -L", "command_path": "/sbin/iptables",
            "command_sha256": "f" * 64,
        }, fingerprint_values=("legacy-direct",), aggregate=False,
        recommendation="review", observed_at="2026-08-13T20:00:00Z")
    nodes = [
        {"boot_id": boot, "audit_id": "42", "pid": 42, "ppid": 40,
         "audit_session": 71, "exe": "/usr/bin/sudo", "argv": ["sudo", "iptables"],
         "success": True, "eoe": True, "ambiguous": False, "incomplete": False},
        {"boot_id": boot, "audit_id": "40", "pid": 40, "ppid": 1,
         "audit_session": 71, "exe": "/usr/bin/bash", "argv": ["bash"],
         "success": True, "eoe": True, "ambiguous": False, "incomplete": False},
    ]
    with tempfile.TemporaryDirectory() as root:
        store = Store(root, "sensor", aggregation, delivery,
                      local_only_diagnostic_path=os.path.join(root, "missing"))
        store.ingest([sudo], process_nodes=nodes, ssh_sessions=[{
            "boot_id": boot, "audit_session": 71, "actor": "deploy", "tty": "pts/1",
            "source_ip": "192.0.2.71", "observed_at": time.time(), "ambiguous": False,
        }])
        event = store.pending_batch(10, 65536)[0]
        attributes = event["attributes"]
        th.assert_eq((attributes["proof_status"], attributes["origin_kind"]),
                     ("partial", "ssh"),
                     "missing broker proof must remain ordinary but explain its SSH origin")
        th.assert_eq(len(attributes["lineage"]), 2,
                     "generic direct sudo must retain available bounded ancestry")
        projected = project("auth.sudo_command", attributes)
        evidence = projected["evidence"]
        th.assert_eq((projected["source_ip"], evidence["tty"], evidence["origin_kind"]),
                     ("192.0.2.71", "pts/1", "ssh"),
                     "central Event evidence must retain IP, TTY, and safe origin")
        th.assert_eq(evidence["proof_status"], "partial",
                     "failed or missing suppression proof must be visible to admins")
        th.assert_true(1 <= len(evidence["ancestors"]) <= 8 and
                       len(evidence["lineage_sha256"]) == 64,
                       "central evidence must carry bounded ancestry and its full digest")
        store.close()
