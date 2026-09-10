import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import types
from unittest import mock

from testit import helpers as th


@th.django_unit_test()
def test_expected_fim_changes_are_annotated_never_suppressed(opts):
    import datetime
    import hashlib
    import tempfile

    from mojo.mojosec.expected_changes import annotation, load_manifest

    with tempfile.TemporaryDirectory() as root:
        path = os.path.join(root, "settings.py")
        digest = hashlib.sha256(b"new bytes").hexdigest()
        manifest_path = os.path.join(root, "expected.json")
        manifest = {
            "schema": "mojosec.expected_changes", "version": 1,
            "entries": [{
                "path": path, "change": "modified", "sha256": digest,
                "expires_at": "2099-01-01T00:00:00Z",
                "deployment_id": "deploy-123",
            }],
        }
        with open(manifest_path, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle)
        os.chmod(manifest_path, 0o600)

        entries = load_manifest(manifest_path, require_root=False)
        value = annotation(entries, path, "modified", {}, {"sha256": digest})
        th.assert_eq(value["deployment_id"], "deploy-123",
                     "an exact path+change+digest+live-expiry match must annotate")
        th.assert_eq(annotation(entries, path, "modified", {}, {"sha256": "0" * 64}), None,
                     "a digest mismatch must not be called an expected deploy change")
        expired = annotation(
            entries, path, "modified", {}, {"sha256": digest},
            now=datetime.datetime(2100, 1, 1, tzinfo=datetime.timezone.utc))
        th.assert_eq(expired, None, "expired deployment expectations must not annotate")


def _journal_config(max_records=10, max_bytes=65536):
    return {
        "max_records": max_records, "max_bytes_per_poll": max_bytes,
        "max_record_bytes": 16384, "timeout_seconds": 5, "lookback_seconds": 300,
        "project_path": "/opt/api", "app_uid": 1000, "app_gid": 1000,
    }


def _journal_record(cursor, source_ip):
    return {
        "__CURSOR": cursor, "SYSLOG_IDENTIFIER": "sshd",
        "_UID": "0", "_COMM": "sshd", "_EXE": "/usr/sbin/sshd",
        "MESSAGE": f"Accepted publickey for deploy from {source_ip} port 50221 ssh2",
    }


def _popen_stream(payload, commands):
    real_popen = subprocess.Popen

    def spawn(command, **kwargs):
        commands.append(command)
        script = "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"
        with tempfile.TemporaryFile() as stream:
            stream.write(payload)
            stream.seek(0)
            return real_popen(
                [sys.executable, "-c", script], stdout=kwargs["stdout"],
                stderr=kwargs["stderr"], stdin=stream, bufsize=kwargs["bufsize"],
            )

    return spawn


def _patch_journal_popen(journal_module, payload, commands):
    # Patch the collector's module reference, not subprocess.Popen on the
    # shared stdlib module: testit may run selected modules concurrently.
    proxy = types.SimpleNamespace(
        Popen=mock.Mock(side_effect=_popen_stream(payload, commands)),
        PIPE=subprocess.PIPE, DEVNULL=subprocess.DEVNULL,
        TimeoutExpired=subprocess.TimeoutExpired,
    )
    return mock.patch.object(journal_module, "subprocess", proxy)


def _fixture_proc(root, live):
    boot_path = os.path.join(root, "sys", "kernel", "random")
    os.makedirs(boot_path, exist_ok=True)
    with open(os.path.join(boot_path, "boot_id"), "w") as handle:
        handle.write("a" * 32)
    for node in live:
        directory = os.path.join(root, str(node["pid"]))
        os.makedirs(directory)
        fields = [str(node["pid"]), "(python)", "S", str(node["ppid"])] + ["0"] * 17
        fields.append(str(node["start_ticks"]))
        with open(os.path.join(directory, "stat"), "w") as handle:
            handle.write(" ".join(fields))
        with open(os.path.join(directory, "cmdline"), "wb") as handle:
            handle.write(b"\0".join(part.encode() for part in node["cmdline"]) + b"\0")
        with open(os.path.join(directory, "cgroup"), "w") as handle:
            handle.write("0::/user.slice/user-1000.slice/session-9.scope")
        os.symlink(node["exe"], os.path.join(directory, "exe"))


@th.django_unit_test()
def test_al2023_collector_store_provenance_golden_path(opts):
    _assert_al2023_provenance()


@th.django_unit_test()
def test_late_fragment_vetoes_same_poll_final_receipt(opts):
    _assert_al2023_provenance(("late_fragment_receipt", "unrelated_fragment_receipt",
                              "other_boot_fragment_receipt"))


@th.django_unit_test()
def test_evicted_fragment_vetoes_same_poll_final_receipt(opts):
    _assert_al2023_provenance(("capacity_fragment_receipt", "capacity_unrelated_fragment_receipt",
                              "capacity_other_boot_fragment_receipt"))


