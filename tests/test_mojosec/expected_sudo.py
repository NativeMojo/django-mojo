import json
import os
import sqlite3
import tempfile
import time

from testit import helpers as th


AGGREGATION = {
    "window_seconds": 60, "flush_count": 25, "max_aggregates": 100,
    "critical_reserve_aggregates": 10,
}
DELIVERY = {
    "max_spool_events": 100, "critical_reserve_events": 10,
    "retry_min_seconds": 1, "retry_max_seconds": 2,
}
BROKER = "/usr/local/sbin/mojo-firewall-broker"
CLASSIFIER = "app_user_firewall_broker_v1"
RETIRED_TABLES = (
    "audit_fragments", "process_nodes", "origin_sessions", "crond_launches",
    "audit_health_epochs", "pending_firewall", "firewall_receipts",
)


def _broker_record(**changes):
    # The AL2023 broker sudo as journald records it: sudo's syslog line is
    # root-authored, and the app identity is the cron session's login UID.
    record = {
        "_BOOT_ID": "a" * 32, "_UID": "0", "_AUDIT_LOGINUID": "1000",
        "_AUDIT_SESSION": "9",
        "_SELINUX_CONTEXT": "system_u:system_r:crond_t:s0-s0:c0.c1023",
        "__CURSOR": "sudo-observation", "_TRANSPORT": "syslog",
        "SYSLOG_IDENTIFIER": "sudo", "_COMM": "sudo", "_EXE": "/usr/bin/sudo",
        "_PID": "20", "__MONOTONIC_TIMESTAMP": "1000000",
        "__REALTIME_TIMESTAMP": "1786190400000000",
        "MESSAGE": f"ec2-user : PWD=/opt/api ; USER=root ; COMMAND={BROKER}",
    }
    record.update(changes)
    return record


def _store(root, expected_app_uid=1000):
    from mojo.mojosec.store import Store

    return Store(root, "expected-sudo", AGGREGATION, DELIVERY,
                 local_only_diagnostic_path=os.path.join(root, "absent.json"),
                 expected_app_uid=expected_app_uid)


def _spooled(store):
    return [dict(row) for row in store.db.execute(
        "SELECT severity, delivery_class, payload FROM events").fetchall()]


@th.django_unit_test()
def test_expected_broker_sudo_is_local_only_and_near_misses_raise(opts):
    from mojo.mojosec.attribution import AttributionResolver
    from mojo.mojosec.detectors import detect_journal

    with tempfile.TemporaryDirectory() as root:
        os.chmod(root, 0o700)
        store = _store(root)
        try:
            found = detect_journal(_broker_record())
            th.assert_eq(found["kind"], "auth.sudo_command",
                         "the broker record must parse as a sudo command")
            store.ingest([found], cursor_key="journal", cursor="c1")
            th.assert_eq(_spooled(store), [],
                         "the expected broker sudo must never reach the ordinary spool")
            th.assert_eq(store.get_meta("local_only_observed"), 1,
                         "the expected broker sudo must be counted local-only")
            th.assert_eq(store.get_meta(f"local_only:{CLASSIFIER}:observed"), 1,
                         "the count must name the app-user broker classifier")
            th.assert_eq(store.get_meta(f"local_only:{CLASSIFIER}:suppressed"), 1,
                         "without the root diagnostic override it is suppressed")
        finally:
            store.close()

    attributed = AttributionResolver([{
        "boot_id": "a" * 32, "audit_session": 9, "actor": "ec2-user", "tty": "",
        "source_ip": "192.0.2.10", "observed_at": time.time(), "ambiguous": False,
    }])
    near_misses = (
        ("a TTY", _broker_record(
            MESSAGE=f"ec2-user : TTY=pts/0 ; PWD=/opt/api ; USER=root ; COMMAND={BROKER}")),
        ("an SSH-attributed session", _broker_record(), attributed),
        ("another login UID", _broker_record(_AUDIT_LOGINUID="0")),
        ("another command", _broker_record(
            MESSAGE="ec2-user : PWD=/opt/api ; USER=root ; COMMAND=/usr/bin/systemctl restart api")),
        ("another target user", _broker_record(
            MESSAGE=f"ec2-user : PWD=/opt/api ; USER=ec2-user ; COMMAND={BROKER}")),
        ("a truncated command", _broker_record(
            MESSAGE=f"ec2-user : PWD=/opt/api ; USER=root ; COMMAND={BROKER} {'a' * 4096}")),
    )
    cases = [(label, record, resolver[0] if resolver else None, 1000)
             for label, record, *resolver in near_misses]
    cases.append(("no expected app user", _broker_record(), None, None))
    for label, record, resolver, expected_uid in cases:
        with tempfile.TemporaryDirectory() as root:
            os.chmod(root, 0o700)
            store = _store(root, expected_app_uid=expected_uid)
            try:
                found = detect_journal(record, resolver)
                th.assert_eq(found["kind"], "auth.sudo_command",
                             f"{label}: the near miss must still parse as sudo")
                store.ingest([found], cursor_key="journal", cursor="c1")
                spooled = _spooled(store)
                th.assert_eq(len(spooled), 1,
                             f"{label}: the near miss must spool exactly one event")
                th.assert_eq((spooled[0]["severity"], spooled[0]["delivery_class"]),
                             ("high", "ordinary"),
                             f"{label}: the near miss must be an ordinary high alarm")
                th.assert_true("local_disposition" not in json.loads(
                    spooled[0]["payload"])["attributes"],
                    f"{label}: a near miss must carry no local disposition")
                th.assert_eq(store.get_meta("local_only_observed", 0), 0,
                             f"{label}: a near miss must not be counted local-only")
            finally:
                store.close()


