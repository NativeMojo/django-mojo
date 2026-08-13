import json
import os
import sqlite3
import sys
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


def _old_v2_database(root):
    """Build the pre-ambiguity v2 shape that briefly existed in development."""
    path = _v1_database(root)
    db = sqlite3.connect(path)
    db.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (json.dumps(2),))
    db.executescript("""
        CREATE TABLE ssh_sessions (
            boot_id TEXT NOT NULL,
            audit_session INTEGER NOT NULL,
            actor TEXT NOT NULL,
            tty TEXT NOT NULL DEFAULT '',
            source_ip TEXT NOT NULL,
            observed_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY(boot_id, audit_session));
        CREATE INDEX ssh_sessions_observed ON ssh_sessions(observed_at);
    """)
    db.execute(
        "INSERT INTO ssh_sessions VALUES(?, ?, ?, ?, ?, ?, ?)",
        ("a" * 32, 71, "deploy", "pts/1", "192.0.2.71", time.time(), time.time()))
    db.commit()
    db.close()
    return path


@th.django_unit_test()
def test_store_v1_to_v3_migration_is_atomic_retryable_and_future_safe(opts):
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
        th.assert_eq(store.get_meta("schema_version"), 4,
                     "retry must complete the idempotent v1-to-v4 migration")
        th.assert_eq(store.get_meta("cursor:journal"), "cursor-kept",
                     "migration must preserve durable collection cursors")
        th.assert_eq(store.load_fim_baseline("profile")["/etc/kept"]["kind"], "file",
                     "migration must preserve FIM baselines")
        th.assert_eq(store.db.execute(
            "SELECT COUNT(*) FROM events WHERE id='queued-event'").fetchone()[0], 1,
            "migration must preserve the delivery spool")
        th.assert_eq(store.db.execute(
            "SELECT delivery_class FROM events WHERE id='queued-event'").fetchone()[0],
            "legacy", "migrated spool rows must enter bounded compatibility reconciliation")
        th.assert_eq(store.db.execute(
            "SELECT count FROM aggregates WHERE fingerprint='queued-aggregate'"
        ).fetchone()[0], 2, "migration must preserve pending aggregates")
        store.close()
        reopened = Store(root, "sensor", AGGREGATION, DELIVERY)
        th.assert_eq(reopened.get_meta("schema_version"), 4,
                     "opening an already-migrated v4 store must be idempotent")
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
def test_store_migrates_old_v2_compatibility_columns_atomically(opts):
    from mojo.mojosec.store import Store

    with tempfile.TemporaryDirectory() as root:
        os.chmod(root, 0o700)
        path = _old_v2_database(root)
        with mock.patch.object(
                Store, "_add_event_delivery_class",
                side_effect=RuntimeError("injected after ambiguous ALTER")):
            with th.assert_raises(RuntimeError):
                Store(root, "sensor", AGGREGATION, DELIVERY)
        db = sqlite3.connect(path)
        columns = {row[1] for row in db.execute("PRAGMA table_info(ssh_sessions)")}
        th.assert_true("ambiguous" not in columns,
                       "failure after the ambiguous ALTER must roll that ALTER back")
        event_columns = {row[1] for row in db.execute("PRAGMA table_info(events)")}
        th.assert_true("delivery_class" not in event_columns,
                       "failed v2-to-v4 migration must not expose its delivery column")
        th.assert_eq(json.loads(db.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]), 2,
            "failed compatibility repair must preserve the advertised v2 version")
        db.close()

        store = Store(root, "sensor", AGGREGATION, DELIVERY)
        th.assert_eq(store.get_meta("schema_version"), 4,
                     "retry must advance the repaired v2 store to v4")
        th.assert_eq(store.db.execute(
            "SELECT delivery_class FROM events WHERE id='queued-event'"
        ).fetchone()[0], "legacy",
                     "v2 queued rows must be marked legacy before the migrated store closes")
        row = store.load_ssh_sessions()[0]
        th.assert_eq((row["actor"], row["source_ip"], row["ambiguous"]),
                     ("deploy", "192.0.2.71", 0),
                     "retry must preserve old-v2 rows and default them unambiguous")
        store.close()
        reopened = Store(root, "sensor", AGGREGATION, DELIVERY)
        th.assert_eq(reopened.load_ssh_sessions()[0]["audit_session"], 71,
                     "the repaired v2 store must reopen idempotently")
        reopened.close()