def _assert_al2023_provenance(scenarios=(
        "complete", "missing_execve", "missing_boundary", "argv_conflict",
        "missing_receipt", "failed_receipt", "conflicting_receipt",
        "unexpected_command", "unexpected_child", "interactive", "ssh", "audit_loss", "parser_loss",
        "parser_loss_no_sidecar", "dead_engine", "sql_unpinned")):
    from mojo.deploy import audit
    from mojo.mojosec import lineage
    from mojo.mojosec.collectors import journal as journal_module
    from mojo.mojosec.runtime import Runtime
    from mojo.mojosec.store import Store

    with open(os.path.join(os.path.dirname(__file__), "golden",
                           "al2023_firewall_provenance_v1.json")) as handle:
        original = json.load(handle)
    aggregation = {"window_seconds": 60, "flush_count": 10,
                   "max_aggregates": 100, "critical_reserve_aggregates": 10}
    delivery = {"max_spool_events": 100, "critical_reserve_events": 10,
                "retry_min_seconds": 1, "retry_max_seconds": 60}
    real_enrich = lineage.enrich_process
    for scenario in scenarios:
        fixture = json.loads(json.dumps(original))
        if scenario == "missing_execve":
            fixture["engine"].pop(1)
        if scenario == "missing_boundary":
            fixture["engine"].pop()
        if scenario == "argv_conflict":
            fixture["engine"][1]["_AUDIT_FIELD_A0"] = '"different"'
        if scenario in ("failed_receipt", "conflicting_receipt"):
            receipt = json.loads(fixture["receipts"][1]["MESSAGE"])
            receipt["ok"] = False
            changed = dict(fixture["receipts"][1], MESSAGE=json.dumps(receipt))
            if scenario == "failed_receipt":
                fixture["receipts"][1] = changed
            else:
                fixture["receipts"].insert(1, changed)
        if scenario == "unexpected_command":
            fixture["observation"]["MESSAGE"] += " --unexpected"
        if scenario == "interactive":
            fixture["observation"]["MESSAGE"] = fixture["observation"]["MESSAGE"].replace(
                "PWD=", "TTY=pts/1 ; PWD=")
        if scenario == "unexpected_child":
            fixture["target"][1].update(_AUDIT_FIELD_A0='"/sbin/ipset"',
                                        MESSAGE='EXECVE argc=1 a0="/sbin/ipset"')
        if scenario == "ssh":
            fixture["launches"].append({
                "__CURSOR": "ssh-login", "_BOOT_ID": "a" * 32,
                "_AUDIT_SESSION": "9", "_AUDIT_LOGINUID": "1000",
                "_TRANSPORT": "audit", "_AUDIT_TYPE_NAME": "USER_LOGIN", "_UID": "0",
                "MESSAGE": 'USER_LOGIN acct="ec2-user" exe="/usr/sbin/sshd" '
                'addr=192.0.2.8 terminal=ssh res=success'})
        health = {"schema": "mojosec.audit-health", "version": 1,
                  "boot_id": "a" * 32, "generation": "c" * 64,
                  "rules_sha256": "d" * 64, "sequence": 1, "enabled": 1,
                  "failure": 1, "rate_limit": 0, "backlog_limit": 8192,
                  "backlog": 0, "lost": 0, "updated_at": time.time()}
        with tempfile.TemporaryDirectory() as root:
            proc_root = os.path.join(root, "proc")
            _fixture_proc(proc_root, fixture["live"])
            state = os.path.join(root, "state")
            store = Store(state, "sensor", aggregation, delivery)
            collector = journal_module.JournalCollector(
                _journal_config(20000, 8 * 1024 * 1024) if scenario.startswith("capacity_")
                else _journal_config(100))
            errors = []
            runtime = Runtime.__new__(Runtime)
            runtime.store = store
            runtime.collector_status = {}
            runtime._collector_error = lambda name, err: errors.append(str(err))
            batches = [
                fixture["launches"] + fixture["bash"] + fixture["jobman"] + fixture["engine"][:2],
                fixture["engine"][2:] + fixture["sudo"] + fixture["broker"] + fixture["target"] +
                [fixture["observation"], fixture["receipts"][0]],
                [] if scenario == "missing_receipt" else fixture["receipts"][1:],
            ]
            def parents(pid):
                return lineage.walk_parents(pid, proc_root=proc_root)

            unavailable = False
            with mock.patch.object(lineage, "enrich_process", side_effect=lambda pid, **kw:
                                   real_enrich(pid, proc_root=proc_root)), \
                    mock.patch.object(journal_module, "walk_parents", side_effect=parents), \
                    mock.patch.object(audit, "read_health", side_effect=lambda:
                                      None if unavailable else dict(health)):
                for index, batch in enumerate(batches):
                    if index == 2 and scenario.endswith("fragment_receipt"):
                        # Evict completed assembler state while retaining the
                        # eligible/pinned canonical engine and pending command.
                        store.db.execute("DELETE FROM audit_fragments")
                        th.assert_eq(store.stats()["provenance"]["engine_anchors"], 1,
                                     "the race must begin with an eligible live engine anchor")
                        th.assert_eq(store.stats()["provenance"]["pending_firewall"], 1,
                                     "the final receipt must race an already-held broker observation")
                        late = dict(fixture["engine"][1], _AUDIT_FIELD_A0='"contradiction"')
                        if scenario.endswith("unrelated_fragment_receipt"):
                            late["_AUDIT_ID"] = "99999999"
                        elif scenario.endswith("other_boot_fragment_receipt"):
                            late["_BOOT_ID"] = "b" * 32
                        if scenario.startswith("capacity_"):
                            batch = batch + [{
                                "__CURSOR": f"filler-{serial}", "_TRANSPORT": "audit",
                                "_BOOT_ID": "a" * 32, "_AUDIT_ID": str(20000 + serial),
                                "_AUDIT_TYPE_NAME": "CWD", "MESSAGE": "CWD cwd=\"/\"",
                            } for serial in range(lineage.COMPOUND_CAP)]
                        batch = batch + [late]
                    if index == 2 and scenario == "parser_loss_no_sidecar":
                        unavailable = True
                    if index == 2 and scenario == "audit_loss":
                        health["lost"] = 1
                    if index == 2 and scenario == "dead_engine":
                        os.unlink(os.path.join(proc_root, "21", "stat"))
                    if index == 2 and scenario == "sql_unpinned":
                        # Resolver must honor current SQL, even a legacy stale JSON pin.
                        store.db.execute("UPDATE process_nodes SET pinned=0 WHERE pid=21")
                    payload = b"".join(json.dumps(row).encode() + b"\n" for row in batch)
                    if index == 2 and scenario in ("parser_loss", "parser_loss_no_sidecar"):
                        payload += b'{"__CURSOR":"malformed","MESSAGE":broken}\n'
                    with _patch_journal_popen(journal_module, payload, []):
                        runtime._poll_stream(collector)
                    th.assert_eq(errors, [], f"the {scenario} collector must execute successfully")
                    if index == 2 and scenario.startswith("capacity_"):
                        retained = store.load_audit_fragments()
                        th.assert_true(len(retained) <= lineage.COMPOUND_CAP,
                                       "capacity pressure must not enlarge persisted fragment bounds")
                        th.assert_true((late["_BOOT_ID"], late["_AUDIT_ID"]) not in retained,
                                       "the regression must place the late fragment beyond retention cutoff")
                    if index == 2 and scenario in ("late_fragment_receipt", "capacity_fragment_receipt"):
                        th.assert_eq(store.stats()["local_only_suppressed"], 0,
                                     "pre-timeout contradictory fragment must veto same-poll receipt suppression")
                        th.assert_eq(store.stats()["provenance"]["engine_anchors"], 0,
                                     "matching fragment uncertainty must immediately revoke the engine pin")
                    if index == 1 and scenario == "complete":
                        th.assert_eq(store.stats()["provenance"]["pending_firewall"], 1,
                                     "begin without result must durably hold the exact broker observation")
                        th.assert_eq(store.stats()["provenance"]["engine_anchors"], 1,
                                     "real /proc plus AL2023 Audit must create a live engine anchor")
                    store.close()
                    store = Store(state, "sensor", aggregation, delivery)
                    runtime.store = store
                store.db.execute("UPDATE pending_firewall SET expires=?", (time.time() - 1,))
                store.reconcile_pending_firewall()
                events = store.pending_batch(100, 65536)
                sudo_events = [event for event in events if event["kind"] == "auth.sudo_command"]
                if (scenario == "complete" or scenario.endswith("unrelated_fragment_receipt") or
                        scenario.endswith("other_boot_fragment_receipt")):
                    th.assert_eq(sudo_events, [], "fully proven broker execution must stay local-only")
                    th.assert_eq(store.stats()["local_only_suppressed"], 1,
                                 "the production collector path must reach the fixed classifier")
                    th.assert_eq(store.db.execute("SELECT COUNT(*) FROM process_nodes").fetchone()[0], 6,
                                 "poll/restart must preserve exactly the six canonical exec generations")
                else:
                    th.assert_eq(len(sudo_events), 1, f"{scenario} must retain ordinary sudo evidence")
                    th.assert_eq(store.stats()["local_only_suppressed"], 0,
                                 f"{scenario} must never suppress unproved privileged activity")
                store.close()


@th.django_unit_test()
def test_journal_parser_loss_vetoes_new_nodes(opts):
    from mojo.mojosec.collectors import journal as journal_module
    from mojo.mojosec.lineage import eligible_process_node

    with open(os.path.join(os.path.dirname(__file__), "golden",
                           "al2023_firewall_provenance_v1.json")) as handle:
        fixture = json.load(handle)
    payload = b"".join(json.dumps(row).encode() + b"\n" for row in fixture["engine"])
    payload += b'{"__CURSOR":"bad","MESSAGE":broken}\n'
    with _patch_journal_popen(journal_module, payload, []), \
            mock.patch.object(journal_module, "walk_parents", return_value={
                "nodes": [], "ambiguous": False}):
        result = journal_module.JournalCollector(_journal_config()).poll()
    th.assert_eq(result["malformed"], 1, "parser loss must remain explicitly counted")
    th.assert_true(result["process_nodes"] and all(
        not eligible_process_node(node) for node in result["process_nodes"]),
        "no node from a lossy journal batch may become suppression-eligible")


@th.django_unit_test()
def test_journal_audit_proc_enrichment_is_optional_but_conflicts_fail_closed(opts):
    from mojo.mojosec.collectors import journal as journal_module

    records = [
        {"__CURSOR": "c1", "_BOOT_ID": "a" * 32, "_AUDIT_ID": "300",
         "_TRANSPORT": "audit", "_AUDIT_TYPE_NAME": "SYSCALL",
         "_AUDIT_FIELD_PID": "4242", "_AUDIT_FIELD_PPID": "100",
         "_AUDIT_FIELD_UID": "1000", "_AUDIT_FIELD_EUID": "0",
         "_AUDIT_LOGINUID": "1000", "_AUDIT_SESSION": "71",
         "_AUDIT_FIELD_TTY": "pts1", "_AUDIT_FIELD_EXE": "/usr/bin/sudo",
         "_AUDIT_FIELD_SUCCESS": "yes", "_AUDIT_FIELD_EXIT": "0",
         "MESSAGE": "SYSCALL arch=c000003e syscall=59 success=yes exit=0"},
        {"__CURSOR": "c2", "_BOOT_ID": "a" * 32, "_AUDIT_ID": "300",
         "_TRANSPORT": "audit", "_AUDIT_TYPE_NAME": "EXECVE",
         "_AUDIT_FIELD_ARGC": "2", "_AUDIT_FIELD_A0": "/usr/bin/sudo",
         "_AUDIT_FIELD_A1": "-s", "MESSAGE": "EXECVE argc=2"},
        {"__CURSOR": "c3", "_BOOT_ID": "a" * 32, "_AUDIT_ID": "300",
         "_TRANSPORT": "audit", "_AUDIT_TYPE_NAME": "EOE", "MESSAGE": "EOE"},
    ]
    payload = b"".join(json.dumps(row).encode() + b"\n" for row in records)
    commands = []
    collector = journal_module.JournalCollector(_journal_config())
    with _patch_journal_popen(journal_module, payload, commands), \
            mock.patch.object(journal_module, "walk_parents", return_value={
                "nodes": [], "ambiguous": False,
            }):
        result = collector.poll()
    node = result["process_nodes"][0]
    th.assert_true(node["success"] and node["eoe"] and not node["ambiguous"],
                   "an exited short-lived process must retain its complete Audit edge")
    th.assert_true("start_ticks" not in node and not node.get("pinned"),
                   "missing /proc enrichment cannot invent a live PID generation")

    live = {"pid": 4242, "ppid": 100, "start_ticks": 99,
            "exe": "/usr/bin/other", "cmdline": [], "unit": "", "cgroup": "",
            "namespaces": {}, "selinux": ""}
    with _patch_journal_popen(journal_module, payload, commands), \
            mock.patch.object(journal_module, "walk_parents", return_value={
                "nodes": [live], "ambiguous": False,
            }):
        conflict = collector.poll()["process_nodes"][0]
    th.assert_true(conflict["ambiguous"],
                   "a live executable that conflicts with Audit must poison proof")