@th.django_unit_test()
def test_store_open_retires_provenance_tables_and_drains_held_candidates(opts):
    from mojo.mojosec.detectors import detect_journal

    with tempfile.TemporaryDirectory() as root:
        os.chmod(root, 0o700)
        store = _store(root)
        store.ingest([], cursor_key="journal", cursor="kept-cursor")
        store.close()
        held = detect_journal(_broker_record())
        held["attributes"].update({"proof_status": "partial", "origin_kind": "cron_jobman"})
        path = os.path.join(root, "state.sqlite3")
        db = sqlite3.connect(path)
        for name in RETIRED_TABLES:
            db.execute(f"CREATE TABLE {name}(payload TEXT, created REAL)")
        db.execute("DROP TABLE pending_firewall")
        db.execute(
            "CREATE TABLE pending_firewall(observation_id TEXT PRIMARY KEY,"
            "payload TEXT NOT NULL,operation_id TEXT NOT NULL DEFAULT '',"
            "created REAL NOT NULL,expires REAL NOT NULL,state TEXT NOT NULL DEFAULT 'pending')")
        db.execute(
            "INSERT INTO pending_firewall(observation_id,payload,created,expires) "
            "VALUES(?,?,?,?)", (held["fingerprint"], json.dumps(held), time.time(),
                                time.time() + 30))
        db.execute("INSERT INTO pending_firewall(observation_id,payload,created,expires) "
                   "VALUES('broken','{not json',?,?)", (time.time(), time.time() + 30))
        db.execute("INSERT INTO process_nodes VALUES(?,?)", ("x" * 4096, time.time()))
        for key in ("audit_health", "provenance_pending_expired"):
            db.execute("INSERT INTO meta(key,value) VALUES(?,?)", (key, json.dumps(1)))
        db.commit()
        db.close()

        store = _store(root)
        try:
            tables = {row["name"] for row in store.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            th.assert_true(not set(RETIRED_TABLES) & tables,
                           f"every provenance table must be dropped on open: {tables}")
            th.assert_eq(_spooled(store), [],
                         "the held broker sudo must not become an ordinary alarm")
            th.assert_eq(store.get_meta(f"local_only:{CLASSIFIER}:observed"), 1,
                         "the held broker sudo must be counted local-only")
            th.assert_eq(store.get_meta("provenance_retired_dropped"), 1,
                         "an unparsable held row must be skipped and counted")
            th.assert_eq(store.get_meta("audit_health"), None,
                         "the audit-health meta key must be removed")
            th.assert_eq(store.get_meta("provenance_pending_expired"), None,
                         "provenance counters must be removed")
            th.assert_true(isinstance(store.get_meta("provenance_retired_at"), float),
                           "the retirement time must be recorded")
            th.assert_eq(store.get_meta("cursor:journal"), "kept-cursor",
                         "an upgraded node must keep its collector cursors")
        finally:
            store.close()

        retired_at = None
        store = _store(root)
        try:
            retired_at = store.get_meta("provenance_retired_at")
            th.assert_eq(store.get_meta("provenance_retired_dropped"), 1,
                         "a second open must not repeat or erase the retirement")
        finally:
            store.close()
        th.assert_true(retired_at is not None, "the retirement time must survive reopen")
