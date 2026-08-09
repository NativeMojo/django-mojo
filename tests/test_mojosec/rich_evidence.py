import json
import os
import sqlite3
import tempfile
import time
from unittest import mock

from testit import helpers as th


AGGREGATION = {
    "window_seconds": 60, "flush_count": 25, "max_aggregates": 100,
    "critical_reserve_aggregates": 10,
}
DELIVERY = {
    "max_spool_events": 100, "critical_reserve_events": 10,
    "retry_min_seconds": 1, "retry_max_seconds": 2,
}


def _v1_database(root, version=1):
    path = os.path.join(root, "state.sqlite3")
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE events (
            id TEXT PRIMARY KEY, payload TEXT NOT NULL, severity TEXT NOT NULL,
            created REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt REAL NOT NULL DEFAULT 0,
            annotation_deadline REAL NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '');
        CREATE INDEX events_delivery ON events(next_attempt, created);
        CREATE TABLE aggregates (
            fingerprint TEXT PRIMARY KEY, payload TEXT NOT NULL, severity TEXT NOT NULL,
            count INTEGER NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
            flush_at REAL NOT NULL);
        CREATE INDEX aggregates_due ON aggregates(flush_at);
        CREATE TABLE fim_baseline (
            profile TEXT NOT NULL, path TEXT NOT NULL, entry TEXT NOT NULL,
            PRIMARY KEY(profile, path));
    """)
    db.execute("INSERT INTO meta(key, value) VALUES('schema_version', ?)",
               (json.dumps(version),))
    db.execute("INSERT INTO meta(key, value) VALUES('cursor:journal', ?)",
               (json.dumps("cursor-kept"),))
    db.execute("INSERT INTO fim_baseline(profile, path, entry) VALUES(?, ?, ?)",
               ("profile", "/etc/kept", json.dumps({"kind": "file"})))
    db.execute(
        "INSERT INTO events(id, payload, severity, created) VALUES(?, ?, ?, ?)",
        ("queued-event", json.dumps({"kept": True}), "high", 1.0))
    db.execute(
        "INSERT INTO aggregates(fingerprint, payload, severity, count, first_seen, "
        "last_seen, flush_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
        ("queued-aggregate", json.dumps({"kept": True}), "warning", 2,
         "2026-08-09T12:00:00Z", "2026-08-09T12:01:00Z", 2.0))
    db.commit()
    db.close()
    os.chmod(path, 0o600)
    return path


@th.django_unit_test()
def test_store_v1_to_v2_migration_is_atomic_retryable_and_future_safe(opts):
    from mojo.mojosec.store import Store, StoreError

    with tempfile.TemporaryDirectory() as root:
        os.chmod(root, 0o700)
        path = _v1_database(root)
        with mock.patch.object(
                Store, "_create_ssh_session_schema", side_effect=RuntimeError("injected")):
            with th.assert_raises(RuntimeError):
                Store(root, "sensor", AGGREGATION, DELIVERY)
        db = sqlite3.connect(path)
        th.assert_eq(json.loads(db.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]), 1,
            "a failed migration must leave the advertised version at v1")
        th.assert_eq(db.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='ssh_sessions'"
        ).fetchone()[0], 0, "a failed migration must roll its new table back")
        db.close()

        store = Store(root, "sensor", AGGREGATION, DELIVERY)
        th.assert_eq(store.get_meta("schema_version"), 2,
                     "retry must complete the idempotent v1-to-v2 migration")
        th.assert_eq(store.get_meta("cursor:journal"), "cursor-kept",
                     "migration must preserve durable collection cursors")
        th.assert_eq(store.load_fim_baseline("profile")["/etc/kept"]["kind"], "file",
                     "migration must preserve FIM baselines")
        th.assert_eq(store.db.execute(
            "SELECT COUNT(*) FROM events WHERE id='queued-event'").fetchone()[0], 1,
            "migration must preserve the delivery spool")
        th.assert_eq(store.db.execute(
            "SELECT count FROM aggregates WHERE fingerprint='queued-aggregate'"
        ).fetchone()[0], 2, "migration must preserve pending aggregates")
        store.close()
        reopened = Store(root, "sensor", AGGREGATION, DELIVERY)
        th.assert_eq(reopened.get_meta("schema_version"), 2,
                     "opening an already-migrated v2 store must be idempotent")
        reopened.close()

    with tempfile.TemporaryDirectory() as root:
        os.chmod(root, 0o700)
        path = _v1_database(root, version=99)
        before = open(path, "rb").read()
        with th.assert_raises(StoreError):
            Store(root, "sensor", AGGREGATION, DELIVERY)
        after = open(path, "rb").read()
        th.assert_eq(after, before,
                     "a future schema must be rejected without mutating the database")


@th.django_unit_test()
def test_ssh_session_overlay_fallback_freshness_and_ambiguity(opts):
    from mojo.mojosec.attribution import AttributionResolver

    timestamp = 1786190400000000
    ssh = {
        "_BOOT_ID": "a" * 32, "_AUDIT_SESSION": "77", "_TTY": "pts/4",
        "__REALTIME_TIMESTAMP": str(timestamp),
        "MESSAGE": "Accepted publickey for deploy from 192.0.2.8 port 5000 ssh2",
    }
    sudo = {
        "_BOOT_ID": "a" * 32, "_AUDIT_SESSION": "77",
        "__REALTIME_TIMESTAMP": str(timestamp + 10 * 1000000),
        "MESSAGE": "deploy : TTY=pts/4 ; USER=root ; COMMAND=/usr/bin/id",
    }
    resolver = AttributionResolver([], [])
    resolver.overlay([sudo, ssh])
    source, provenance, context = resolver.resolve(sudo, "deploy", "pts/4")
    th.assert_eq((source, provenance), ("192.0.2.8", "audit_session"),
                 "same-poll overlay must resolve exact boot plus audit-session identity")
    th.assert_eq(context["audit_session"], 77,
                 "the exact Linux audit session should remain evidence")

    fallback = dict(sudo, _AUDIT_SESSION="4294967295")
    fresh = {"actor": "deploy", "tty": "pts/4", "source_ip": "198.51.100.9",
             "observed_at": timestamp / 1000000}
    resolver = AttributionResolver([], [fresh])
    th.assert_eq(resolver.resolve(fallback, "deploy", "pts/4")[:2],
                 ("198.51.100.9", "who"),
                 "one fresh exact actor-plus-TTY who row may attribute the source")
    stale = dict(fresh, observed_at=fresh["observed_at"] - 301)
    th.assert_eq(AttributionResolver([], [stale]).resolve(
        fallback, "deploy", "pts/4")[:2], ("", "none"),
        "stale who rows must never be reused")
    th.assert_eq(AttributionResolver([], [fresh, dict(fresh)]).resolve(
        fallback, "deploy", "pts/4")[:2], ("", "none"),
        "ambiguous who rows must never attribute a source")


@th.django_unit_test()
def test_ssh_session_store_is_atomic_persistent_ttl_and_capped(opts):
    from mojo.mojosec.store import SSH_SESSION_CAP, SSH_SESSION_TTL_SECONDS, Store

    with tempfile.TemporaryDirectory() as root:
        os.chmod(root, 0o700)
        store = Store(root, "sensor", AGGREGATION, DELIVERY)
        now = time.time()
        sessions = [{
            "boot_id": f"{index:032x}", "audit_session": index,
            "actor": "deploy", "tty": "pts/0", "source_ip": "192.0.2.8",
            "observed_at": now - index,
        } for index in range(SSH_SESSION_CAP + 4)]
        sessions.append({
            "boot_id": "f" * 32, "audit_session": 99999, "actor": "old",
            "tty": "pts/9", "source_ip": "192.0.2.9",
            "observed_at": now - SSH_SESSION_TTL_SECONDS - 1,
        })
        store.ingest([], cursor_key="journal", cursor="cursor-2", ssh_sessions=sessions)
        th.assert_eq(len(store.load_ssh_sessions(now=now)), SSH_SESSION_CAP,
                     "session correlation state must retain only the newest 4096 rows")
        th.assert_true(all(row["actor"] != "old" for row in store.load_ssh_sessions(now=now)),
                       "session correlation state must drop rows older than 30 days")
        store.close()
        restarted = Store(root, "sensor", AGGREGATION, DELIVERY)
        th.assert_eq(restarted.get_meta("cursor:journal"), "cursor-2",
                     "cursor and session overlay must survive one atomic restart")
        th.assert_eq(len(restarted.load_ssh_sessions(now=now)), SSH_SESSION_CAP,
                     "bounded session correlation must survive restart")
        restarted.close()


@th.django_unit_test()
def test_evidence_byte_budget_unicode_poison_and_truthful_fingerprints(opts):
    from mojo.mojosec.detectors import detect_nginx
    from mojo.mojosec.evidence import EVIDENCE_BYTES, build_evidence, encoded_size

    evidence = build_evidence("web.error", {
        "source_ip": "192.0.2.1", "method": "GET", "status": 500,
        "request_uri": "/" + "\ud800雪" * 5000,
        "referrer": "https://example.invalid/" + "雪" * 5000,
        "user_agent": "agent/1 " + "雪" * 5000,
    })
    th.assert_true(encoded_size(evidence) <= EVIDENCE_BYTES,
                   "UTF-8 and JSON expansion must stay below the protocol attribute limit")
    th.assert_true(evidence.get("request_uri_truncated") is True,
                   "truncation must be explicit before SQLite persistence")
    th.assert_true(len(evidence.get("request_uri_sha256", "")) == 64,
                   "truncated evidence must retain a deterministic full-value digest")

    first = detect_nginx({
        "status": 500, "method": "GET", "request_uri": "/same",
        "source_ip": "192.0.2.1", "host": "one.example",
    })
    second = detect_nginx({
        "status": 500, "method": "GET", "request_uri": "/same",
        "source_ip": "192.0.2.2", "host": "one.example",
    })
    third = detect_nginx({
        "status": 502, "method": "POST", "request_uri": "/same",
        "source_ip": "192.0.2.1", "host": "two.example",
    })
    th.assert_true(len({first["fingerprint"], second["fingerprint"],
                        third["fingerprint"]}) == 3,
                   "interleaved IP, host, method, and status identities must not collapse")