@th.django_unit_test()
def test_journal_detector_keeps_logins_and_aggregates_failures(opts):
    from mojo.apps.incident.services.mojosec_evidence import project
    from mojo.mojosec.detectors import detect_journal

    accepted = detect_journal({
        "SYSLOG_IDENTIFIER": "sshd",
        "_UID": "0", "_COMM": "sshd", "_EXE": "/usr/sbin/sshd",
        "MESSAGE": "Accepted publickey for deploy from 192.0.2.8 port 50221 ssh2",
        "__REALTIME_TIMESTAMP": "1786190400000000",
    })
    th.assert_eq(accepted["kind"], "auth.ssh_login",
                 "every accepted SSH login must become a host event")
    th.assert_eq(accepted["aggregate"], False,
                 "accepted SSH logins must never wait in a noise aggregation window")
    th.assert_eq(accepted["attributes"]["source_ip"], "192.0.2.8",
                 "the accepted-login source must be normalized for central policy")

    failed = detect_journal({
        "SYSLOG_IDENTIFIER": "sshd",
        "_UID": "0", "_COMM": "sshd", "_EXE": "/usr/sbin/sshd",
        "_SYSTEMD_UNIT": "sshd.service",
        "MESSAGE": "Failed password for invalid user admin from 198.51.100.9 port 43812 ssh2",
    })
    th.assert_eq(failed["kind"], "auth.ssh_failure",
                 "failed SSH authentication should be retained as a security signal")
    th.assert_eq(failed["aggregate"], True,
                 "repeated SSH failures should be aggregated before delivery")

    facility_only = detect_journal({
        "SYSLOG_FACILITY": "10", "_SYSTEMD_UNIT": "sshd.service",
        "_UID": "0", "_COMM": "sshd", "_EXE": "/usr/sbin/sshd",
        "MESSAGE": "Accepted publickey for deploy from 203.0.113.8 port 50221 ssh2",
    })
    th.assert_eq(facility_only["kind"], "auth.ssh_login",
                 "AL2023 facility-10 auth records must work without a stable systemd unit")

    sudo = detect_journal({
        "SYSLOG_IDENTIFIER": "sudo", "SYSLOG_FACILITY": "10",
        "_SYSTEMD_UNIT": "session-202.scope",
        "_UID": "0", "_COMM": "sudo", "_EXE": "/usr/bin/sudo",
        "MESSAGE": "deploy : TTY=pts/0 ; PWD=/opt/api ; USER=root ; COMMAND=/usr/bin/systemctl restart api",
    })
    th.assert_eq(sudo["kind"], "auth.sudo_command",
                 "sudo commands attached to transient AL2023 scopes must be retained")
    th.assert_eq(sudo["attributes"]["command"],
                 "/usr/bin/systemctl restart api",
                 "the protected sensor evidence must retain bounded command context")

    secret = "top-secret-password"
    sensitive_sudo = detect_journal({
        "SYSLOG_IDENTIFIER": "sudo", "SYSLOG_FACILITY": "10",
        "_UID": "0", "_COMM": "sudo", "_EXE": "/usr/bin/sudo",
        "MESSAGE": f"deploy : USER=root ; COMMAND=/usr/bin/curl --password {secret} https://example.invalid",
    })
    encoded = json.dumps(sensitive_sudo)
    th.assert_true(secret in encoded and "--password" in encoded,
                   "sensor evidence must retain exact bounded text for receipt and admin Event projection")
    th.assert_eq(sensitive_sudo["attributes"]["command_path"], "/usr/bin/curl",
                 "sudo evidence should retain only the invoked executable path")

    oversized_command = "/usr/bin/curl " + "x" * 3000
    oversized_sudo = detect_journal({
        "SYSLOG_IDENTIFIER": "sudo", "SYSLOG_FACILITY": "10",
        "_UID": "0", "_COMM": "sudo", "_EXE": "/usr/bin/sudo",
        "MESSAGE": f"deploy : USER=root ; COMMAND={oversized_command}",
    })
    th.assert_eq(oversized_sudo["attributes"]["command"], oversized_command[:2048],
                 "trusted sudo parsing must retain the sensor's exact 2,048-byte prefix")
    th.assert_true(oversized_sudo["attributes"]["command_truncated"] is True,
                   "an oversized raw sudo command must carry a truthful truncation marker")
    th.assert_eq(oversized_sudo["attributes"]["command_sha256"],
                 hashlib.sha256(oversized_command.encode()).hexdigest(),
                 "the sensor must digest the full raw command rather than a message prefix")
    th.assert_eq(oversized_sudo["attributes"]["command_path"], "/usr/bin/curl",
                 "executable parsing must use the complete trusted sudo message")

    long_path = "/opt/" + "é" * 300
    long_path_sudo = detect_journal({
        "SYSLOG_IDENTIFIER": "sudo", "SYSLOG_FACILITY": "10",
        "_UID": "0", "_COMM": "sudo", "_EXE": "/usr/bin/sudo",
        "MESSAGE": f"deploy : USER=root ; COMMAND={long_path}",
    })
    expected_path = long_path.encode("utf-8")[:512].decode("utf-8", errors="ignore")
    th.assert_eq(long_path_sudo["attributes"]["command_path"], expected_path,
                 "build_evidence must retain the exact UTF-8-safe 512-byte executable prefix")
    th.assert_true(long_path_sudo["attributes"]["command_path_truncated"] is True,
                   "an executable path over 512 UTF-8 bytes must be marked truncated")
    th.assert_eq(long_path_sudo["attributes"]["command_path_sha256"],
                 hashlib.sha256(long_path.encode()).hexdigest(),
                 "the executable digest must cover the complete parsed path")
    projected = project("auth.sudo_command", long_path_sudo["attributes"])["evidence"]
    th.assert_true("command_family" not in projected,
                   "a sensor-truncated executable path must not drive Event command family")

    for forged in (
            {"SYSLOG_IDENTIFIER": "sshd", "_UID": "1000", "_COMM": "python3",
             "MESSAGE": "Accepted publickey for deploy from 192.0.2.99 port 1 ssh2"},
            {"SYSLOG_IDENTIFIER": "sudo", "_UID": "1000", "_COMM": "python3",
             "MESSAGE": "deploy : TTY=pts/0 ; USER=root ; COMMAND=/usr/bin/id"}):
        th.assert_eq(detect_journal(forged), None,
                     "user-controlled journal identifiers/messages must not forge auth events")


@th.django_unit_test()
def test_live_al2023_auth_tuples_are_exactly_trusted(opts):
    from mojo.mojosec.detectors import detect_journal

    split = {
        "SYSLOG_IDENTIFIER": "attacker-controlled-and-irrelevant",
        "_UID": "0", "_COMM": "sshd-session",
        "_EXE": "/usr/libexec/openssh/sshd-session",
        "_SYSTEMD_UNIT": "sshd.service",
        "_BOOT_ID": "a" * 32, "_AUDIT_SESSION": "91", "_TTY": "pts/2",
        "MESSAGE": "Accepted publickey for deploy from 192.0.2.91 port 50221 ssh2",
    }
    login = detect_journal(split)
    th.assert_eq(login["kind"], "auth.ssh_login",
                 "the deployed split-OpenSSH tuple must produce a login event")
    th.assert_eq(login["attributes"]["source_ip"], "192.0.2.91",
                 "the accepted login must retain its validated remote address")

    for field, value in (
            ("_UID", "1000"), ("_COMM", "sshd"),
            ("_EXE", "/usr/sbin/sshd"), ("_SYSTEMD_UNIT", "session-91.scope")):
        mutated = dict(split, **{field: value})
        th.assert_eq(detect_journal(mutated), None,
                     f"mutating authoritative split-SSH field {field} must fail closed")

    sudo = {
        "SYSLOG_IDENTIFIER": "not-authoritative", "_UID": "1000",
        "_COMM": "sudo", "_EXE": "/usr/bin/sudo",
        "_SYSTEMD_UNIT": "session-202.scope", "_BOOT_ID": "a" * 32,
        "_AUDIT_SESSION": "91",
        "MESSAGE": "deploy : USER=root ; COMMAND=/usr/bin/id",
    }
    command = detect_journal(sudo)
    th.assert_eq(command["kind"], "auth.sudo_command",
                 "the deployed non-root invoking UID sudo tuple must be trusted")
    th.assert_true("tty" not in command["attributes"] and
                   "cwd" not in command["attributes"],
                   "missing sudo TTY and PWD must remain absent")
    for field, value in (
            ("_UID", "01000"), ("_COMM", "sudoedit"),
            ("_EXE", ""), ("_SYSTEMD_UNIT", "user@1000.service")):
        mutated = dict(sudo, **{field: value})
        th.assert_eq(detect_journal(mutated), None,
                     f"mutating authoritative sudo field {field} must fail closed")