@th.django_unit_test()
def test_ssh_session_overlay_fallback_freshness_and_ambiguity(opts):
    from mojo.mojosec.attribution import AttributionResolver, ssh_session

    timestamp = 1786190400000000
    ssh = {
        "_BOOT_ID": "a" * 32, "_AUDIT_SESSION": "77", "_TTY": "pts/4",
        "_TRANSPORT": "audit", "_AUDIT_TYPE_NAME": "USER_LOGIN", "_UID": "0",
        "__REALTIME_TIMESTAMP": str(timestamp),
        "MESSAGE": ('op=login acct="deploy" exe="/usr/sbin/sshd" '
                    'addr=192.0.2.8 terminal=ssh res=success'),
    }
    sudo = {
        "_BOOT_ID": "a" * 32, "_AUDIT_SESSION": "77",
        "_UID": "0", "_COMM": "sudo", "_EXE": "/usr/bin/sudo",
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

    forged = dict(ssh, _TRANSPORT="journal")
    th.assert_eq(AttributionResolver([], []).overlay([forged]), [],
                 "an accepted-looking user journal message must not establish audit identity")

    split = dict(
        ssh,
        MESSAGE=('op=login acct="deploy" '
                 'exe="/usr/libexec/openssh/sshd-session" '
                 'addr=192.0.2.8 terminal=ssh res=success'),
    )
    th.assert_eq(ssh_session(split)["source_ip"], "192.0.2.8",
                 "the deployed split-OpenSSH audit executable must establish attribution")
    matching_types = dict(split, _AUDIT_TYPE="1112")
    th.assert_eq(ssh_session(matching_types)["audit_session"], 77,
                 "matching audit type name and number must remain compatible")
    th.assert_eq(ssh_session(dict(split, _AUDIT_TYPE="1105")), None,
                 "contradictory allowed audit type name and number must fail closed")
    th.assert_eq(ssh_session(dict(
        split, AUDIT_TYPE_NAME="USER_START", _AUDIT_TYPE="1112")), None,
        "contradictory audit type-name aliases must fail closed")
    th.assert_eq(ssh_session(dict(
        split, _AUDIT_TYPE="1112", AUDIT_TYPE="1105")), None,
        "contradictory audit type-number aliases must fail closed")
    mutations = (
        ("_TRANSPORT", "journal"), ("_AUDIT_TYPE_NAME", "USER_AUTH"),
        ("_UID", "1000"), ("_BOOT_ID", "bad"),
        ("_AUDIT_SESSION", "4294967295"),
    )
    for field, value in mutations:
        th.assert_eq(ssh_session(dict(split, **{field: value})), None,
                     f"mutating authoritative SSH audit field {field} must fail closed")
    for old, new in (
            ("res=success", "res=failed"), ("terminal=ssh", "terminal=pts/1"),
            ("exe=\"/usr/libexec/openssh/sshd-session\"", "exe=\"/tmp/sshd\""),
            ("acct=\"deploy\"", "acct=\"bad user\""),
            ("addr=192.0.2.8", "addr=not-an-ip")):
        th.assert_eq(ssh_session(dict(split, MESSAGE=split["MESSAGE"].replace(old, new))), None,
                     f"mutating authoritative SSH audit message field {old} must fail closed")


@th.django_unit_test()
def test_who_fallback_is_stream_bounded_and_times_out_closed(opts):
    import mojo.mojosec.attribution as attribution

    valid = attribution.load_who_sessions(command=[
        sys.executable, "-c",
        "print('deploy pts/4 2026-08-09 12:00 (198.51.100.9)')",
    ])
    th.assert_eq(valid[0]["source_ip"], "198.51.100.9",
                 "one bounded C-locale who row should parse")
    overflow = attribution.load_who_sessions(command=[
        sys.executable, "-c",
        f"import sys;sys.stdout.write('x'*{attribution.WHO_MAX_BYTES + 1})",
    ])
    th.assert_eq(overflow, [], "who byte overflow must kill capture and fail closed")
    with mock.patch.object(attribution, "WHO_TIMEOUT_SECONDS", 0.05):
        timed_out = attribution.load_who_sessions(command=[
            sys.executable, "-c", "import time;time.sleep(1)",
        ])
    th.assert_eq(timed_out, [], "a stalled who process must be killed and fail closed")

    commands = []
    with mock.patch.object(attribution, "_who_output", side_effect=lambda command: commands.append(command) or b""):
        attribution.load_who_sessions()
    th.assert_eq(commands, [["/usr/bin/who"]],
                 "the production fallback must use portable plain who without unsupported flags")


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
def test_ssh_session_conflict_is_sticky_across_overlay_and_restart(opts):
    from mojo.mojosec.attribution import AttributionResolver
    from mojo.mojosec.store import Store

    first = {
        "boot_id": "a" * 32, "audit_session": 91, "actor": "deploy",
        "tty": "pts/1", "source_ip": "192.0.2.10", "observed_at": time.time(),
    }
    conflict = dict(first, source_ip="192.0.2.11")
    resolver = AttributionResolver([first], [{
        "actor": "deploy", "tty": "pts/1", "source_ip": "198.51.100.50",
        "observed_at": time.time(),
    }])
    conflict_record = {
        "_BOOT_ID": first["boot_id"], "_AUDIT_SESSION": 91, "_TTY": "pts/1",
        "_TRANSPORT": "audit", "_AUDIT_TYPE_NAME": "USER_START", "_UID": "0",
        "__REALTIME_TIMESTAMP": str(int(time.time() * 1000000)),
        "MESSAGE": ('acct="deploy" exe="/usr/sbin/sshd" addr=192.0.2.11 '
                    'terminal=ssh res=success'),
    }
    resolver.overlay([conflict_record, dict(
        conflict_record,
        MESSAGE=('acct="deploy" exe="/usr/sbin/sshd" addr=192.0.2.10 '
                 'terminal=ssh res=success'))])
    th.assert_eq(resolver.resolve({
        "_BOOT_ID": first["boot_id"], "_AUDIT_SESSION": 91,
        "__REALTIME_TIMESTAMP": str(int(time.time() * 1000000)),
    }, "deploy", "pts/1")[:2], ("", "none"),
        "an exact conflicting identity must not be restored by who fallback")

    mismatch = AttributionResolver([first], [{
        "actor": "other", "tty": "pts/1", "source_ip": "198.51.100.51",
        "observed_at": time.time(),
    }])
    th.assert_eq(mismatch.resolve({
        "_BOOT_ID": first["boot_id"], "_AUDIT_SESSION": 91,
        "__REALTIME_TIMESTAMP": str(int(time.time() * 1000000)),
    }, "other", "pts/1")[:2], ("", "none"),
        "an actor mismatch on an existing exact key must fail without who fallback")

    with tempfile.TemporaryDirectory() as root:
        os.chmod(root, 0o700)
        store = Store(root, "sensor", AGGREGATION, DELIVERY)
        store.ingest([], ssh_sessions=[first, conflict])
        row = store.load_ssh_sessions()[0]
        th.assert_true(bool(row["ambiguous"]),
                       "SQLite must persist a conflicting key as an ambiguous tombstone")
        store.ingest([], ssh_sessions=[first])
        store.close()
        restarted = Store(root, "sensor", AGGREGATION, DELIVERY)
        row = restarted.load_ssh_sessions()[0]
        th.assert_true(bool(row["ambiguous"]),
                       "later matching rows and restart must never restabilize a tombstone")
        th.assert_eq(AttributionResolver([row]).resolve({
            "_BOOT_ID": first["boot_id"], "_AUDIT_SESSION": 91,
            "__REALTIME_TIMESTAMP": str(int(time.time() * 1000000)),
        }, "deploy", "pts/1")[:2], ("", "none"),
            "a restarted ambiguous identity must remain unusable")
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
    th.assert_eq(first["fingerprint"], second["fingerprint"],
                 "web.error identity is the failure shape, so client address alone must not split it")
    th.assert_true(third["fingerprint"] not in (first["fingerprint"], second["fingerprint"]),
                   "distinct web.error status, method, and host identities must not collapse")
    denied_one = detect_nginx({
        "status": 403, "method": "GET", "request_uri": "/private",
        "source_ip": "192.0.2.1", "host": "one.example",
    })
    denied_two = detect_nginx({
        "status": 403, "method": "GET", "request_uri": "/private",
        "source_ip": "192.0.2.2", "host": "one.example",
    })
    th.assert_true(denied_one["fingerprint"] != denied_two["fingerprint"],
                   "web.denied stays per-actor: distinct client identities must not collapse")


@th.django_unit_test()
def test_web_error_collapses_client_ips_probes_stay_per_actor(opts):
    from mojo.mojosec.detectors import detect_nginx
    from mojo.mojosec.store import Store

    clients = ["192.0.2.10", "192.0.2.11", "192.0.2.12", "198.51.100.7",
               "203.0.113.9", "2001:db8::1"]
    errors = []
    for index, client in enumerate(clients):
        record = {
            "time": "2026-08-11T02:44:00Z", "status": 503, "method": "GET",
            "request_uri": "/api/orders", "remote_addr": client,
            "host": "api.example", "upstream_status": "-",
        }
        if index == 1:
            record["peer_addr"] = "10.0.0.9"
        errors.append(detect_nginx(record))
    th.assert_eq(len({item["fingerprint"] for item in errors}), 1,
                 "one failing endpoint must map every client, IPv6 and proxied included, "
                 "to one aggregation key")
    for item, client in zip(errors, clients):
        th.assert_eq(item["attributes"]["source_ip"], client,
                     "collapsing the aggregation key must not strip the per-client "
                     "source from evidence")

    with tempfile.TemporaryDirectory() as root:
        os.chmod(root, 0o700)
        store = Store(root, "sensor-test", AGGREGATION, DELIVERY)
        try:
            store.ingest(errors)
            stats = store.stats()
            th.assert_eq(stats["pending_aggregates"], 1,
                         "many clients of one server fault must occupy one aggregate row")
            th.assert_eq(stats["spooled_events"], 0,
                         "below flush_count one outage must stay in its aggregation window")
            row = store._aggregate_row(errors[0]["fingerprint"])
            th.assert_eq(row["count"], len(clients),
                         "the single aggregate row must count every client occurrence")
            th.assert_eq(row["observation"]["attributes"].get("source_ip"), clients[-1],
                         "the aggregate payload must retain the latest-occurrence sample source")
        finally:
            store.close()

    storm_aggregation = dict(AGGREGATION, flush_count=2)
    with tempfile.TemporaryDirectory() as root:
        os.chmod(root, 0o700)
        storm = Store(root, "sensor-test", storm_aggregation, DELIVERY)
        try:
            storm.ingest(errors)
            stats = storm.stats()
            th.assert_eq(stats["spooled_events"], 1,
                         "same-second window restarts must dedupe to one spooled event")
            th.assert_eq(stats["deduped_events"], 2,
                         "same-second window restarts must be counted, never silent")
        finally:
            storm.close()

    probes = [detect_nginx({
        "time": "2026-08-11T02:44:00Z", "status": 404, "method": "GET",
        "request_uri": "/wp-login.php", "remote_addr": client,
        "host": "api.example",
    }) for client in clients]
    th.assert_eq(len({item["fingerprint"] for item in probes}), len(clients),
                 "probe identity is the actor: distinct client IPs must stay distinct")
    with tempfile.TemporaryDirectory() as root:
        os.chmod(root, 0o700)
        probe_store = Store(root, "sensor-test", AGGREGATION, DELIVERY)
        try:
            probe_store.ingest(probes)
            th.assert_eq(probe_store.stats()["pending_aggregates"], len(clients),
                         "per-actor probe aggregation must keep one row per client")
        finally:
            probe_store.close()


@th.django_unit_test()
def test_nginx_rich_measurements_and_latest_aggregate_sample(opts):
    from mojo.mojosec.aggregation import merge
    from mojo.mojosec.detectors import detect_nginx
    from mojo.mojosec.protocol import make_event

    base = {
        "time": "2026-08-11T02:44:00Z", "status": 403, "method": "GET",
        "request_uri": "/private", "remote_addr": "192.0.2.80",
        "peer_addr": "10.0.0.8", "host": "example.invalid",
        "upstream_status": "403, -", "request_id": "a" * 32,
        "scheme": "https", "protocol": "HTTP/2.0", "tls_protocol": "TLSv1.3",
        "tls_cipher": "TLS_AES_256_GCM_SHA384", "remote_port": "52344",
        "peer_port": "443", "server_port": "443", "request_length": "512",
        "response_bytes": "1024", "response_body_bytes": "900",
        "request_time": "0.125", "upstream_connect_time": "0.010, -",
        "upstream_header_time": "0.020", "upstream_response_time": "0.030",
        "upstream_response_length": "777", "upstream_bytes_received": "800",
        "upstream_bytes_sent": "400", "referrer": "https://ref.invalid/path?q=secret",
        "user_agent": "curl/8.9.0",
    }
    first = detect_nginx(base)
    th.assert_eq(first["attributes"]["response_body_bytes"], 900,
                 "validated nginx response body bytes must reach protected evidence")
    th.assert_eq(first["attributes"]["upstream_connect_time"], "0.010",
                 "mixed nginx upstream '-' sentinels must be normalized as absence")

    latest = detect_nginx(dict(base, time="2026-08-11T02:44:01Z",
                               request_id="b" * 32, response_bytes="2048",
                               user_agent="Mozilla/5.0 Firefox/141.0"))
    aggregate = merge(None, first, 60)
    aggregate = merge(aggregate, latest, 60)
    event = make_event("sensor", aggregate["observation"], count=aggregate["count"],
                       first_seen=aggregate["first_seen"], last_seen=aggregate["last_seen"])
    th.assert_eq(event["count"], 2,
                 "two requests varying only volatile sample values must aggregate")
    th.assert_eq(event["attributes"]["request_id"], "b" * 32,
                 "the aggregate payload must retain the actual latest occurrence")

    from mojo.mojosec.detectors import DetectorError
    for field, value in (("remote_port", "0"), ("request_length", "-1"),
                         ("request_id", "A" * 32),
                         ("upstream_bytes_received", "1,broken"),
                         ("upstream_status", ",".join(["500"] * 9))):
        with th.assert_raises(DetectorError):
            detect_nginx(dict(base, **{field: value}))
