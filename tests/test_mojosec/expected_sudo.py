"""The one sudo the deployed automation is expected to run (maestro #9769)."""

import json
import os
import sqlite3
import tempfile
import time

from testit import helpers as th


AGGREGATION = {
    "window_seconds": 60, "flush_count": 2, "max_aggregates": 100,
    "critical_reserve_aggregates": 10,
}
DELIVERY = {
    "batch_events": 100, "batch_bytes": 256 * 1024, "timeout_seconds": 10,
    "retry_min_seconds": 1, "retry_max_seconds": 30, "gzip": True,
    "max_spool_events": 100, "critical_reserve_events": 10,
}
BROKER = "/usr/local/sbin/mojo-firewall-broker"


def _sudo_record(message=None, **changes):
    # The production AL2023 shape: sudo's own journal line is root-authored
    # (_UID=0); the invoking identity is the kernel login UID.
    record = {
        "__CURSOR": "sudo-observation", "__REALTIME_TIMESTAMP": "1761700000000000",
        "__MONOTONIC_TIMESTAMP": "1000000", "_BOOT_ID": "a" * 32,
        "_TRANSPORT": "syslog", "SYSLOG_IDENTIFIER": "sudo", "_COMM": "sudo",
        "_EXE": "/usr/bin/sudo", "_UID": "0", "_PID": "20",
        "_AUDIT_LOGINUID": "1000", "_AUDIT_SESSION": "9",
        "_SELINUX_CONTEXT": "system_u:system_r:crond_t:s0-s0:c0.c1023",
        "MESSAGE": message or f"ec2-user : PWD=/opt/api ; USER=root ; COMMAND={BROKER}",
    }
    record.update(changes)
    return record


def _detect(record, sessions=None):
    from mojo.mojosec.attribution import AttributionResolver
    from mojo.mojosec.detectors import detect_journal

    found = detect_journal(record, AttributionResolver(sessions, who_sessions=[]))
    th.assert_true(found is not None and found["kind"] == "auth.sudo_command",
                   "the fixture must still parse as a trusted sudo command")
    return found


def _open(root, expected_app_uid=1000):
    from mojo.mojosec.store import Store

    return Store(root, "sensor", AGGREGATION, DELIVERY,
                 local_only_diagnostic_path=os.path.join(root, "missing"),
                 expected_app_uid=expected_app_uid)


@th.django_unit_test()
def test_expected_broker_sudo_is_local_only_and_near_misses_raise(opts):
    from mojo.mojosec.disposition import APP_USER_FIREWALL_BROKER_CLASSIFIER

    with tempfile.TemporaryDirectory() as root:
        store = _open(root)
        try:
            store.ingest([_detect(_sudo_record())], cursor_key="journal", cursor="c1")
            stats = store.stats()
            th.assert_eq(stats["local_only_observed"], 1,
                         "the app user's tty-less broker sudo is the expected automation")
            th.assert_eq(stats["local_only_suppressed"], 1,
                         "without the diagnostic override the expectation is counted, not delivered")
            th.assert_eq(store.get_meta(
                f"local_only:{APP_USER_FIREWALL_BROKER_CLASSIFIER}:observed"), 1,
                "the status counters must name the fixed classifier")
            th.assert_eq(store.pending_batch(10, 65536), [],
                         "an expected broker sudo must never reach the ordinary spool")
            th.assert_eq(store.get_meta("cursor:journal"), "c1",
                         "the journal cursor must advance in the same transaction")
            th.assert_eq(stats["local_only_last_seen"], "2025-10-29T01:06:40Z",
                         "the last-seen marker must carry the observation time")
        finally:
            store.close()

    ssh_session = [{"boot_id": "a" * 32, "audit_session": 9, "actor": "ec2-user",
                    "tty": "", "source_ip": "192.0.2.8", "observed_at": time.time(),
                    "ambiguous": False}]
    near_misses = (
        ("interactive tty", _sudo_record(
            f"ec2-user : TTY=pts/1 ; PWD=/opt/api ; USER=root ; COMMAND={BROKER}"), None, 1000),
        ("ssh-attributed session", _sudo_record(), ssh_session, 1000),
        ("another login uid", _sudo_record(_AUDIT_LOGINUID="0"), None, 1000),
        ("unset login uid", _sudo_record(_AUDIT_LOGINUID="4294967295"), None, 1000),
        ("another command", _sudo_record(
            "ec2-user : PWD=/opt/api ; USER=root ; COMMAND=/usr/bin/systemctl restart api"),
         None, 1000),
        ("broker with arguments", _sudo_record(
            f"ec2-user : PWD=/opt/api ; USER=root ; COMMAND={BROKER} --status"), None, 1000),
        ("another target user", _sudo_record(
            f"ec2-user : PWD=/opt/api ; USER=ec2-user ; COMMAND={BROKER}"), None, 1000),
        ("truncated command", _sudo_record(
            f"ec2-user : PWD=/opt/api ; USER=root ; COMMAND={BROKER}" + " " * 4096 + "x"),
         None, 1000),
        ("no configured app uid", _sudo_record(), None, None),
        ("app uid of a different account", _sudo_record(), None, 1001),
    )
    for label, record, sessions, app_uid in near_misses:
        with tempfile.TemporaryDirectory() as root:
            store = _open(root, expected_app_uid=app_uid)
            try:
                store.ingest([_detect(record, sessions)])
                events = store.pending_batch(10, 65536)
                th.assert_eq(len(events), 1, f"{label} must stay an ordinary alert")
                th.assert_eq(events[0]["kind"], "auth.sudo_command",
                             f"{label} must keep its sudo evidence kind")
                th.assert_true("local_disposition" not in events[0]["attributes"],
                               f"{label} must not carry the expectation marker")
                th.assert_eq(store.stats()["local_only_observed"], 0,
                             f"{label} must never be counted as expected automation")
            finally:
                store.close()