@th.django_unit_test()
def test_systemd_user_session_open_is_local_rich_and_exact(opts):
    from mojo.mojosec.attribution import AttributionResolver
    from mojo.mojosec.detectors import detect_journal

    record = {
        "_UID": "0", "_PID": "4123", "_COMM": "(systemd)",
        "_EXE": "/usr/lib/systemd/systemd", "_SYSTEMD_UNIT": "user@80.service",
        "_BOOT_ID": "b" * 32, "_AUDIT_SESSION": "44", "_AUDIT_LOGINUID": "80",
        "MESSAGE": "pam_unix(systemd-user:session): session opened for user www(uid=80) by (uid=0)",
    }
    session = detect_journal(record, AttributionResolver([], [{
        "actor": "www", "tty": "pts/9", "source_ip": "198.51.100.80",
        "observed_at": 1786190400,
    }]))
    th.assert_eq((session["kind"], session["severity"], session["summary"]),
                 ("auth.session_open", "info", "PAM service session opened"),
                 "a trusted systemd-user open must be informational local PAM evidence")
    th.assert_eq(session["attributes"]["target_uid"], 80,
                 "the target UID must survive as structured evidence")
    th.assert_eq(session["attributes"]["producer_exe"], "/usr/lib/systemd/systemd",
                 "the trusted producer executable must survive as evidence")
    th.assert_true("source_ip" not in session["attributes"],
                   "generic PAM opens must never use the looser who fallback")

    for field, value in (
            ("_UID", "80"), ("_PID", "0"), ("_COMM", "systemd"),
            ("_EXE", "/tmp/systemd"), ("_SYSTEMD_UNIT", "user@81.service"),
            ("_AUDIT_LOGINUID", "81"), ("_BOOT_ID", "bad"),
            ("_AUDIT_SESSION", "4294967295"),
            ("MESSAGE", record["MESSAGE"].replace("by (uid=0)", "by (uid=1)"))):
        th.assert_eq(detect_journal(dict(record, **{field: value})), None,
                     f"mutating authoritative PAM field {field} must fail closed")


@th.django_unit_test()
def test_local_only_classifier_requires_the_complete_coherent_tuple(opts):
    import copy

    from mojo.mojosec.detectors import detect_journal
    from mojo.mojosec.disposition import is_local_only
    from mojo.mojosec.protocol import make_event

    record = {
        "_UID": "0", "_PID": "4123", "_COMM": "(systemd)",
        "_EXE": "/usr/lib/systemd/systemd", "_SYSTEMD_UNIT": "user@80.service",
        "_BOOT_ID": "b" * 32, "_AUDIT_SESSION": "44", "_AUDIT_LOGINUID": "80",
        "__REALTIME_TIMESTAMP": "1786190400000000",
        "MESSAGE": "pam_unix(systemd-user:session): session opened for user www(uid=80) by (uid=0)",
    }
    found = detect_journal(record)
    th.assert_true(is_local_only(found, wire=False),
                   "the detector's complete trusted observation must classify local-only")
    wire = make_event("sensor-classifier", found)
    th.assert_true(is_local_only(wire, wire=True),
                   "the same semantic tuple must classify in validated wire shape")
    attributed = copy.deepcopy(wire)
    attributed["attributes"].update({
        "attribution_provenance": "audit_session", "source_ip": "192.0.2.80",
    })
    th.assert_true(is_local_only(attributed, wire=True),
                   "exact audit-session attribution with a canonical IP remains local-only")

    aggregate = copy.deepcopy(found)
    aggregate["aggregate"] = True
    th.assert_true(not is_local_only(aggregate, wire=False),
                   "an aggregate-capable observation must retain ordinary delivery")

    mutations = []
    for field, value in (
            ("kind", "auth.ssh_login"), ("severity", "warning"),
            ("summary", "PAM session opened"), ("recommendation", "review"),
            ("count", 2), ("count", True)):
        changed = copy.deepcopy(wire)
        changed[field] = value
        mutations.append((field, changed))
    for field, value in (
            ("service", "sshd"), ("target_user", "bad user"),
            ("target_uid", 81), ("opener_uid", 1), ("producer_uid", 1),
            ("producer_pid", 0), ("producer_comm", "systemd"),
            ("producer_exe", "/tmp/systemd"), ("systemd_unit", "user@81.service"),
            ("boot_id", "bad"), ("audit_session", -1), ("audit_loginuid", 81),
            ("attribution_provenance", "who")):
        changed = copy.deepcopy(wire)
        changed["attributes"][field] = value
        mutations.append((field, changed))
    for field in (
            "target_uid", "opener_uid", "producer_uid", "producer_pid",
            "audit_session", "audit_loginuid"):
        changed = copy.deepcopy(wire)
        changed["attributes"][field] = False
        mutations.append((f"boolean {field}", changed))
    missing = copy.deepcopy(wire)
    del missing["attributes"]["producer_exe"]
    mutations.append(("missing producer_exe", missing))
    extra = copy.deepcopy(wire)
    extra["attributes"]["message"] = "trusted-looking extra"
    mutations.append(("extra evidence", extra))
    contradictory = copy.deepcopy(wire)
    contradictory["attributes"].update({
        "attribution_provenance": "none", "source_ip": "192.0.2.1",
    })
    mutations.append(("contradictory source", contradictory))
    for field in ("source_ip", "tty"):
        explicit_null = copy.deepcopy(wire)
        explicit_null["attributes"][field] = None
        mutations.append((f"null {field}", explicit_null))
    for label, changed in mutations:
        th.assert_true(not is_local_only(changed, wire=True),
                       f"near-match {label} must retain ordinary fleet delivery")


@th.django_unit_test()
def test_journal_collector_streams_forward_without_tail_skips(opts):
    import mojo.mojosec.collectors.journal as journal_module

    records = [_journal_record(f"cursor-{index}", f"192.0.2.{index}") for index in range(1, 4)]
    payload = b"".join(json.dumps(record).encode("utf-8") + b"\n" for record in records)
    commands = []
    collector = journal_module.JournalCollector(_journal_config(max_records=2))
    with _patch_journal_popen(journal_module, payload, commands):
        result = collector.poll("cursor-0")

    th.assert_eq(len(result["observations"]), 2,
                 "the record ceiling should return the first two records after the cursor")
    th.assert_eq(result["cursor"], "cursor-2",
                 "the durable cursor must stop at the last processed record, preserving the burst remainder")
    th.assert_true(not any(argument.startswith("--lines") for argument in commands[0]),
                   "journalctl tail semantics must never skip the beginning of a post-cursor burst")
    th.assert_in("--after-cursor=cursor-0", commands[0],
                 "journal collection must stream forward from the committed cursor")


@th.django_unit_test()
def test_journal_poison_record_is_counted_and_cursor_advances(opts):
    import mojo.mojosec.collectors.journal as journal_module

    records = [_journal_record("cursor-1", "192.0.2.1"),
               _journal_record("cursor-2", "192.0.2.2")]
    payload = b"".join(json.dumps(record).encode("utf-8") + b"\n" for record in records)
    commands = []
    collector = journal_module.JournalCollector(_journal_config())
    with _patch_journal_popen(journal_module, payload, commands), \
            mock.patch.object(journal_module, "detect_journal", side_effect=(ValueError("poison"), None)):
        result = collector.poll()

    th.assert_eq(result["malformed"], 1,
                 "a detector failure must be counted as one malformed journal record")
    th.assert_eq(result["cursor"], "cursor-2",
                 "a poison record must not prevent progress to the next valid journal cursor")


@th.django_unit_test()
def test_journal_byte_ceiling_keeps_last_fully_processed_cursor(opts):
    import mojo.mojosec.collectors.journal as journal_module

    records = [_journal_record("cursor-1", "192.0.2.1"),
               _journal_record("cursor-2", "192.0.2.2")]
    encoded = [json.dumps(record).encode("utf-8") + b"\n" for record in records]
    payload = b"".join(encoded)
    commands = []
    config = _journal_config(max_bytes=len(encoded[0]) + len(encoded[1]) // 2)
    collector = journal_module.JournalCollector(config)
    with _patch_journal_popen(journal_module, payload, commands):
        result = collector.poll()

    th.assert_eq(len(result["observations"]), 1,
                 "the byte ceiling should retain only the fully processed first record")
    th.assert_eq(result["cursor"], "cursor-1",
                 "a partial normal record must remain after the cursor for the next poll")


@th.django_unit_test()
def test_nginx_detector_is_behavioral_and_quiet(opts):
    from mojo.mojosec.detectors import detect_nginx

    noisy_bot = detect_nginx({
        "time_iso8601": "2026-08-08T12:00:00+00:00", "status": 404,
        "request_method": "GET", "uri": "/ordinary-missing-page",
        "remote_addr": "192.0.2.3", "user_agent": "GPTBot/1.0",
    })
    th.assert_eq(noisy_bot, None,
                 "a user-agent claim and routine 404 must not create sensor noise")

    probe = detect_nginx({
        "time_iso8601": "2026-08-08T12:00:00+00:00", "status": 404,
        "request_method": "GET", "request_uri": "/wp-login.php?redirect=secret",
        "remote_addr": "198.51.100.5", "realip_remote_addr": "10.0.0.10",
        "referer": "https://example.invalid/private?token=secret",
    })
    th.assert_eq(probe["kind"], "web.probe",
                 "a known exploit path should create a high-signal probe event")
    th.assert_eq(probe["recommendation"], "block_ip",
                 "known exploit probes should carry an advisory block recommendation")
    th.assert_eq(probe["attributes"]["path"], "/wp-login.php",
                 "query strings must never be copied into incident evidence")
    th.assert_eq(probe["attributes"]["referrer"],
                 "https://example.invalid/private?token=secret",
                 "bounded raw referrer evidence must be available for central scrubbing")

    server_error = detect_nginx({
        "status": 502, "method": "POST", "path": "/api/orders",
        "source_ip": "203.0.113.22", "request_time": "1.250",
    })
    th.assert_eq(server_error["kind"], "web.error",
                 "nginx 5xx responses must be retained for operational detection")

    first_token = "AbCdEfGhIjKlMnOpQrStUvWxYz123456"
    second_token = "ZyXwVuTsRqPoNmLkJiHgFeDcBa654321"
    first_secret_path = detect_nginx({
        "status": 500, "method": "GET", "path": f"/api/reset/{first_token}",
    })
    second_secret_path = detect_nginx({
        "status": 500, "method": "GET", "path": f"/api/reset/{second_token}",
    })
    th.assert_true(first_token in first_secret_path["attributes"]["request_uri"],
                   "bounded raw request evidence must remain available to the protected receipt")
    th.assert_true(first_token not in first_secret_path["attributes"]["path"],
                   "the aggregation path must hash high-entropy URL segments")
    th.assert_eq(first_secret_path["fingerprint"], second_secret_path["fingerprint"],
                 "different reset tokens must share one bounded aggregation key")

    from mojo.mojosec.detectors import DetectorError
    with th.assert_raises(DetectorError):
        detect_nginx({"status": 700, "method": "GET", "path": "/bad-status"})
    with th.assert_raises(DetectorError):
        detect_nginx({"status": 500, "method": "GET", "path": "/slow",
                      "request_time": "NaN"})


@th.django_unit_test()
def test_nginx_collector_resumes_at_a_durable_byte_cursor(opts):
    from mojo.mojosec.collectors.nginx import NginxCollector

    with tempfile.TemporaryDirectory() as root:
        path = os.path.join(root, "security.json.log")
        first = {"status": 500, "method": "GET", "path": "/one", "source_ip": "192.0.2.1"}
        second = {"status": 502, "method": "GET", "path": "/two", "source_ip": "192.0.2.2"}
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(first) + "\n")
        collector = NginxCollector({
            "paths": [path], "max_bytes_per_poll": 65536, "max_line_bytes": 16384,
        })
        initial = collector.poll()
        th.assert_eq(len(initial["observations"]), 1,
                     "the first complete structured nginx line should be collected")

        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(second) + "\n")
        resumed = collector.poll(initial["cursor"])
        th.assert_eq(len(resumed["observations"]), 1,
                     "resuming from the committed byte cursor must collect only new lines")
        th.assert_eq(resumed["observations"][0]["attributes"]["path"], "/two",
                     "the resumed nginx event should be the newly appended record")

        os.replace(path, path + ".1")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "status": 503, "method": "GET", "path": "/after-rotate",
            }) + "\n")
        rotated = collector.poll(resumed["cursor"])
        th.assert_eq(len(rotated["observations"]), 1,
                     "a normal nginx rename+USR1 rotation must resume on the new inode")
        th.assert_eq(rotated["observations"][0]["attributes"]["path"],
                     "/after-rotate",
                     "the first complete record after rotation must be collected")

        # logrotate copytruncate preserves the inode. Regrow beyond the old
        # offset before the next poll to exercise the case a size-only cursor
        # would mistake for an ordinary append and silently skip.
        same_inode = os.stat(path).st_ino
        after_truncate = "/after-copytruncate"
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "status": 504, "method": "GET", "path": after_truncate,
                "padding": "x" * 400,
            }) + "\n")
        th.assert_eq(os.stat(path).st_ino, same_inode,
                     "the regression must model copytruncate on the active inode")
        truncated = collector.poll(rotated["cursor"])
        th.assert_eq(len(truncated["observations"]), 1,
                     "copytruncate/regrow must reset the durable cursor without stalling")
        th.assert_eq(truncated["observations"][0]["attributes"]["path"],
                     after_truncate,
                     "the first complete record after copytruncate must be collected")


@th.django_unit_test()
def test_nginx_poison_numeric_is_counted_without_stalling_cursor(opts):
    from mojo.mojosec.collectors.nginx import NginxCollector

    with tempfile.TemporaryDirectory() as root:
        path = os.path.join(root, "security.json.log")
        poison = {"status": 500, "method": "GET", "path": "/poison", "request_time": "inf"}
        valid = {"status": 502, "method": "GET", "path": "/valid"}
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(poison) + "\n")
            handle.write(json.dumps(valid) + "\n")
        collector = NginxCollector({
            "paths": [path], "max_bytes_per_poll": 65536, "max_line_bytes": 16384,
        })
        result = collector.poll()
        th.assert_eq(result["malformed"], 1,
                     "a non-finite nginx numeric must be rejected and counted")
        th.assert_eq(len(result["observations"]), 1,
                     "a poison nginx record must not suppress the following valid event")
        th.assert_eq(result["cursor"][path]["offset"], os.path.getsize(path),
                     "the nginx cursor must advance past rejected records so they cannot stall collection")


@th.django_unit_test()
def test_nginx_rich_line_cap_accepts_header_limits_and_rejects_overflow(opts):
    from mojo.mojosec.collectors.nginx import MAX_STRUCTURED_LINE_BYTES, NginxCollector

    with tempfile.TemporaryDirectory() as root:
        path = os.path.join(root, "security.json.log")
        rich = {
            "status": 500, "method": "GET",
            "request_uri": "/wp-login.php?" + "q" * 8192,
            "referrer": "https://example.invalid/" + "r" * 8192,
            "user_agent": "Agent/1 " + "u" * 8192,
            "host": "example.invalid",
        }
        encoded = json.dumps(rich).encode() + b"\n"
        th.assert_true(len(encoded) > 16 * 1024,
                       "regression input must exceed the retired 16 KiB ceiling")
        with open(path, "wb") as handle:
            handle.write(encoded)
        collector = NginxCollector({
            "paths": [path], "max_bytes_per_poll": 2 * MAX_STRUCTURED_LINE_BYTES,
            "max_line_bytes": MAX_STRUCTURED_LINE_BYTES,
        })
        result = collector.poll()
        th.assert_eq(len(result["observations"]), 1,
                     "valid nginx-header-limit evidence must reach the per-kind byte builder")
        th.assert_true(result["observations"][0]["attributes"].get("request_uri_truncated"),
                       "the evidence builder, not ingress, must bound a rich request target")

        overflow = b'{"padding":"' + b"x" * MAX_STRUCTURED_LINE_BYTES + b'"}\n'
        valid = json.dumps({
            "status": 502, "method": "GET", "request_uri": "/after-overflow",
        }).encode() + b"\n"
        with open(path, "wb") as handle:
            handle.write(overflow + valid)
        result = collector.poll()
        th.assert_eq((result["malformed"], len(result["observations"])), (1, 1),
                     "derived-cap overflow must fail closed without wedging the next record")


@th.django_unit_test()
def test_fim_detects_changes_without_following_symlinks(opts):
    from mojo.mojosec.collectors.fim import FimCollector

    with tempfile.TemporaryDirectory() as root:
        root = os.path.realpath(root)
        watched = os.path.join(root, "watched")
        outside = os.path.join(root, "outside")
        os.mkdir(watched)
        os.mkdir(outside)
        config_path = os.path.join(watched, "settings.conf")
        outside_path = os.path.join(outside, "secret.txt")
        with open(config_path, "w", encoding="utf-8") as handle:
            handle.write("safe\n")
        with open(outside_path, "w", encoding="utf-8") as handle:
            handle.write("outside\n")
        link_path = os.path.join(watched, "outside-link")
        os.symlink(outside, link_path)

        collector = FimCollector({
            "targets": [{"path": watched, "recursive": True}],
            "max_entries": 100, "max_file_bytes": 1024 * 1024, "max_depth": 16,
        })
        initial = collector.scan()
        th.assert_eq(initial["snapshot"][link_path]["kind"], "symlink",
                     "FIM must record a symlink itself without traversing it")
        th.assert_true(outside_path not in initial["snapshot"],
                       "recursive FIM must never escape a target through a symlink")

        with open(config_path, "w", encoding="utf-8") as handle:
            handle.write("changed\n")
        changed = collector.scan()
        events = collector.diff(initial["snapshot"], changed)
        th.assert_eq(len(events), 1,
                     "one targeted file modification should produce one FIM event")
        th.assert_eq(events[0]["attributes"]["change"], "modified",
                     "the FIM event must identify a file modification")