@th.django_unit_test()
def test_store_open_retires_provenance_tables_and_drains_held_candidates(opts):
    from mojo.mojosec.disposition import APP_USER_FIREWALL_BROKER_CLASSIFIER
    from mojo.mojosec.protocol import canonical_json
    from mojo.mojosec.store import RETIRED_PROVENANCE_TABLES

    with tempfile.TemporaryDirectory() as root:
        _open(root).close()
        held = _detect(_sudo_record())
        path = os.path.join(root, "state.sqlite3")
        db = sqlite3.connect(path)
        for name in RETIRED_PROVENANCE_TABLES:
            if name != "pending_firewall":
                db.execute(f"CREATE TABLE {name}(payload TEXT)")
                db.execute(f"INSERT INTO {name} VALUES('{{}}')")
        db.execute("CREATE TABLE pending_firewall(observation_id TEXT PRIMARY KEY, "
                   "payload TEXT, operation_id TEXT, created REAL, expires REAL, state TEXT)")
        db.execute("INSERT INTO pending_firewall VALUES(?,?,?,?,?,?)",
                   (held["fingerprint"], canonical_json(held), "", time.time(),
                    time.time() + 30, "pending"))
        db.execute("INSERT INTO pending_firewall VALUES(?,?,?,?,?,?)",
                   ("broken", "not json", "", time.time(), time.time() + 30, "pending"))
        db.execute("INSERT INTO meta VALUES('audit_health', '{}')")
        db.execute("INSERT INTO meta VALUES('provenance_pending_expired', '7')")
        db.commit()
        db.close()
        before = os.path.getsize(path)

        store = _open(root)
        try:
            tables = {row["name"] for row in store.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            th.assert_true(not tables & set(RETIRED_PROVENANCE_TABLES),
                           f"every retired provenance table must be dropped on open: {tables}")
            th.assert_eq(store.get_meta("audit_health"), None,
                         "the audit-health meta key must go with the health epochs")
            th.assert_eq(store.get_meta("provenance_pending_expired"), None,
                         "the old provenance counters must go with the tables")
            th.assert_true(isinstance(store.get_meta("provenance_retired_at"), float),
                           "the retirement must be stamped once")
            th.assert_eq(store.get_meta("provenance_retired_drained"), 1,
                         "the held broker sudo must be drained through the live path")
            th.assert_eq(store.get_meta("provenance_retired_dropped"), 1,
                         "an unparsable held row is counted, never a startup failure")
            th.assert_eq(store.get_meta(
                f"local_only:{APP_USER_FIREWALL_BROKER_CLASSIFIER}:observed"), 1,
                "a held expected broker sudo becomes local-only, not an alert and not lost")
            th.assert_eq(store.pending_batch(10, 65536), [],
                         "draining must not spool the expected sudo")
            th.assert_true(os.path.getsize(path) <= before,
                           "the vacuum must give the dropped pages back")
        finally:
            store.close()
        reopened = _open(root)
        try:
            th.assert_eq(reopened.get_meta("provenance_retired_drained"), 1,
                         "a second open must be a no-op, not a second drain")
            th.assert_true(reopened.get_meta("sensor_id") == "sensor",
                           "the spool identity must survive the retirement")
        finally:
            reopened.close()