@th.django_unit_test()
def test_fim_symlink_swap_fails_closed_and_pending_work_is_bounded(opts):
    import mojo.mojosec.collectors.fim as fim_module

    with tempfile.TemporaryDirectory() as root:
        root = os.path.realpath(root)
        watched = os.path.join(root, "watched")
        outside = os.path.join(root, "outside-secret")
        os.mkdir(watched)
        with open(outside, "w", encoding="utf-8") as handle:
            handle.write("must-not-be-hashed\n")
        victim = os.path.join(watched, "victim.conf")
        with open(victim, "w", encoding="utf-8") as handle:
            handle.write("initial\n")
        for index in range(10):
            with open(os.path.join(watched, f"bounded-{index}.conf"), "w", encoding="utf-8") as handle:
                handle.write(str(index))

        collector = fim_module.FimCollector({
            "targets": [{"path": watched, "recursive": True}],
            "max_entries": 100, "max_file_bytes": 1024 * 1024, "max_depth": 16,
        })
        real_open = fim_module.os.open
        swapped = {"done": False}

        def swap_before_open(name, flags, *args, **kwargs):
            if name == "victim.conf" and kwargs.get("dir_fd") is not None and not swapped["done"]:
                os.unlink(victim)
                os.symlink(outside, victim)
                swapped["done"] = True
            return real_open(name, flags, *args, **kwargs)

        with mock.patch.object(fim_module.os, "open", side_effect=swap_before_open):
            scan = collector.scan()

        th.assert_eq(scan["complete"], False,
                     "a file-to-symlink swap must make the FIM scan incomplete")
        th.assert_true(victim not in scan["snapshot"],
                       "a raced symlink target must never be hashed into the FIM baseline")

        bounded_collector = fim_module.FimCollector({
            "targets": [{"path": watched, "recursive": True}],
            "max_entries": 4, "max_file_bytes": 1024 * 1024, "max_depth": 16,
        })
        bounded = bounded_collector.scan()
        th.assert_true(len(bounded["snapshot"]) <= 4,
                       "descriptor traversal must keep discovered work within max_entries")

        with mock.patch.object(bounded_collector, "_descriptor_walk_supported", return_value=False):
            unsupported = bounded_collector.scan()
        th.assert_eq(unsupported["complete"], False,
                     "platforms without descriptor-relative no-follow traversal must fail closed")
        th.assert_eq(unsupported["snapshot"], {},
                     "the fail-closed platform path must not use an unsafe pathname fallback")


@th.django_unit_test()
def test_standard_profile_is_immutable_bounded_and_covers_system_python(opts):
    from mojo.mojosec.config import build_config
    from mojo.mojosec.profiles import PRIVATE_TREES, resolve_profile

    profile = resolve_profile("al2023-web-v2")
    th.assert_eq(len(profile["digest"]), 64,
                 "the immutable profile must carry one deterministic SHA-256 identity")
    th.assert_eq(profile["tiers"]["fast"]["interval_seconds"], 60,
                 "the standard persistence graph must run every minute")
    th.assert_eq(profile["tiers"]["slow"]["interval_seconds"], 21600,
                 "the binary integrity graph must run every six hours")
    fast_paths = [item["path"] for item in profile["tiers"]["fast"]["targets"]]
    th.assert_eq(fast_paths.count("/usr/local/lib"), 1,
                 "the root-pip system site tree must have exactly one traversal owner")
    etc = next(item for item in profile["tiers"]["fast"]["targets"]
               if item["path"] == "/etc")
    th.assert_true("mojosec/**" in etc["exclude"],
                   "the expanded broad /etc graph must subtract MojoSec private state")
    th.assert_true(all(not any(path.startswith(private + "/") or path == private
                               for private in PRIVATE_TREES)
                       for path in fast_paths),
                   "no final submitted target may directly enter private MojoSec state")

    config = build_config({
        "sensor_id": "profile-test",
        "endpoint": "https://example.invalid/api/incident/mojosec/batch",
        "profile": "al2023-web-v2",
    })
    th.assert_eq(set(config["collectors"]["fim"]["tiers"]), {"fast", "slow"},
                 "selecting the profile name must resolve the packaged graph")
    th.assert_true(config["collectors"]["rpm"]["enabled"],
                   "the standard profile must monitor the imported system Python environment")


@th.django_unit_test()
def test_al2023_v2_uses_canonical_cloud_instance_tree_without_following_alias(opts):
    from mojo.mojosec.collectors.fim import FimCollector
    from mojo.mojosec.profiles import resolve_profile

    v1 = resolve_profile("al2023-web-v1")
    v2 = resolve_profile("al2023-web-v2")
    th.assert_eq(v1["digest"],
                 "6a45b233ae8dd87e77456d88a037ea435e6e9240d07884814fae4a23d30f3e0e",
                 "the retained v1 profile must keep its published immutable identity")
    th.assert_eq(v2["digest"],
                 "1cb26a911e1a2c2418d5a6428c873bc4118a39c8731c4d8442d7c4f7f3cbd14f",
                 "the v2 profile graph must keep its published immutable identity")
    v1_paths = [target["path"] for target in v1["tiers"]["fast"]["targets"]]
    v2_paths = [target["path"] for target in v2["tiers"]["fast"]["targets"]]
    th.assert_true("/var/lib/cloud/instance/scripts" in v1_paths,
                   "v1 must remain byte-stable for retained baselines and rollback")
    th.assert_true("/var/lib/cloud/instance/scripts" not in v2_paths,
                   "v2 must remove the descendant of AL2023's mutable instance alias")
    th.assert_true("/var/lib/cloud/instances" in v2_paths,
                   "v2 must retain canonical recursive cloud-init instance coverage")

    with tempfile.TemporaryDirectory() as root:
        root = os.path.realpath(root)
        cloud = os.path.join(root, "cloud")
        instances = os.path.join(cloud, "instances")
        instance = os.path.join(cloud, "instance")
        scripts = os.path.join(instances, "i-test", "scripts")
        os.makedirs(scripts)
        script = os.path.join(scripts, "part-001")
        with open(script, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\necho ready\n")
        os.symlink(os.path.join("instances", "i-test"), instance)

        alias_scan = FimCollector({
            "targets": [{"path": os.path.join(instance, "scripts"), "recursive": True}],
            "max_entries": 100, "max_file_bytes": 1024 * 1024, "max_depth": 16,
        }).scan()
        th.assert_eq(alias_scan["complete"], False,
                     "descriptor-safe traversal must fail closed at the instance symlink")
        th.assert_true(script not in alias_scan["snapshot"],
                       "a symlink-descendant target must never be followed into a baseline")

        canonical_scan = FimCollector({
            "targets": [{"path": instances, "recursive": True}],
            "max_entries": 100, "max_file_bytes": 1024 * 1024, "max_depth": 16,
        }).scan()
        th.assert_eq(canonical_scan["complete"], True,
                     "the canonical cloud-init instances tree must scan completely")
        th.assert_true(script in canonical_scan["snapshot"],
                       "canonical recursive coverage must retain cloud-init script bytes")


@th.django_unit_test()
def test_fim_reuses_strict_metadata_and_retains_alias_anomalies(opts):
    from mojo.mojosec.collectors.fim import FimCollector

    with tempfile.TemporaryDirectory() as root:
        root = os.path.realpath(root)
        watched = os.path.join(root, "site-packages")
        outside = os.path.join(root, "outside")
        os.mkdir(watched)
        os.mkdir(outside)
        module = os.path.join(watched, "module.py")
        alias = os.path.join(watched, "module-alias.py")
        pth = os.path.join(watched, "startup.pth")
        escape = os.path.join(watched, "escape")
        with open(module, "w", encoding="utf-8") as handle:
            handle.write("VALUE = 1\n")
        os.link(module, alias)
        with open(pth, "w", encoding="utf-8") as handle:
            handle.write("import attacker_startup\n")
        os.symlink(outside, escape)
        collector = FimCollector({
            "targets": [{"path": watched, "recursive": True}],
            "max_entries": 100, "max_file_bytes": 1024 * 1024, "max_depth": 16,
        })
        first = collector.scan()
        th.assert_eq(first["snapshot"][alias]["sha256"], first["snapshot"][module]["sha256"],
                     "hardlink aliases must retain logical observations while sharing content")
        th.assert_true(all("digest_reused" not in entry
                           for entry in first["snapshot"].values()),
                       "digest reuse is transient scan state, never baseline evidence")
        th.assert_eq(first["snapshot"][pth]["anomaly"], "pth_executable",
                     "an executable .pth directive must be explicit integrity evidence")
        th.assert_eq(first["snapshot"][escape]["anomaly"], "symlink_escape",
                     "a symlink escaping the approved site root must be an anomaly")

        second = collector.scan(first["snapshot"])
        th.assert_eq(collector.diff(first["snapshot"], second), [],
                     "an unchanged second scan, including hardlinks, must diff empty")
        th.assert_eq(second["snapshot"], first["snapshot"],
                     "digest reuse must not alter persisted comparison state")


@th.django_unit_test()
def test_system_python_roots_are_strict_and_fim_remains_legacy_rpm_tier(opts):
    from mojo.mojosec.collectors.rpm import (
        SystemPythonCollector, SystemPythonError, discover_system_python_roots,
    )

    candidates = [
        "/usr/lib/python3.11/site-packages",
        "/usr/lib64/python3.11/site-packages",
        "/usr/local/lib/python3.11/site-packages",
        "/opt/api/.venv/lib/python3.11/site-packages",
        "/tmp/attacker/site-packages",
    ]
    config = {
        "interpreter": "/usr/bin/python3", "interval_seconds": 21600,
        "max_entries": 100, "max_packages": 10, "max_owner_queries": 100,
        "max_output_bytes": 65536, "timeout_seconds": 5,
        "max_file_bytes": 1024, "max_depth": 16,
    }
    roots = discover_system_python_roots(config, roots_provider=lambda: candidates)
    th.assert_eq(roots, [
        "/usr/lib/python3.11/site-packages",
        "/usr/lib64/python3.11/site-packages",
        "/usr/local/lib/python3.11/site-packages",
    ], "in-process discovery must reject project and temporary environments")
    for invalid in (lambda: [], lambda: ["/tmp/site-packages"], lambda: [None] * 129):
        with th.assert_raises(SystemPythonError):
            discover_system_python_roots(config, roots_provider=invalid)


@th.django_unit_test()
def test_system_python_collector_preserves_complete_and_incomplete_fim(opts):
    from mojo.mojosec.collectors.rpm import SystemPythonCollector

    root = "/usr/lib/python3.12/site-packages"
    module_path = root + "/module.py"
    link_path = root + "/module-link.py"
    walk_calls = []
    complete = [True, False]

    class Walker:
        def __init__(self, config, expected_changes_path, identity, tier,
                     hash_filter=None):
            th.assert_eq(hash_filter, None,
                         "the system-Python walk must not suppress any file hashes")
            th.assert_eq(config["max_file_bytes"], 1024,
                         "the system-Python walk must retain its file-size bound")
            th.assert_eq(config["targets"], [
                {"path": root, "recursive": True, "optional": False},
            ], "the descriptor walk must contain only approved site roots")

        def scan(self, previous):
            walk_calls.append(previous)
            return {
                "complete": complete.pop(0),
                "snapshot": {
                    module_path: {"kind": "file", "sha256": "c" * 64},
                    link_path: {
                        "kind": "symlink", "target_sha256": "d" * 64,
                        "anomaly": "symlink_escape",
                    },
                },
            }

    config = {
        "interpreter": "/usr/bin/python3", "interval_seconds": 21600,
        "max_entries": 100, "max_packages": 10, "max_owner_queries": 100,
        "max_output_bytes": 65536, "timeout_seconds": 5,
        "max_file_bytes": 1024, "max_depth": 16,
    }
    collector = SystemPythonCollector(
        config, {"name": "al2023-web-v2", "version": 2, "digest": "e" * 64},
        roots_provider=lambda: [root], fim_factory=Walker,
    )
    retained = {"prior": {"kind": "file", "sha256": "f" * 64}}
    scan = collector.scan(previous=retained)
    th.assert_eq(walk_calls, [retained],
                 "the descriptor walk must receive the prior complete baseline")
    th.assert_eq(scan["snapshot"][module_path]["sha256"], "c" * 64,
                 "a regular Python file must retain its descriptor-safe hash")
    th.assert_eq(scan["snapshot"][link_path]["target_sha256"], "d" * 64,
                 "a Python symlink must retain its target hash")
    th.assert_eq(scan["anomalies"], 1,
                 "FIM anomalies must remain visible on the compatibility tier")
    th.assert_eq(scan["packages"], 0,
                 "the stable preview field must not imply package inspection")
    th.assert_eq(scan["tier"], "rpm",
                 "the stored tier identity must remain backward compatible")

    incomplete = collector.scan(previous=retained)
    th.assert_true(not incomplete["complete"],
                   "an incomplete descriptor walk must stay incomplete")
    th.assert_eq(retained, {"prior": {"kind": "file", "sha256": "f" * 64}},
                 "an incomplete scan must leave the prior baseline untouched")


@th.django_unit_test()
def test_fim_paths_with_undecodable_bytes_stay_deliverable(opts):
    from mojo.mojosec.collectors.fim import FimCollector
    from mojo.mojosec.events import bounded_text
    from mojo.mojosec.protocol import make_event, validate_event

    collector = FimCollector({"targets": []})
    bad_path = b"/etc/bad-\xff".decode("utf-8", "surrogateescape")
    sibling_path = b"/etc/bad-\xfe".decode("utf-8", "surrogateescape")

    def one_change(path):
        scan = {"snapshot": {path: {"kind": "file", "size": 1}}, "complete": True}
        found = collector.diff({}, scan)
        th.assert_eq(len(found), 1,
                     "one created path must yield exactly one FIM change")
        return found[0]

    first = one_change(bad_path)
    second = one_change(sibling_path)
    try:
        first["attributes"]["path"].encode("utf-8")
    except UnicodeEncodeError:
        th.assert_true(False,
                       "a surrogateescape filesystem path must be normalized into "
                       "storable evidence, not shipped raw")
    event = make_event("sensor-test", first)
    validate_event(event)
    th.assert_true(first["fingerprint"] != second["fingerprint"],
                   "paths differing only in invalid bytes must keep distinct event "
                   "identities via the raw-path fingerprint")

    normalized = bounded_text("summary \udcff tail", 64)
    try:
        normalized.encode("utf-8")
    except UnicodeEncodeError:
        th.assert_true(False,
                       "bounded_text must normalize lone surrogates so every summary "
                       "is storable by construction")


@th.django_unit_test()
def test_system_service_error_requires_pid1_failure_and_collapses_by_unit(opts):
    from mojo.mojosec.detectors import detect_journal
    from mojo.mojosec.events import fingerprint

    kex_noise = (
        "error: kex_exchange_identification: Connection closed by remote host",
        "error: kex_exchange_identification: read: Connection reset by peer",
        'error: kex_exchange_identification: client sent invalid protocol '
        'identifier "GET / HTTP/1.1"',
    )
    for message in kex_noise:
        record = {
            "SYSLOG_IDENTIFIER": "sshd", "_UID": "0", "_COMM": "sshd",
            "_EXE": "/usr/sbin/sshd", "_SYSTEMD_UNIT": "sshd.service",
            "PRIORITY": "3", "MESSAGE": message,
        }
        th.assert_eq(detect_journal(record), None,
                     "routine err-level daemon lines must not become service failures")

    failure = {
        "_PID": "1", "_UID": "0", "_COMM": "systemd",
        "SYSLOG_IDENTIFIER": "systemd", "_SYSTEMD_UNIT": "init.scope",
        "PRIORITY": "4", "MESSAGE": "api.service: Failed with result 'exit-code'.",
        "__REALTIME_TIMESTAMP": "1786190400000000",
    }
    fired = detect_journal(failure)
    th.assert_eq(fired["kind"], "system.service_error",
                 "a PID 1 unit-failure declaration must fire a service failure")
    th.assert_eq(fired["severity"], "high",
                 "genuine unit failures must stay high severity")
    th.assert_eq(fired["aggregate"], True,
                 "unit failures must aggregate so restart loops collapse")
    th.assert_eq(fired["attributes"]["unit"], "api.service",
                 "the failed unit must be named, not systemd's init.scope bookkeeping unit")
    th.assert_eq(fired["attributes"]["failure_kind"], "exit-code",
                 "the systemd result vocabulary must reach evidence as failure_kind")
    th.assert_true("Failed with result" in fired["attributes"].get("message", ""),
                   "the bounded failure message must stay in evidence")
    repeat = detect_journal(dict(failure, __REALTIME_TIMESTAMP="1786190460000000"))
    th.assert_eq(fired["fingerprint"], repeat["fingerprint"],
                 "repeats of one unit failure must share one aggregation key")
    th.assert_eq(fired["fingerprint"],
                 fingerprint("system.service_error", ("api.service", "exit-code")),
                 "the fingerprint must be (unit, failure_kind) with no volatile message text")

    entered = detect_journal(dict(
        failure, MESSAGE="api.service: Unit entered failed state."))
    th.assert_eq((entered["kind"], entered["attributes"]["failure_kind"]),
                 ("system.service_error", "failed"),
                 "the mid-era entered-failed-state wording must fire with kind 'failed'")
    legacy = detect_journal(dict(
        failure, MESSAGE="Unit api.service entered failed state."))
    th.assert_eq(legacy["attributes"]["unit"], "api.service",
                 "the pre-230 legacy failure wording must still name the unit")
    scope_unit = detect_journal(dict(
        failure, MESSAGE="session-42.scope: Failed with result 'timeout'."))
    th.assert_eq((scope_unit["attributes"]["unit"], scope_unit["attributes"]["failure_kind"]),
                 ("session-42.scope", "timeout"),
                 "non-service unit types must be accepted by the failure grammar")
    drifted = detect_journal(dict(
        failure, MESSAGE="api.service has gone belly-up.",
        MESSAGE_ID="d9b373ed55a64feb8242e02dbe79a49c", UNIT="api.service"))
    th.assert_eq((drifted["attributes"]["unit"], drifted["attributes"]["failure_kind"]),
                 ("api.service", "unknown"),
                 "the unit-failed MESSAGE_ID fallback must survive future wording drift")

    for mutation in (
            dict(failure, _PID="200"),
            dict(failure, _UID="1000"),
            dict(failure, MESSAGE="api.service: Main process exited, "
                                  "code=exited, status=1/FAILURE"),
            dict(failure, MESSAGE="Failed to start LSB: api daemon."),
            dict(failure, _PID="4242", _COMM="myapp",
                 SYSLOG_IDENTIFIER="myapp", _SYSTEMD_UNIT="myapp.service",
                 MESSAGE="api.service: Failed with result 'exit-code'."),
            dict(failure, MESSAGE="api.service has gone belly-up.",
                 MESSAGE_ID="d9b373ed55a64feb8242e02dbe79a49c", UNIT="../etc"),
            dict(failure, MESSAGE="api.service has gone belly-up.",
                 MESSAGE_ID="d9b373ed55a64feb8242e02dbe79a49c"),
    ):
        th.assert_eq(detect_journal(mutation), None,
                     "anything but a PID 1 root unit-failure declaration must fail closed")


@th.django_unit_test()
def test_system_oom_is_kernel_transport_only_and_single_fire(opts):
    from mojo.mojosec.detectors import detect_journal

    kill_line = (
        "Out of memory: Killed process 21437 (gunicorn) total-vm:1482912kB, "
        "anon-rss:1201234kB, file-rss:0kB, shmem-rss:0kB, UID:1001 "
        "pgtables:2891kB oom_score_adj:0"
    )
    genuine = detect_journal({
        "SYSLOG_IDENTIFIER": "kernel", "_TRANSPORT": "kernel",
        "PRIORITY": "3", "MESSAGE": kill_line,
    })
    th.assert_eq(genuine["kind"], "system.oom",
                 "a kernel-transport OOM kill line must fire the critical OOM event")
    th.assert_eq(genuine["severity"], "critical",
                 "kernel OOM kills must stay critical")
    th.assert_eq(genuine["aggregate"], False,
                 "a kernel OOM kill is a discrete incident, never aggregated away")
    th.assert_eq(genuine["attributes"]["unit"], "kernel",
                 "kernel-origin OOM evidence must attribute to the kernel")
    memcg = detect_journal({
        "SYSLOG_IDENTIFIER": "kernel", "_TRANSPORT": "kernel", "PRIORITY": "3",
        "MESSAGE": "Memory cgroup out of memory: Killed process 991 (api) "
                   "total-vm:100kB, anon-rss:90kB",
    })
    th.assert_eq(memcg["kind"], "system.oom",
                 "cgroup OOM kill lines must fire like global kills")

    app_text = {
        "SYSLOG_IDENTIFIER": "myapp", "_SYSTEMD_UNIT": "myapp.service",
        "_TRANSPORT": "journal", "PRIORITY": "6",
        "MESSAGE": "watchdog: killed process tree for job 7",
    }
    th.assert_eq(detect_journal(app_text), None,
                 "application text echoing OOM phrases must never fire a critical event")
    th.assert_eq(detect_journal(dict(app_text, PRIORITY="3")), None,
                 "the err-level catch-all is gone: app text stays silent at priority 3")
    forged = detect_journal({
        "SYSLOG_IDENTIFIER": "kernel", "_TRANSPORT": "syslog",
        "PRIORITY": "3", "MESSAGE": kill_line,
    })
    th.assert_eq(forged, None,
                 "a forged kernel identifier without kernel transport must fail closed")

    for detail_line in (
            "oom-kill:constraint=CONSTRAINT_NONE,nodemask=(null),cpuset=/,"
            "mems_allowed=0,task=gunicorn,pid=21437,uid=1001",
            "python3 invoked oom-killer: gfp_mask=0x140cca(GFP_HIGHUSER_MOVABLE"
            "|__GFP_COMP), order=0, oom_score_adj=0",
    ):
        record = {"SYSLOG_IDENTIFIER": "kernel", "_TRANSPORT": "kernel",
                  "PRIORITY": "3", "MESSAGE": detail_line}
        th.assert_eq(detect_journal(record), None,
                     "one kernel OOM kill must fire exactly one event, not one per log line")

    routed = detect_journal({
        "_PID": "1", "_UID": "0", "_COMM": "systemd",
        "SYSLOG_IDENTIFIER": "systemd", "_SYSTEMD_UNIT": "init.scope",
        "PRIORITY": "4", "MESSAGE": "api.service: Failed with result 'oom-kill'.",
    })
    th.assert_eq((routed["kind"], routed["attributes"]["failure_kind"]),
                 ("system.service_error", "oom-kill"),
                 "PID 1's oom-kill unit failure must route to service_error, not system.oom")


@th.django_unit_test()
def test_incomplete_fim_scan_emits_only_overflow_until_reconciled(opts):
    from mojo.mojosec.collectors.fim import FimCollector

    with tempfile.TemporaryDirectory() as root:
        root = os.path.realpath(root)
        watched = os.path.join(root, "watched")
        os.mkdir(watched)
        for index in range(10):
            with open(os.path.join(watched, f"file-{index}.conf"), "w", encoding="utf-8") as handle:
                handle.write(f"content {index}\n")

        bounded = FimCollector({
            "targets": [{"path": watched, "recursive": True}],
            "max_entries": 4, "max_file_bytes": 1024 * 1024, "max_depth": 16,
        })
        partial = bounded.scan()
        th.assert_eq(partial["complete"], False,
                     "a scan capped below the tree size must report itself incomplete")
        th.assert_true(len(partial["snapshot"]) > 0,
                       "the capped scan must still carry a non-empty partial snapshot")

        observations = bounded.diff({}, partial)
        th.assert_eq([item["kind"] for item in observations], ["fim.overflow"],
                     "an incomplete scan must yield exactly one overflow and zero change events")
        th.assert_eq(observations[0]["severity"], "critical",
                     "the incomplete-scan overflow signal must stay critical")
        th.assert_eq(observations[0]["attributes"]["entries"], len(partial["snapshot"]),
                     "the overflow evidence must report the partial snapshot size")

        for _ in range(3):
            repeat = bounded.diff({}, partial)
            th.assert_eq([item["kind"] for item in repeat], ["fim.overflow"],
                         "every interval of a persistently incomplete scan must emit only the overflow")

        manifest_path = os.path.join(root, "expected.json")
        with open(manifest_path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        with_manifest = FimCollector({
            "targets": [{"path": watched, "recursive": True}],
            "max_entries": 4, "max_file_bytes": 1024 * 1024, "max_depth": 16,
        }, expected_changes_path=manifest_path)
        noisy = with_manifest.scan()
        th.assert_eq(noisy["complete"], False,
                     "the manifest-bearing collector must also overflow at the same cap")
        th.assert_eq([item["kind"] for item in with_manifest.diff({}, noisy)], ["fim.overflow"],
                     "a malformed manifest must stay silent while the scan is incomplete")

        adequate = FimCollector({
            "targets": [{"path": watched, "recursive": True}],
            "max_entries": 100, "max_file_bytes": 1024 * 1024, "max_depth": 16,
        })
        baseline_scan = adequate.scan()
        th.assert_eq(baseline_scan["complete"], True,
                     "an adequately bounded scan over the fixture must complete")
        baseline = baseline_scan["snapshot"]

        modified_path = os.path.join(watched, "file-0.conf")
        deleted_path = os.path.join(watched, "file-1.conf")
        with open(modified_path, "w", encoding="utf-8") as handle:
            handle.write("changed content\n")
        os.remove(deleted_path)

        rescan = adequate.scan()
        th.assert_eq(rescan["complete"], True,
                     "the reconcile fixture scan must complete")
        fake_incomplete = dict(rescan)
        fake_incomplete["complete"] = False
        th.assert_eq([item["kind"] for item in adequate.diff(baseline, fake_incomplete)],
                     ["fim.overflow"],
                     "real pending changes must stay withheld while a scan is incomplete")

        reconciled = adequate.diff(baseline, rescan)
        th.assert_true(all(item["kind"] == "fim.change" for item in reconciled),
                       "a complete reconcile scan must emit only change events, no overflow")
        changes = {item["attributes"]["path"]: item["attributes"]["change"]
                   for item in reconciled}
        th.assert_eq(changes.get(modified_path), "modified",
                     "the first complete scan must report the modification deferred while incomplete")
        th.assert_eq(changes.get(deleted_path), "deleted",
                     "the first complete scan must report the deletion deferred while incomplete")
        th.assert_true("created" not in changes.values(),
                       "a reconcile against the retained baseline must not invent created paths")
