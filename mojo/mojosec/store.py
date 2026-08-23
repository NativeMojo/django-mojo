"""Crash-safe MojoSec state, aggregation, FIM baseline, and delivery spool."""

import datetime
import hashlib
import json
import os
import sqlite3
import stat
import time

from mojo.deploy.audit import select_health_fields

from .aggregation import merge, should_flush
from .attribution import merge_session
from .disposition import (
    JOBMAN_FIREWALL_CLASSIFIER, LOCAL_ONLY_DIAGNOSTIC_PATH,
    classify_local_only, diagnostic_override, is_local_only,
    observed_timestamp,
)
from .evidence import build_evidence
from .protocol import canonical_json, make_event


SCHEMA_VERSION = 3
SSH_SESSION_CAP = 4096
SSH_SESSION_TTL_SECONDS = 30 * 24 * 60 * 60
PROCESS_NODE_CAP = 131072
PROCESS_NODE_TTL_SECONDS = 7 * 24 * 60 * 60
PROCESS_PINNED_CAP = 64
PROCESS_NODE_MAX_BYTES = 96 * 1024 * 1024
AUDIT_FRAGMENT_CAP = 8192
AUDIT_FRAGMENT_TTL_SECONDS = 10 * 60
AUDIT_FRAGMENT_MAX_BYTES = 8 * 1024 * 1024
HEALTH_EPOCH_CAP = 128
PENDING_OPERATION_CAP = 4096
FIREWALL_RECEIPT_CAP = 32768
FIREWALL_RECEIPT_TTL_SECONDS = 7 * 24 * 60 * 60
PROVENANCE_MAX_BYTES = 256 * 1024 * 1024
STATE_MAX_BYTES = PROVENANCE_MAX_BYTES
PENDING_OPERATION_MAX_BYTES = 32 * 1024 * 1024
FIREWALL_RECEIPT_MAX_BYTES = 32 * 1024 * 1024
WAL_MAX_BYTES = 64 * 1024 * 1024
ANNOTATION_GRACE_SECONDS = 120
# The producer caps an active operation at 15 minutes (mojo.deploy
# .mojosec_changes.MAX_TTL_SECONDS). Retain exact active paths for that ceiling
# plus the post-completion match window of whichever tier produced the change.
ANNOTATION_MAX_OPERATION_SECONDS = 15 * 60
# The default hold, for every tier on the shared 300s deploy window.
ANNOTATION_MAX_GRACE_SECONDS = 20 * 60
# A tier may not hold evidence longer than the producer TTL plus the widest
# window config permits (collectors.fim.tiers.*.correlation_seconds).
ANNOTATION_MAX_CORRELATION_SECONDS = 1800
LOCAL_ONLY_RECONCILE_LIMIT = 256
SATURATING_COUNTER_MAX = 2 ** 63 - 1
_BROKER_FUNCTION_OPERATIONS = {
    "mojo.apps.incident.asyncjobs.broadcast_block_ip": {"rules.contains", "rule.insert"},
    "mojo.apps.incident.asyncjobs.broadcast_unblock_ip": {"rules.contains", "rule.delete"},
    "mojo.apps.incident.asyncjobs.broadcast_ipset_add_blocked": {
        "set.add", "set.rule_ensure"},
    "mojo.apps.incident.asyncjobs.broadcast_ipset_del_blocked": {"set.delete"},
    "mojo.apps.incident.asyncjobs.sync_firewall": {"set.replace", "set.rule_ensure"},
    "mojo.apps.incident.asyncjobs.broadcast_sync_ipset": {
        "set.replace", "set.rule_ensure"},
    "mojo.apps.incident.asyncjobs.broadcast_remove_ipset": {"set.remove"},
}


class StoreError(RuntimeError):
    pass


def _private_directory(path):
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
        info = os.lstat(path)
    except OSError as err:
        raise StoreError(f"cannot prepare state directory {path}: {err}") from err
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise StoreError(f"state directory is not a real directory: {path}")
    if info.st_mode & 0o077:
        raise StoreError(f"state directory must not be accessible by group or other: {path}")
    if os.geteuid() == 0 and info.st_uid != 0:
        raise StoreError(f"state directory must be owned by root: {path}")


class Store:
    def __init__(self, state_dir, sensor_id, aggregation_config, delivery_config,
                 local_only_diagnostic_path=LOCAL_ONLY_DIAGNOSTIC_PATH):
        _private_directory(state_dir)
        self.sensor_id = sensor_id
        self.aggregation_config = aggregation_config
        self.delivery_config = delivery_config
        self.local_only_diagnostic_path = local_only_diagnostic_path
        self.local_only_diagnostic = {"active": False, "until": "", "error": ""}
        self._repaired_v4 = False
        self.path = os.path.join(state_dir, "state.sqlite3")
        self.db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        try:
            self._create_schema()
        except Exception:
            self.db.close()
            raise
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=30000")
        # The state database also owns the event/FIM spool, so a global SQLite
        # page ceiling would let provenance starve unrelated durable evidence.
        # Provenance tables are budgeted independently below.
        self.db.execute(f"PRAGMA journal_size_limit={WAL_MAX_BYTES}")
        self.db.execute("PRAGMA wal_autocheckpoint=1000")
        os.chmod(self.path, 0o600)
        stored_sensor_id = self.get_meta("sensor_id")
        if stored_sensor_id is None:
            self.set_meta("sensor_id", sensor_id)
        elif stored_sensor_id != sensor_id:
            self.db.close()
            raise StoreError(
                f"state belongs to sensor {stored_sensor_id!r}, not configured sensor {sensor_id!r}"
            )
        if self._repaired_v4:
            self._flush_repaired_v4_pending()

    def _create_schema(self):
        tables = {row["name"] for row in self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if not tables:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self._create_base_schema()
                self._create_ssh_session_schema()
                self._create_private_schema()
                self.set_meta("schema_version", SCHEMA_VERSION)
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
            return
        if "meta" not in tables:
            raise StoreError("state schema has no version metadata")
        row = self.db.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        if row is None:
            raise StoreError("state schema version is missing")
        try:
            version = json.loads(row["value"])
        except json.JSONDecodeError as err:
            raise StoreError("state schema version is corrupt") from err
        if not isinstance(version, int) or isinstance(version, bool):
            raise StoreError("state schema version is invalid")
        if version == SCHEMA_VERSION:
            self._ensure_v3_schema()
            return
        if version == 4:
            self._repair_development_v4_to_v3()
            self._ensure_v3_schema()
            return
        if version not in (1, 2):
            raise StoreError(f"unsupported state schema version: {version}")
        if version == 1:
            self._migrate_v1_to_v3()
        else:
            self._migrate_v2_to_v3()
        self._ensure_v3_schema()

    def _repair_development_v4_to_v3(self):
        """Retire the never-public shared v4 marker without losing private state."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute(
                "SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if row is None or json.loads(row["value"]) != 4:
                raise StoreError("state schema changed during v4 compatibility repair")
            columns = {row["name"] for row in self.db.execute(
                "PRAGMA table_info(audit_fragments)").fetchall()}
            if "audit_serial" in columns and "audit_id" not in columns:
                statements = (
                    "CREATE TABLE audit_fragments_v3 ("
                    "boot_id TEXT NOT NULL,audit_id TEXT NOT NULL,"
                    "payload TEXT NOT NULL CHECK(length(payload)<=32768),"
                    "observed_at REAL NOT NULL,updated_at REAL NOT NULL,"
                    "PRIMARY KEY(boot_id,audit_id))",
                    "INSERT INTO audit_fragments_v3 SELECT boot_id,"
                    "CAST(audit_serial AS TEXT),payload,observed_at,updated_at "
                    "FROM audit_fragments",
                    "DROP TABLE audit_fragments",
                    "ALTER TABLE audit_fragments_v3 RENAME TO audit_fragments",
                    "CREATE INDEX audit_fragments_updated ON audit_fragments(updated_at)",
                )
                for statement in statements:
                    self.db.execute(statement)
            self.set_meta("schema_version", SCHEMA_VERSION)
            self.db.execute("COMMIT")
            self._repaired_v4 = True
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def _flush_repaired_v4_pending(self):
        """Make transitional v4 candidates ordinary before an older v3 downgrade."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            rows = self.db.execute(
                "SELECT observation_id,payload FROM pending_firewall "
                "WHERE state='pending' ORDER BY created,rowid LIMIT ?",
                (PENDING_OPERATION_CAP,)).fetchall()
            now = time.time()
            for row in rows:
                observation = json.loads(row["payload"])
                event = make_event(self.sensor_id, observation)
                exists = self.db.execute(
                    "SELECT 1 FROM events WHERE id=?", (event["id"],)).fetchone()
                if exists is not None or self._enqueue(event, now=now):
                    self.db.execute(
                        "DELETE FROM pending_firewall WHERE observation_id=?",
                        (row["observation_id"],))
                    self._increment_saturating("provenance_pending_downgrade_flush")
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def _create_base_schema(self):
        statements = (
            """CREATE TABLE meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )""",
            """CREATE TABLE events (
                id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                severity TEXT NOT NULL,
                created REAL NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt REAL NOT NULL DEFAULT 0,
                annotation_deadline REAL NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                delivery_class TEXT NOT NULL DEFAULT 'ordinary'
            )""",
            "CREATE INDEX events_delivery ON events(next_attempt, created)",
            "CREATE INDEX events_delivery_class ON events(delivery_class, next_attempt, created)",
            "CREATE INDEX events_delivery_class_created "
            "ON events(delivery_class, created, id)",
            """CREATE TABLE aggregates (
                fingerprint TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                severity TEXT NOT NULL,
                count INTEGER NOT NULL,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                flush_at REAL NOT NULL
            )""",
            "CREATE INDEX aggregates_due ON aggregates(flush_at)",
            """CREATE TABLE fim_baseline (
                profile TEXT NOT NULL,
                path TEXT NOT NULL,
                entry TEXT NOT NULL,
                PRIMARY KEY(profile, path)
            )""",
        )
        for statement in statements:
            self.db.execute(statement)

    def _create_ssh_session_schema(self):
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS ssh_sessions (
                boot_id TEXT NOT NULL,
                audit_session INTEGER NOT NULL,
                actor TEXT NOT NULL,
                tty TEXT NOT NULL DEFAULT '',
                source_ip TEXT NOT NULL,
                observed_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                ambiguous INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(boot_id, audit_session)
            )
        """)
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS ssh_sessions_observed ON ssh_sessions(observed_at)")

    def _add_ssh_session_ambiguous_column(self):
        self.db.execute(
            "ALTER TABLE ssh_sessions "
            "ADD COLUMN ambiguous INTEGER NOT NULL DEFAULT 0")

    def _add_event_delivery_class(self):
        self.db.execute(
            "ALTER TABLE events ADD COLUMN delivery_class TEXT NOT NULL DEFAULT 'legacy'")
        self.db.execute(
            "CREATE INDEX events_delivery_class ON events(delivery_class, next_attempt, created)")
        self.db.execute(
            "CREATE INDEX events_delivery_class_created "
            "ON events(delivery_class, created, id)")

    def _create_private_schema(self):
        statements = (
            """CREATE TABLE IF NOT EXISTS audit_fragments (
                boot_id TEXT NOT NULL, audit_id TEXT NOT NULL,
                payload TEXT NOT NULL CHECK(length(payload) <= 32768),
                observed_at REAL NOT NULL, updated_at REAL NOT NULL,
                PRIMARY KEY(boot_id, audit_id)
            )""",
            "CREATE INDEX IF NOT EXISTS audit_fragments_updated ON audit_fragments(updated_at)",
            """CREATE TABLE IF NOT EXISTS process_nodes (
                boot_id TEXT NOT NULL, pid INTEGER NOT NULL, generation TEXT NOT NULL,
                audit_session INTEGER, payload TEXT NOT NULL CHECK(length(payload) <= 8192),
                observed_at REAL NOT NULL, updated_at REAL NOT NULL,
                pinned INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(boot_id, pid, generation)
            )""",
            "CREATE INDEX IF NOT EXISTS process_nodes_session ON process_nodes(boot_id,audit_session,updated_at)",
            "CREATE INDEX IF NOT EXISTS process_nodes_updated ON process_nodes(pinned,updated_at)",
            """CREATE TABLE IF NOT EXISTS origin_sessions (
                boot_id TEXT NOT NULL, audit_session INTEGER NOT NULL,
                origin_kind TEXT NOT NULL, actor TEXT NOT NULL DEFAULT '',
                tty TEXT NOT NULL DEFAULT '', source_ip TEXT NOT NULL DEFAULT '',
                anchor_pid INTEGER, anchor_generation TEXT NOT NULL DEFAULT '',
                observed_at REAL NOT NULL, updated_at REAL NOT NULL,
                ambiguous INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(boot_id,audit_session)
            )""",
            "CREATE INDEX IF NOT EXISTS origin_sessions_updated ON origin_sessions(updated_at)",
            """CREATE TABLE IF NOT EXISTS crond_launches (
                boot_id TEXT NOT NULL, audit_session INTEGER NOT NULL,
                payload TEXT NOT NULL CHECK(length(payload) <= 4096),
                observed_at REAL NOT NULL, updated_at REAL NOT NULL,
                ambiguous INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(boot_id,audit_session)
            )""",
            "CREATE INDEX IF NOT EXISTS crond_launches_updated ON crond_launches(updated_at)",
            """CREATE TABLE IF NOT EXISTS audit_health_epochs (
                boot_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                payload TEXT NOT NULL CHECK(length(payload) <= 4096),
                observed_at REAL NOT NULL, healthy INTEGER NOT NULL,
                PRIMARY KEY(boot_id, sequence)
            )""",
            "CREATE INDEX IF NOT EXISTS audit_health_observed ON audit_health_epochs(observed_at)",
            """CREATE TABLE IF NOT EXISTS pending_firewall (
                observation_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL CHECK(length(payload) <= 65536),
                operation_id TEXT NOT NULL DEFAULT '', created REAL NOT NULL,
                expires REAL NOT NULL, state TEXT NOT NULL DEFAULT 'pending'
            )""",
            "CREATE INDEX IF NOT EXISTS pending_firewall_due ON pending_firewall(state,expires)",
            "CREATE INDEX IF NOT EXISTS pending_firewall_operation ON pending_firewall(operation_id)",
            """CREATE TABLE IF NOT EXISTS firewall_receipts (
                operation_id TEXT NOT NULL, kind TEXT NOT NULL,
                payload TEXT NOT NULL CHECK(length(payload) <= 32768),
                observed_at REAL NOT NULL,
                PRIMARY KEY(operation_id, kind)
            )""",
            "CREATE INDEX IF NOT EXISTS firewall_receipts_observed ON firewall_receipts(observed_at)",
        )
        for statement in statements:
            self.db.execute(statement)

    def _ensure_v3_schema(self):
        """Repair compatibility columns and indexes transactionally."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
            if row is None or json.loads(row["value"]) != SCHEMA_VERSION:
                raise StoreError("state schema changed during compatibility check")
            self._create_ssh_session_schema()
            columns = {
                row["name"] for row in self.db.execute(
                    "PRAGMA table_info(ssh_sessions)").fetchall()
            }
            if "ambiguous" not in columns:
                self._add_ssh_session_ambiguous_column()
            event_columns = {
                row["name"] for row in self.db.execute(
                    "PRAGMA table_info(events)").fetchall()
            }
            if "delivery_class" not in event_columns:
                self._add_event_delivery_class()
            else:
                self.db.execute(
                    "CREATE INDEX IF NOT EXISTS events_delivery_class "
                    "ON events(delivery_class, next_attempt, created)")
                self.db.execute(
                    "CREATE INDEX IF NOT EXISTS events_delivery_class_created "
                    "ON events(delivery_class, created, id)")
            # Provenance is an additive private extension beneath the shared
            # v3 compatibility marker. Older v3 code ignores these tables.
            self._create_private_schema()
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def _migrate_v1_to_v3(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
            if row is None or json.loads(row["value"]) != 1:
                raise StoreError("state schema changed during migration")
            self._create_ssh_session_schema()
            self._add_event_delivery_class()
            # Version is deliberately last: rollback/retry can never advertise
            # v3 before every v3 object exists.
            self.set_meta("schema_version", SCHEMA_VERSION)
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def _migrate_v2_to_v3(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
            if row is None or json.loads(row["value"]) != 2:
                raise StoreError("state schema changed during migration")
            self._create_ssh_session_schema()
            columns = {
                row["name"] for row in self.db.execute(
                    "PRAGMA table_info(ssh_sessions)").fetchall()
            }
            if "ambiguous" not in columns:
                self._add_ssh_session_ambiguous_column()
            self._add_event_delivery_class()
            self.set_meta("schema_version", SCHEMA_VERSION)
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def close(self):
        self.db.close()

    def get_meta(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError as err:
            raise StoreError(f"corrupt state value: {key}") from err

    def set_meta(self, key, value):
        self.db.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, canonical_json(value)),
        )

    def _increment(self, key, amount=1):
        self.set_meta(key, int(self.get_meta(key, 0)) + amount)

    def _increment_saturating(self, key, amount=1):
        current = self.get_meta(key, 0)
        if not isinstance(current, int) or isinstance(current, bool) or current < 0:
            current = 0
        self.set_meta(key, min(SATURATING_COUNTER_MAX, current + max(0, amount)))

    @staticmethod
    def _payload_local_only(payload):
        try:
            return int(is_local_only(json.loads(payload), wire=True))
        except (TypeError, ValueError, json.JSONDecodeError):
            return 0

    def _record_local_only(self, found, diagnostic, now):
        classifier = classify_local_only(found, wire=False)
        if not classifier:
            raise StoreError("local-only record lacks a canonical classifier")
        self._increment_saturating("local_only_observed")
        self._increment_saturating(f"local_only:{classifier}:observed")
        seen = observed_timestamp(found)
        current = self.get_meta("local_only_last_seen")
        if seen is not None:
            try:
                current_time = datetime.datetime.fromisoformat(
                    current.replace("Z", "+00:00"))
            except (AttributeError, ValueError):
                current_time = None
            seen_time = datetime.datetime.fromisoformat(seen.replace("Z", "+00:00"))
            if current_time is None or seen_time > current_time:
                self.set_meta("local_only_last_seen", seen)
        if diagnostic["active"]:
            event = make_event(self.sensor_id, found)
            if self._enqueue(event, now=now, delivery_class="local_only_diagnostic"):
                self._increment_saturating("local_only_diagnostic_delivered")
                self._increment_saturating(
                    f"local_only:{classifier}:diagnostic_delivered")
        else:
            self._increment_saturating("local_only_suppressed")
            self._increment_saturating(f"local_only:{classifier}:suppressed")

    def _reconcile_local_only(self, diagnostic_active):
        suppressed = 0
        if not diagnostic_active:
            rows = self.db.execute(
                "SELECT id FROM events WHERE delivery_class = 'local_only_diagnostic' "
                "ORDER BY created, id LIMIT ?", (LOCAL_ONLY_RECONCILE_LIMIT,)
            ).fetchall()
            if rows:
                self.db.executemany(
                    "DELETE FROM events WHERE id = ?", ((row["id"],) for row in rows))
                suppressed += len(rows)
        legacy = self.db.execute(
            "SELECT id, payload FROM events WHERE delivery_class = 'legacy' "
            "ORDER BY created, id LIMIT ?", (LOCAL_ONLY_RECONCILE_LIMIT,)
        ).fetchall()
        ordinary = []
        diagnostic = []
        stale = []
        for row in legacy:
            if self._payload_local_only(row["payload"]):
                if diagnostic_active:
                    diagnostic.append((row["id"],))
                else:
                    stale.append((row["id"],))
            else:
                ordinary.append((row["id"],))
        if ordinary:
            self.db.executemany(
                "UPDATE events SET delivery_class = 'ordinary' WHERE id = ?", ordinary)
        if diagnostic:
            self.db.executemany(
                "UPDATE events SET delivery_class = 'local_only_diagnostic' WHERE id = ?",
                diagnostic)
        if stale:
            self.db.executemany("DELETE FROM events WHERE id = ?", stale)
            suppressed += len(stale)
        if suppressed:
            self._increment_saturating("local_only_suppressed", suppressed)
        return suppressed

    def load_ssh_sessions(self, now=None):
        cutoff = (now if now is not None else time.time()) - SSH_SESSION_TTL_SECONDS
        rows = self.db.execute(
            "SELECT boot_id, audit_session, actor, tty, source_ip, observed_at, ambiguous "
            "FROM ssh_sessions WHERE observed_at >= ? ORDER BY observed_at DESC LIMIT ?",
            (cutoff, SSH_SESSION_CAP),
        ).fetchall()
        return [dict(row) for row in rows]

    def _record_ssh_sessions(self, sessions, now):
        cutoff = now - SSH_SESSION_TTL_SECONDS
        self.db.execute("DELETE FROM ssh_sessions WHERE observed_at < ?", (cutoff,))
        for session in sessions or ():
            if float(session["observed_at"]) < cutoff:
                continue
            existing = self.db.execute(
                "SELECT boot_id, audit_session, actor, tty, source_ip, observed_at, ambiguous "
                "FROM ssh_sessions WHERE boot_id = ? AND audit_session = ?",
                (session["boot_id"], session["audit_session"]),
            ).fetchone()
            session = merge_session(dict(existing) if existing is not None else None, session)
            self.db.execute(
                "INSERT INTO ssh_sessions(boot_id, audit_session, actor, tty, source_ip, "
                "observed_at, updated_at, ambiguous) VALUES(?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(boot_id, audit_session) DO UPDATE SET "
                "actor=excluded.actor, tty=excluded.tty, source_ip=excluded.source_ip, "
                "observed_at=excluded.observed_at, updated_at=excluded.updated_at, "
                "ambiguous=excluded.ambiguous",
                (session["boot_id"], session["audit_session"], session["actor"],
                 session.get("tty", ""), session["source_ip"],
                 float(session["observed_at"]), now, int(bool(session.get("ambiguous")))),
            )
            self.db.execute(
                "INSERT INTO origin_sessions(boot_id,audit_session,origin_kind,actor,tty,"
                "source_ip,observed_at,updated_at,ambiguous) VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(boot_id,audit_session) DO UPDATE SET "
                "origin_kind=excluded.origin_kind,actor=excluded.actor,tty=excluded.tty,"
                "source_ip=excluded.source_ip,observed_at=excluded.observed_at,"
                "updated_at=excluded.updated_at,ambiguous=excluded.ambiguous",
                (session["boot_id"], session["audit_session"], "ssh", session["actor"],
                 session.get("tty", ""), session["source_ip"],
                 float(session["observed_at"]), now,
                 int(bool(session.get("ambiguous")))))
        self.db.execute(
            "DELETE FROM ssh_sessions WHERE rowid IN ("
            "SELECT rowid FROM ssh_sessions ORDER BY observed_at DESC, rowid DESC "
            "LIMIT -1 OFFSET ?)", (SSH_SESSION_CAP,))
        self.db.execute("DELETE FROM origin_sessions WHERE updated_at < ?",
                        (now - SSH_SESSION_TTL_SECONDS,))
        self.db.execute(
            "DELETE FROM origin_sessions WHERE rowid IN (SELECT rowid FROM origin_sessions "
            "ORDER BY updated_at DESC,rowid DESC LIMIT -1 OFFSET ?)", (SSH_SESSION_CAP,))

    def load_audit_fragments(self, now=None):
        cutoff = (time.time() if now is None else now) - AUDIT_FRAGMENT_TTL_SECONDS
        rows = self.db.execute(
            "SELECT boot_id,audit_id,payload FROM audit_fragments "
            "WHERE updated_at >= ? ORDER BY updated_at DESC LIMIT ?",
            (cutoff, AUDIT_FRAGMENT_CAP)).fetchall()
        result = {}
        for row in rows:
            try:
                result[(row["boot_id"], row["audit_id"])] = json.loads(row["payload"])
            except json.JSONDecodeError:
                continue
        return result

    def _record_provenance(self, fragments, process_nodes, health, receipts,
                           crond_launches, now):
        self.db.execute("DELETE FROM audit_fragments WHERE updated_at < ?",
                        (now - AUDIT_FRAGMENT_TTL_SECONDS,))
        for item in fragments or ():
            payload = canonical_json(item)
            if len(payload.encode()) > 32768:
                continue
            self.db.execute(
                "INSERT INTO audit_fragments(boot_id,audit_id,payload,observed_at,updated_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(boot_id,audit_id) DO UPDATE SET "
                "payload=excluded.payload,updated_at=excluded.updated_at",
                (item["boot_id"], item["audit_id"], payload, now, now))
        self.db.execute(
            "DELETE FROM audit_fragments WHERE rowid IN (SELECT rowid FROM audit_fragments "
            "ORDER BY updated_at DESC,rowid DESC LIMIT -1 OFFSET ?)",
            (AUDIT_FRAGMENT_CAP,))
        self._prune_payload_budget(
            "audit_fragments", "updated_at", AUDIT_FRAGMENT_MAX_BYTES)

        self.db.execute(
            "DELETE FROM process_nodes WHERE pinned=0 AND updated_at < ?",
            (now - PROCESS_NODE_TTL_SECONDS,))
        self._refresh_process_pins(now)
        for item in process_nodes or ():
            payload = canonical_json(item)
            if len(payload.encode()) > 8192:
                continue
            generation = str(item.get("start_ticks") or
                             f"audit-{item.get('audit_id', '')}")[:128]
            self.db.execute(
                "INSERT INTO process_nodes(boot_id,pid,generation,audit_session,payload,"
                "observed_at,updated_at,pinned) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(boot_id,pid,generation) DO UPDATE SET "
                "payload=excluded.payload,audit_session=excluded.audit_session,"
                "updated_at=excluded.updated_at,pinned=max(process_nodes.pinned,excluded.pinned)",
                (item["boot_id"], item["pid"], generation,
                 item.get("audit_session"), payload, now, now,
                 int(bool(item.get("pinned")))))
        self._record_crond_launches(crond_launches, now)
        self._record_jobman_origins(now)
        self.db.execute(
            "DELETE FROM process_nodes WHERE rowid IN (SELECT rowid FROM process_nodes "
            "WHERE pinned=0 ORDER BY updated_at DESC,rowid DESC LIMIT -1 OFFSET ?)",
            (PROCESS_NODE_CAP,))
        self.db.execute(
            "UPDATE process_nodes SET pinned=0 WHERE rowid IN (SELECT rowid FROM "
            "process_nodes WHERE pinned=1 ORDER BY updated_at DESC,rowid DESC "
            "LIMIT -1 OFFSET ?)", (PROCESS_PINNED_CAP,))
        self._prune_payload_budget(
            "process_nodes", "updated_at", PROCESS_NODE_MAX_BYTES,
            where="pinned=0")

        if health is not None:
            payload = canonical_json(health)
            if len(payload.encode()) <= 4096:
                self.db.execute(
                    "INSERT OR REPLACE INTO audit_health_epochs(boot_id,sequence,payload,"
                    "observed_at,healthy) VALUES(?,?,?,?,?)",
                    (health["boot_id"], health["sequence"], payload, now,
                     int(bool(health.get("healthy")))))
            self.db.execute(
                "DELETE FROM audit_health_epochs WHERE rowid IN (SELECT rowid FROM "
                "audit_health_epochs ORDER BY observed_at DESC,rowid DESC LIMIT -1 OFFSET ?)",
                (HEALTH_EPOCH_CAP,))
            self.set_meta("audit_health", select_health_fields(health))

        self.db.execute("DELETE FROM firewall_receipts WHERE observed_at < ?",
                        (now - FIREWALL_RECEIPT_TTL_SECONDS,))
        for item in receipts or ():
            payload = canonical_json(item)
            if len(payload.encode()) > 32768:
                continue
            self.db.execute(
                "INSERT OR REPLACE INTO firewall_receipts(operation_id,kind,payload,observed_at) "
                "VALUES(?,?,?,?)", (item["operation_id"], item["kind"], payload, now))
        self.db.execute(
            "DELETE FROM firewall_receipts WHERE rowid IN (SELECT rowid FROM firewall_receipts "
            "ORDER BY observed_at DESC,rowid DESC LIMIT -1 OFFSET ?)",
            (FIREWALL_RECEIPT_CAP,))
        self._prune_payload_budget(
            "firewall_receipts", "observed_at", FIREWALL_RECEIPT_MAX_BYTES)

    def _record_crond_launches(self, launches, now):
        """Merge trusted CROND syslog and PAM halves with sticky conflict."""
        self.db.execute("DELETE FROM crond_launches WHERE updated_at < ?",
                        (now - SSH_SESSION_TTL_SECONDS,))
        for launch in launches or ():
            key = (launch.get("boot_id"), launch.get("audit_session"))
            half = launch.get("half")
            if half not in ("syslog", "pam"):
                continue
            row = self.db.execute(
                "SELECT payload,ambiguous FROM crond_launches "
                "WHERE boot_id=? AND audit_session=?", key).fetchone()
            payload = {} if row is None else json.loads(row["payload"])
            conflict = bool(row and row["ambiguous"])
            prior = payload.get(half)
            clean = {name: launch[name] for name in (
                "boot_id", "audit_session", "half", "monotonic", "command_sha256")}
            if half == "syslog":
                clean["launch_pid"] = launch["launch_pid"]
            if prior is not None and prior != clean:
                conflict = True
            payload[half] = clean
            encoded = canonical_json(payload)
            self.db.execute(
                "INSERT INTO crond_launches(boot_id,audit_session,payload,observed_at,"
                "updated_at,ambiguous) VALUES(?,?,?,?,?,?) ON CONFLICT(boot_id,audit_session) "
                "DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at,"
                "ambiguous=max(crond_launches.ambiguous,excluded.ambiguous)",
                (key[0], key[1], encoded, now, now, int(conflict)))
        self.db.execute(
            "DELETE FROM crond_launches WHERE rowid IN (SELECT rowid FROM crond_launches "
            "ORDER BY updated_at DESC,rowid DESC LIMIT -1 OFFSET ?)", (SSH_SESSION_CAP,))

    def _prune_payload_budget(self, table, order_column, maximum, where="1=1"):
        """Retain newest bounded payload bytes using one indexed window scan."""
        allowed = {
            ("audit_fragments", "updated_at", "1=1"),
            ("process_nodes", "updated_at", "pinned=0"),
            ("firewall_receipts", "observed_at", "1=1"),
        }
        if (table, order_column, where) not in allowed:
            raise StoreError("invalid provenance prune target")
        self.db.execute(
            f"DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM ("
            f"SELECT rowid,SUM(length(CAST(payload AS BLOB))) OVER ("
            f"ORDER BY {order_column} DESC,rowid DESC) AS retained "
            f"FROM {table} WHERE {where}) WHERE retained > ?)", (maximum,))

    def _refresh_process_pins(self, now):
        """Touch an anchor only while /proc proves the same PID generation."""
        from .lineage import enrich_process

        rows = self.db.execute(
            "SELECT rowid,pid,generation FROM process_nodes WHERE pinned=1 "
            "ORDER BY updated_at DESC LIMIT ?", (PROCESS_PINNED_CAP,)).fetchall()
        for row in rows:
            live = enrich_process(row["pid"])
            payload = self.db.execute(
                "SELECT payload FROM process_nodes WHERE rowid=?", (row["rowid"],)).fetchone()
            node = json.loads(payload["payload"]) if payload is not None else {}
            if (self._eligible_process_node(node) and live is not None and
                    str(live["start_ticks"]) == row["generation"]):
                self.db.execute(
                    "UPDATE process_nodes SET updated_at=? WHERE rowid=?", (now, row["rowid"]))
            else:
                self.db.execute(
                    "UPDATE process_nodes SET pinned=0 WHERE rowid=?", (row["rowid"],))

    def _record_jobman_origins(self, now):
        anchors = self.db.execute(
            "SELECT boot_id,audit_session,pid,generation,payload FROM process_nodes "
            "WHERE pinned=1 AND audit_session IS NOT NULL ORDER BY updated_at DESC LIMIT ?",
            (PROCESS_PINNED_CAP,)).fetchall()
        node_rows = self.db.execute(
            "WITH anchors AS (SELECT boot_id,audit_session FROM process_nodes "
            "WHERE pinned=1 AND audit_session IS NOT NULL ORDER BY updated_at DESC LIMIT ?) "
            "SELECT p.boot_id,p.audit_session,p.payload FROM process_nodes p JOIN anchors a "
            "ON a.boot_id=p.boot_id AND a.audit_session=p.audit_session "
            "ORDER BY p.updated_at DESC LIMIT ?",
            (PROCESS_PINNED_CAP, PROCESS_PINNED_CAP * 256)).fetchall()
        session_nodes = {}
        for row in node_rows:
            session_nodes.setdefault(
                (row["boot_id"], row["audit_session"]), []).append(
                    json.loads(row["payload"]))
        origins = {
            (row["boot_id"], row["audit_session"]): row
            for row in self.db.execute(
                "SELECT boot_id,audit_session,origin_kind,anchor_pid,anchor_generation "
                "FROM origin_sessions ORDER BY updated_at DESC LIMIT ?",
                (SSH_SESSION_CAP,)).fetchall()
        }
        for anchor in anchors:
            key = (anchor["boot_id"], anchor["audit_session"])
            launch_row = self.db.execute(
                "SELECT payload,ambiguous FROM crond_launches WHERE boot_id=? "
                "AND audit_session=?", key).fetchone()
            if launch_row is not None and launch_row["ambiguous"]:
                self.db.execute(
                    "UPDATE origin_sessions SET ambiguous=1,updated_at=? "
                    "WHERE boot_id=? AND audit_session=?", (now, key[0], key[1]))
                continue
            if launch_row is None:
                continue
            launch = json.loads(launch_row["payload"])
            syslog = launch.get("syslog")
            pam = launch.get("pam")
            if (not syslog or not pam or
                    syslog.get("command_sha256") != pam.get("command_sha256") or
                    pam.get("monotonic", 0) >= syslog.get("monotonic", 0)):
                continue
            nodes = session_nodes.get(key, [])
            by_pid = {}
            for node in nodes:
                by_pid.setdefault(node.get("pid"), []).append(node)
            anchor_node = json.loads(anchor["payload"])
            if not self._eligible_process_node(anchor_node):
                continue
            launch_pid = syslog.get("launch_pid")
            jobman = [node for node in nodes if self._eligible_process_node(node) and
                      node.get("pid") == anchor_node.get("ppid") and
                      "start" in [str(part) for part in node.get("argv", [])] and
                      any(str(part).endswith("/bin/jobman") or
                          part == "mojo.deploy.jobman" for part in node.get("argv", []))]
            if len(jobman) != 1:
                continue
            jobman_node = jobman[0]
            bash = [node for node in nodes if self._eligible_process_node(node) and
                    node.get("exe") == "/usr/bin/bash" and
                    (node.get("pid") == launch_pid == jobman_node.get("pid") or
                     node.get("pid") == launch_pid == jobman_node.get("ppid"))]
            if len(bash) != 1:
                continue
            bash_node = bash[0]
            if not (jobman_node.get("pid") == bash_node.get("pid") or
                    jobman_node.get("ppid") == bash_node.get("pid")):
                continue
            order = (pam["monotonic"], syslog["monotonic"],
                     bash_node.get("monotonic"), jobman_node.get("monotonic"),
                     anchor_node.get("monotonic"))
            if (any(not isinstance(value, int) for value in order) or
                    any(left >= right for left, right in zip(order, order[1:]))):
                continue
            existing = origins.get(key)
            conflict = bool(existing and (
                existing["origin_kind"] != "cron_jobman" or
                existing["anchor_pid"] not in (None, anchor["pid"]) or
                existing["anchor_generation"] not in ("", anchor["generation"])))
            if existing is None:
                self.db.execute(
                    "INSERT INTO origin_sessions(boot_id,audit_session,origin_kind,actor,"
                    "anchor_pid,anchor_generation,observed_at,updated_at,ambiguous) "
                    "VALUES(?,?,?,?,?,?,?,?,0)",
                    (anchor["boot_id"], anchor["audit_session"], "cron_jobman", "",
                     anchor["pid"], anchor["generation"], now, now))
            elif conflict:
                self.db.execute(
                    "UPDATE origin_sessions SET ambiguous=1,updated_at=? "
                    "WHERE boot_id=? AND audit_session=?",
                    (now, anchor["boot_id"], anchor["audit_session"]))
            else:
                self.db.execute(
                    "UPDATE origin_sessions SET anchor_pid=?,anchor_generation=?,updated_at=? "
                    "WHERE boot_id=? AND audit_session=?",
                    (anchor["pid"], anchor["generation"], now,
                     anchor["boot_id"], anchor["audit_session"]))

    def _record_local_origin(self, observation, now):
        attributes = observation.get("attributes", {})
        if (observation.get("kind") != "auth.session_open" or
                attributes.get("service") != "systemd-user" or
                attributes.get("source_ip") or attributes.get("tty") or
                attributes.get("attribution_provenance") != "none" or
                not attributes.get("boot_id") or
                not isinstance(attributes.get("audit_session"), int)):
            return
        key = (attributes["boot_id"], attributes["audit_session"])
        current = self.db.execute(
            "SELECT origin_kind,actor,ambiguous FROM origin_sessions "
            "WHERE boot_id=? AND audit_session=?", key).fetchone()
        actor = str(attributes.get("target_user") or "")[:128]
        if current is None:
            self.db.execute(
                "INSERT INTO origin_sessions(boot_id,audit_session,origin_kind,actor,"
                "observed_at,updated_at,ambiguous) VALUES(?,?,?,?,?,?,0)",
                (key[0], key[1], "local_systemd_user", actor, now, now))
        elif current["origin_kind"] != "local_systemd_user" or current["actor"] != actor:
            self.db.execute(
                "UPDATE origin_sessions SET ambiguous=1,updated_at=? "
                "WHERE boot_id=? AND audit_session=?", (now, key[0], key[1]))
        else:
            self.db.execute(
                "UPDATE origin_sessions SET updated_at=? WHERE boot_id=? AND audit_session=?",
                (now, key[0], key[1]))

    def _enrich_sudo(self, observation, now):
        if observation.get("kind") != "auth.sudo_command":
            return
        attributes = observation.get("attributes", {})
        boot = attributes.get("boot_id")
        session = attributes.get("audit_session")
        producer = attributes.get("producer_pid")
        if not boot or not isinstance(session, int):
            attributes["proof_status"] = "ineligible"
            return
        rows = self.db.execute(
            "SELECT payload FROM process_nodes WHERE boot_id=? AND audit_session=? "
            "ORDER BY updated_at DESC LIMIT 256", (boot, session)).fetchall()
        nodes = [json.loads(row["payload"]) for row in rows]
        by_pid = {}
        for node in nodes:
            by_pid.setdefault(node.get("pid"), []).append(node)
        lineage = []
        current_pid = producer
        seen = set()
        conflict = False
        for unused in range(8):
            if not isinstance(current_pid, int) or current_pid <= 0 or current_pid in seen:
                conflict = current_pid in seen
                break
            seen.add(current_pid)
            found = by_pid.get(current_pid, [])
            eligible = [node for node in found if self._eligible_process_node(node)]
            if len(eligible) != 1:
                conflict = len(found) > 1 or bool(found and not eligible)
                break
            node = eligible[0]
            lineage.append(node)
            conflict = conflict or bool(node.get("ambiguous") or node.get("incomplete"))
            current_pid = node.get("ppid")
        if lineage:
            from .lineage import project_ancestors
            attributes["lineage"] = project_ancestors(lineage)
            attributes["lineage_sha256"] = hashlib.sha256(
                canonical_json(lineage).encode()).hexdigest()
        origin = self.db.execute(
            "SELECT origin_kind,ambiguous FROM origin_sessions "
            "WHERE boot_id=? AND audit_session=?", (boot, session)).fetchone()
        if origin is not None:
            attributes["origin_kind"] = origin["origin_kind"]
            conflict = conflict or bool(origin["ambiguous"])
        receipt_rows = self.db.execute(
            "SELECT payload FROM firewall_receipts WHERE observed_at>=? "
            "ORDER BY observed_at DESC LIMIT 64", (now - 30,)).fetchall()
        semantics = []
        for row in receipt_rows:
            receipt = json.loads(row["payload"])
            if (receipt.get("boot_id") == boot and receipt.get("audit_session") == session and
                    receipt.get("semantic") not in semantics):
                semantics.append(receipt["semantic"])
        if semantics:
            attributes["receipt_semantics"] = semantics[:8]
        attributes["proof_status"] = (
            "conflict" if conflict else "partial" if lineage or semantics else "missing")

    def hold_firewall_observation(self, observation, operation_id="", now=None):
        now = time.time() if now is None else now
        payload = canonical_json(observation)
        if len(payload.encode()) > 65536:
            raise StoreError("pending firewall observation is too large")
        self.db.execute(
            "INSERT OR IGNORE INTO pending_firewall(observation_id,payload,operation_id,"
            "created,expires,state) VALUES(?,?,?,?,?,'pending')",
            (observation["fingerprint"], payload, str(operation_id)[:64], now, now + 30))
        rows = self.db.execute(
            "SELECT observation_id,payload FROM pending_firewall WHERE state='pending' "
            "ORDER BY created,rowid LIMIT -1 OFFSET ?", (PENDING_OPERATION_CAP,)).fetchall()
        for row in rows:
            # Capacity pressure is fail-open: enqueue the untouched original.
            self._ingest_one(json.loads(row["payload"]), now)
            self._increment_saturating("provenance_pending_cap_flush")
            self.db.execute("DELETE FROM pending_firewall WHERE observation_id=?",
                            (row["observation_id"],))
        while True:
            used = self.db.execute(
                "SELECT COALESCE(SUM(length(CAST(payload AS BLOB))),0) AS used "
                "FROM pending_firewall WHERE state='pending'").fetchone()["used"]
            if used <= PENDING_OPERATION_MAX_BYTES:
                break
            rows = self.db.execute(
                "SELECT observation_id,payload FROM pending_firewall WHERE state='pending' "
                "ORDER BY created,rowid LIMIT 128").fetchall()
            if not rows:
                break
            for row in rows:
                self._ingest_one(json.loads(row["payload"]), now)
                self._increment_saturating("provenance_pending_cap_flush")
                self.db.execute("DELETE FROM pending_firewall WHERE observation_id=?",
                                (row["observation_id"],))

    def _flush_expired_firewall(self, now):
        rows = self.db.execute(
            "SELECT observation_id,payload FROM pending_firewall "
            "WHERE state='pending' AND expires <= ? ORDER BY expires LIMIT 512",
            (now,)).fetchall()
        for row in rows:
            self._ingest_one(json.loads(row["payload"]), now)
            self._increment_saturating("provenance_pending_expired")
            self.db.execute("DELETE FROM pending_firewall WHERE observation_id=?",
                            (row["observation_id"],))

    @staticmethod
    def _broker_candidate(observation):
        attributes = observation.get("attributes", {})
        return bool(
            observation.get("kind") == "auth.sudo_command" and
            attributes.get("command") == "/usr/local/sbin/mojo-firewall-broker" and
            attributes.get("attribution_provenance") == "none" and
            not attributes.get("source_ip") and not attributes.get("tty") and
            attributes.get("boot_id") and
            isinstance(attributes.get("audit_session"), int) and
            isinstance(attributes.get("producer_pid"), int) and
            isinstance(attributes.get("monotonic"), int))

    @staticmethod
    def _receipt_pair_valid(begin, result):
        exact = (
            "operation_id", "execution_id", "job_id", "function", "operation",
            "semantic", "argv_digest", "stdin_digest", "stdin_length", "count",
            "broker_pid", "broker_start_ticks", "target_exe", "boot_id",
            "audit_session",
        )
        return bool(
            begin and result and result.get("ok") is True and
            all(begin.get(key) == result.get(key) for key in exact) and
            begin.get("operation") in _BROKER_FUNCTION_OPERATIONS.get(
                begin.get("function"), set()) and
            isinstance(begin.get("monotonic_ns"), int) and
            isinstance(result.get("monotonic_ns"), int) and
            result["monotonic_ns"] >= begin["monotonic_ns"] and
            isinstance(result.get("target_pid"), int) and result["target_pid"] > 0 and
            isinstance(result.get("target_start_ticks"), int) and
            result["target_start_ticks"] > 0 and
            begin.get("children") == [] and
            isinstance(result.get("children"), list) and
            1 <= len(result["children"]) <= 8 and
            all(child.get("ok") is True for child in result["children"]))

    @staticmethod
    def _eligible_process_node(node):
        argv = node.get("argv")
        return bool(
            isinstance(node, dict) and node.get("success") is True and
            node.get("eoe") is True and not node.get("ambiguous") and
            not node.get("incomplete") and isinstance(argv, list) and
            1 <= len(argv) <= 64 and all(isinstance(part, str) for part in argv) and
            sum(len(part.encode("utf-8", errors="replace")) for part in argv) <= 16384)

    @staticmethod
    def _one_pid_generation(nodes, pid, start_ticks, earliest, latest, exe=None):
        found = []
        for node in nodes:
            if node.get("pid") != pid or not Store._eligible_process_node(node):
                continue
            if exe is not None and node.get("exe") != exe:
                continue
            node_ticks = node.get("start_ticks")
            if node_ticks is not None and node_ticks != start_ticks:
                continue
            monotonic = node.get("monotonic")
            if node_ticks is None and not (
                    isinstance(monotonic, int) and
                    earliest - 1_000_000_000 <= monotonic * 1000 <= latest + 1_000_000_000):
                continue
            found.append(node)
        # More than one Audit exec generation for a PID in the proof window is
        # a reuse/order ambiguity, never a reason to guess.
        return found[0] if len(found) == 1 else None

    @staticmethod
    def _parent_node(nodes, child):
        found = [node for node in nodes
                 if node.get("pid") == child.get("ppid") and
                 Store._eligible_process_node(node)]
        if len(found) == 1:
            return found[0]
        # Several exec generations of one parent PID are safe only when one is
        # the still-live, pinned engine anchor.
        pinned = [node for node in found if node.get("pinned")]
        return pinned[0] if len(pinned) == 1 else None

    def _resolve_pending_firewall(self, now, current_health=True):
        candidates = self.db.execute(
            "SELECT observation_id,payload FROM pending_firewall WHERE state='pending' "
            "ORDER BY created LIMIT 512").fetchall()
        for row in candidates:
            observation = json.loads(row["payload"])
            attributes = observation["attributes"]
            if current_health is None:
                continue
            if current_health is False:
                self._ingest_one(observation, now)
                self._increment_saturating("provenance_health_fail_open")
                self.db.execute("DELETE FROM pending_firewall WHERE observation_id=?",
                                (row["observation_id"],))
                continue
            health = self.db.execute(
                "SELECT payload,healthy FROM audit_health_epochs WHERE boot_id=? "
                "ORDER BY sequence DESC LIMIT 1", (attributes["boot_id"],)).fetchone()
            if health is None or not health["healthy"]:
                self._ingest_one(observation, now)
                self._increment_saturating("provenance_health_fail_open")
                self.db.execute("DELETE FROM pending_firewall WHERE observation_id=?",
                                (row["observation_id"],))
                continue
            receipts = self.db.execute(
                "SELECT operation_id,kind,payload FROM firewall_receipts "
                "WHERE observed_at >= ? ORDER BY observed_at DESC LIMIT 1024",
                (now - 30,)).fetchall()
            pairs = {}
            for receipt in receipts:
                pairs.setdefault(receipt["operation_id"], {})[receipt["kind"]] = json.loads(
                    receipt["payload"])
            nodes = self.db.execute(
                "SELECT payload FROM process_nodes WHERE boot_id=? AND audit_session=? "
                "ORDER BY updated_at DESC LIMIT 256",
                (attributes["boot_id"], attributes["audit_session"])).fetchall()
            nodes = [json.loads(node["payload"]) for node in nodes]
            origin = self.db.execute(
                "SELECT origin_kind,anchor_pid,anchor_generation,ambiguous "
                "FROM origin_sessions WHERE boot_id=? AND audit_session=?",
                (attributes["boot_id"], attributes["audit_session"])).fetchone()
            if origin is not None and origin["ambiguous"]:
                self._ingest_one(observation, now)
                self.db.execute("DELETE FROM pending_firewall WHERE observation_id=?",
                                (row["observation_id"],))
                continue
            if origin is None or origin["origin_kind"] != "cron_jobman":
                continue
            conflicted = False
            for operation_id, pair in pairs.items():
                begin = pair.get("begin")
                result = pair.get("result")
                if begin and result and not self._receipt_pair_valid(begin, result):
                    conflicted = True
                    continue
                if (not self._receipt_pair_valid(begin, result) or
                        begin.get("boot_id") != attributes["boot_id"] or
                        result.get("boot_id") != attributes["boot_id"] or
                        begin.get("audit_session") != attributes["audit_session"] or
                        result.get("audit_session") != attributes["audit_session"]):
                    continue
                broker = self._one_pid_generation(
                    nodes, begin["broker_pid"], begin["broker_start_ticks"],
                    begin["monotonic_ns"], result["monotonic_ns"])
                targets = []
                for child in result["children"]:
                    target = self._one_pid_generation(
                        nodes, child["pid"], child["start_ticks"],
                        begin["monotonic_ns"], result["monotonic_ns"], exe=child["exe"])
                    if target is None:
                        targets = []
                        break
                    if (target.get("ppid") != begin["broker_pid"] or
                            target.get("argv_sha256") != child["argv_digest"]):
                        conflicted = True
                        targets = []
                        break
                    targets.append(target)
                if broker is None or len(targets) != len(result["children"]):
                    if any(node.get("ambiguous") or node.get("incomplete") for node in nodes
                           if node.get("pid") in (
                               begin["broker_pid"], result["target_pid"])):
                        conflicted = True
                    continue
                sudo = self._parent_node(nodes, broker)
                observed_ns = attributes["monotonic"] * 1000
                if (sudo is None or sudo.get("exe") != "/usr/bin/sudo" or
                        sudo.get("pid") != attributes["producer_pid"] or
                        not begin["monotonic_ns"] - 2_000_000_000 <= observed_ns <=
                        result["monotonic_ns"] + 2_000_000_000):
                    continue
                engine = self._parent_node(nodes, sudo)
                if (engine is None or not engine.get("pinned") or
                        engine.get("pid") != origin["anchor_pid"] or
                        str(engine.get("start_ticks") or
                            f"audit-{engine.get('audit_id', '')}") !=
                        origin["anchor_generation"] or
                        not any("bin/jobs.py" in str(part) for part in engine.get("argv", [])) or
                        "engine" not in engine.get("argv", []) or
                        "foreground" not in engine.get("argv", [])):
                    continue
                from .lineage import project_ancestors
                attributes.update({
                    "local_disposition": JOBMAN_FIREWALL_CLASSIFIER,
                    "operation_id": operation_id,
                    "execution_id": begin.get("execution_id"),
                    "job_id": begin.get("job_id"),
                    "job_function": begin.get("function"),
                    "origin_kind": "cron_jobman",
                    "proof_status": "proven",
                    "lineage": project_ancestors(targets + [broker, sudo, engine]),
                    "lineage_sha256": hashlib.sha256(canonical_json(
                        targets + [broker, sudo, engine]).encode()).hexdigest(),
                })
                observation["attributes"] = build_evidence(
                    observation["kind"], observation["attributes"])
                self._record_local_only(observation, self.local_only_diagnostic, now)
                self.db.execute("DELETE FROM pending_firewall WHERE observation_id=?",
                                (row["observation_id"],))
                break
            else:
                if conflicted:
                    observation["attributes"]["proof_status"] = "conflict"
                    self._ingest_one(observation, now)
                    self._increment_saturating("provenance_proof_conflict")
                    self.db.execute("DELETE FROM pending_firewall WHERE observation_id=?",
                                    (row["observation_id"],))

    def _enqueue(self, event, now=None, delivery_class="ordinary"):
        now = now if now is not None else time.time()
        if delivery_class not in ("ordinary", "local_only_diagnostic"):
            raise StoreError("event delivery class is invalid")
        if self.db.execute("SELECT 1 FROM events WHERE id = ?", (event["id"],)).fetchone():
            self._increment("deduped_events")
            return False
        current = self.db.execute("SELECT COUNT(*) AS count FROM events").fetchone()["count"]
        maximum = self.delivery_config["max_spool_events"]
        reserve = self.delivery_config["critical_reserve_events"]
        high_priority = event["severity"] in ("high", "critical")
        limit = maximum if high_priority else maximum - reserve
        if current >= limit:
            self._increment("dropped_capacity")
            self._increment(f"dropped_capacity_{event['severity']}")
            return False
        grace = (
            ANNOTATION_GRACE_SECONDS
            if event.get("kind") == "fim.change" and
            not event.get("attributes", {}).get("expected_change") else 0
        )
        cursor = self.db.execute(
            "INSERT OR IGNORE INTO events("
            "id, payload, severity, created, next_attempt, annotation_deadline, delivery_class) "
            "VALUES(?, ?, ?, ?, ?, ?, ?)",
            (event["id"], canonical_json(event), event["severity"], now,
             now + grace, now + grace if grace else 0, delivery_class),
        )
        return cursor.rowcount == 1

    def _aggregate_row(self, fingerprint):
        row = self.db.execute(
            "SELECT * FROM aggregates WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        if row is None:
            return None
        return {
            "fingerprint": row["fingerprint"],
            "observation": json.loads(row["payload"]),
            "severity": row["severity"],
            "count": row["count"],
            "first_seen": row["first_seen"],
            "last_seen": row["last_seen"],
            "flush_at": row["flush_at"],
        }

    def _save_aggregate(self, aggregate, now):
        flush_at = aggregate.get("flush_at")
        if flush_at is None:
            flush_at = now + self.aggregation_config["window_seconds"]
        self.db.execute(
            "INSERT INTO aggregates(fingerprint, payload, severity, count, first_seen, last_seen, flush_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?) ON CONFLICT(fingerprint) DO UPDATE SET "
            "payload=excluded.payload, severity=excluded.severity, count=excluded.count, "
            "last_seen=excluded.last_seen",
            (aggregate["fingerprint"], canonical_json(aggregate["observation"]),
             aggregate["observation"]["severity"], aggregate["count"],
             aggregate["first_seen"], aggregate["last_seen"], flush_at),
        )
        aggregate["flush_at"] = flush_at

    def _flush_aggregate(self, aggregate, now):
        event = make_event(
            self.sensor_id, aggregate["observation"], count=aggregate["count"],
            first_seen=aggregate["first_seen"], last_seen=aggregate["last_seen"],
        )
        self._enqueue(event, now=now)
        self.db.execute("DELETE FROM aggregates WHERE fingerprint = ?", (aggregate["fingerprint"],))

    def _ingest_one(self, observation, now, diagnostic=None):
        if observation.get("kind") == "auth.sudo_command":
            observation["attributes"] = build_evidence(
                observation["kind"], observation.get("attributes", {}))
        if is_local_only(observation, wire=False):
            self._record_local_only(
                observation, diagnostic or self.local_only_diagnostic, now)
            return
        if not observation.get("aggregate"):
            self._enqueue(make_event(self.sensor_id, observation), now=now)
            return
        current = self._aggregate_row(observation["fingerprint"])
        if current is None:
            count = self.db.execute("SELECT COUNT(*) AS count FROM aggregates").fetchone()["count"]
            maximum = self.aggregation_config["max_aggregates"]
            reserve = self.aggregation_config["critical_reserve_aggregates"]
            high_priority = observation["severity"] in ("high", "critical")
            limit = maximum if high_priority else maximum - reserve
            if count >= limit and high_priority:
                victim = self.db.execute(
                    "SELECT fingerprint FROM aggregates WHERE severity IN ('info', 'warning') "
                    "ORDER BY flush_at LIMIT 1"
                ).fetchone()
                if victim is not None:
                    aggregate = self._aggregate_row(victim["fingerprint"])
                    self._flush_aggregate(aggregate, now)
                    self._increment("aggregate_evicted_for_priority")
                    count -= 1
            if count >= limit:
                self._increment("dropped_aggregate_capacity")
                self._increment(f"dropped_aggregate_capacity_{observation['severity']}")
                return
        aggregate = merge(current, observation, self.aggregation_config["window_seconds"])
        self._save_aggregate(aggregate, now)
        if should_flush(aggregate, self.aggregation_config["flush_count"]):
            self._flush_aggregate(aggregate, now)

    def _flush_due(self, now):
        rows = self.db.execute(
            "SELECT fingerprint FROM aggregates WHERE flush_at <= ? ORDER BY flush_at LIMIT 1000",
            (now,),
        ).fetchall()
        for row in rows:
            aggregate = self._aggregate_row(row["fingerprint"])
            if aggregate and should_flush(aggregate, self.aggregation_config["flush_count"], due=True):
                self._flush_aggregate(aggregate, now)

    def ingest(self, observations, cursor_key=None, cursor=None, ssh_sessions=None,
               audit_fragments=None, process_nodes=None, audit_health=None,
               firewall_receipts=None, crond_launches=None):
        """Durably queue observations and advance their collector cursor atomically."""
        now = time.time()
        diagnostic = diagnostic_override(self.local_only_diagnostic_path, now=now)
        self.local_only_diagnostic = diagnostic
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if ssh_sessions is not None:
                self._record_ssh_sessions(ssh_sessions, now)
            self._record_provenance(
                audit_fragments, process_nodes, audit_health, firewall_receipts,
                crond_launches, now)
            for found in observations:
                self._record_local_origin(found, now)
                self._enrich_sudo(found, now)
                if found.get("kind") == "auth.sudo_command":
                    found["attributes"] = build_evidence(
                        found["kind"], found.get("attributes", {}))
                if self._broker_candidate(found):
                    boot_id = found["attributes"]["boot_id"]
                    healthy = self.db.execute(
                        "SELECT healthy FROM audit_health_epochs WHERE boot_id=? "
                        "ORDER BY sequence DESC LIMIT 1", (boot_id,)).fetchone()
                    if healthy is not None and healthy["healthy"]:
                        self.hold_firewall_observation(found, now=now)
                    else:
                        self._ingest_one(found, now, diagnostic=diagnostic)
                else:
                    self._ingest_one(found, now, diagnostic=diagnostic)
            self.db.execute(
                "DELETE FROM origin_sessions WHERE rowid IN (SELECT rowid FROM origin_sessions "
                "ORDER BY updated_at DESC,rowid DESC LIMIT -1 OFFSET ?)",
                (SSH_SESSION_CAP,))
            self._resolve_pending_firewall(
                now, current_health=(None if audit_health is None else
                                     bool(audit_health.get("healthy"))))
            self._flush_expired_firewall(now)
            self._flush_due(now)
            if cursor_key is not None:
                self.set_meta(f"cursor:{cursor_key}", cursor)
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def flush_due(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self._flush_due(time.time())
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def reconcile_pending_firewall(self, now=None):
        """Fail open expired proof candidates independently of journal health."""
        now = time.time() if now is None else now
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self._flush_expired_firewall(now)
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    @staticmethod
    def _event_grace(attributes):
        """This event's bounded hold: producer TTL plus its own tier window."""
        found = attributes.get("correlation_seconds")
        if (not isinstance(found, int) or isinstance(found, bool) or
                not 0 < found <= ANNOTATION_MAX_CORRELATION_SECONDS):
            return ANNOTATION_MAX_GRACE_SECONDS
        return ANNOTATION_MAX_OPERATION_SECONDS + found

    def annotate_pending_fim(self, expected_changes_path, active_paths=None, now=None,
                             directory_metadata_roots=()):
        """Enrich already-durable FIM evidence during its bounded delivery grace."""
        from .expected_changes import ExpectedChangeError, annotation, load_manifest

        try:
            entries = load_manifest(expected_changes_path)
        except ExpectedChangeError:
            entries = []
        now = now if now is not None else time.time()
        active_paths = set(active_paths or ())
        directory_metadata_roots = tuple(directory_metadata_roots or ())
        changed = 0
        self.db.execute("BEGIN IMMEDIATE")
        try:
            rows = self.db.execute(
                "SELECT id, payload, created, next_attempt, annotation_deadline "
                "FROM events WHERE annotation_deadline > 0 AND attempts = 0 "
                "AND last_error = '' ORDER BY created LIMIT 4096",
            ).fetchall()
            for row in rows:
                event = json.loads(row["payload"])
                if (event.get("kind") != "fim.change" or
                        event.get("attributes", {}).get("expected_change")):
                    continue
                attributes = event["attributes"]
                deadline = row["annotation_deadline"]
                grace = self._event_grace(attributes)
                if (attributes.get("path") in active_paths and
                        now < row["created"] + grace):
                    deadline = min(
                        row["created"] + grace,
                        max(deadline, now + ANNOTATION_GRACE_SECONDS),
                    )
                    if deadline > row["annotation_deadline"]:
                        self.db.execute(
                            "UPDATE events SET next_attempt = ?, annotation_deadline = ? "
                            "WHERE id = ?", (deadline, deadline, row["id"]))
                if deadline <= now or not entries:
                    continue
                evidence = {field: attributes.get(field) for field in (
                    "kind", "mode", "uid", "gid", "size", "mtime_ns", "ctime_ns",
                    "device", "inode", "sha256", "target_sha256")}
                value = annotation(
                    entries, attributes.get("path"), attributes.get("change"),
                    evidence if attributes.get("change") == "deleted" else None,
                    evidence if attributes.get("change") != "deleted" else None,
                    now=now, observed_at=row["created"],
                    correlation_seconds=attributes.get("correlation_seconds"),
                    directory_metadata_roots=directory_metadata_roots,
                )
                if value is None:
                    continue
                attributes["expected_change"] = value
                self.db.execute(
                    "UPDATE events SET payload = ?, next_attempt = ?, "
                    "annotation_deadline = 0 WHERE id = ?",
                    (canonical_json(event), now, row["id"]),
                )
                changed += 1
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        return changed

    def pending_batch(self, max_events, max_bytes):
        now = time.time()
        diagnostic_state = diagnostic_override(
            self.local_only_diagnostic_path, now=now)
        self.local_only_diagnostic = diagnostic_state
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self._reconcile_local_only(diagnostic_state["active"])
            ordinary = self.db.execute(
                "SELECT id, payload, created FROM events WHERE next_attempt <= ? "
                "AND delivery_class = 'ordinary' ORDER BY created, id LIMIT ?",
                (now, max_events),
            ).fetchall()
            diagnostic = []
            remaining = max_events - len(ordinary)
            if diagnostic_state["active"] and remaining > 0:
                diagnostic = self.db.execute(
                    "SELECT id, payload, created FROM events WHERE next_attempt <= ? "
                    "AND delivery_class = 'local_only_diagnostic' "
                    "ORDER BY created, id LIMIT ?", (now, remaining),
                ).fetchall()
            rows = [*ordinary, *diagnostic]
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        events = []
        used = 1024
        for row in rows:
            event = json.loads(row["payload"])
            if event.get("kind") == "fim.change":
                attributes = event.get("attributes", {})
                for field in (
                        "mtime_ns", "ctime_ns", "device", "inode",
                        "sha256", "target_sha256", "correlation_seconds"):
                    attributes.pop(field, None)
            payload = canonical_json(event)
            size = len(payload.encode("utf-8")) + 1
            if events and used + size > max_bytes:
                break
            events.append(event)
            used += size
        return events

    def mark_delivery(self, sent_ids, results, error=""):
        statuses = {result["id"]: result for result in results}
        now = time.time()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            for event_id in sent_ids:
                result = statuses.get(event_id, {"status": "retry", "reason": error or "missing ack"})
                status_value = result["status"]
                if status_value in ("accepted", "duplicate", "rejected"):
                    self.db.execute("DELETE FROM events WHERE id = ?", (event_id,))
                    self._increment(f"delivery_{status_value}")
                    continue
                row = self.db.execute("SELECT attempts FROM events WHERE id = ?", (event_id,)).fetchone()
                if row is None:
                    continue
                attempts = row["attempts"] + 1
                delay = min(
                    self.delivery_config["retry_max_seconds"],
                    self.delivery_config["retry_min_seconds"] * (2 ** min(attempts - 1, 16)),
                )
                reason = str(result.get("reason") or error or "retry")[:256]
                self.db.execute(
                    "UPDATE events SET attempts=?, next_attempt=?, annotation_deadline=0, "
                    "last_error=? WHERE id=?",
                    (attempts, now + delay, reason, event_id),
                )
                self._increment("delivery_retry")
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def fim_initialized(self, profile):
        return bool(self.get_meta(f"fim_initialized:{profile}", False))

    def load_fim_baseline(self, profile):
        rows = self.db.execute(
            "SELECT path, entry FROM fim_baseline WHERE profile = ?", (profile,)
        ).fetchall()
        return {row["path"]: json.loads(row["entry"]) for row in rows}

    def record_fim_scan(self, profile, snapshot, observations, complete):
        """Commit FIM evidence and its new complete baseline as one unit."""
        now = time.time()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            for found in observations:
                self._ingest_one(found, now)
            if complete:
                self.db.execute("DELETE FROM fim_baseline WHERE profile = ?", (profile,))
                self.db.executemany(
                    "INSERT INTO fim_baseline(profile, path, entry) VALUES(?, ?, ?)",
                    ((profile, path, canonical_json(entry)) for path, entry in snapshot.items()),
                )
                self.set_meta(f"fim_initialized:{profile}", True)
                self.set_meta(f"fim_scan:{profile}", {
                    "at": now, "complete": True, "entries": len(snapshot),
                })
            self._flush_due(now)
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def activate_fim_profile(self, identity, scans, reason="initialize"):
        """Atomically commit every complete tier and select one immutable profile."""
        if (not isinstance(identity, dict) or
                set(identity) != {"name", "version", "digest"}):
            raise StoreError("profile activation identity is invalid")
        if not isinstance(scans, dict) or not scans:
            raise StoreError("profile activation requires complete tier scans")
        for tier, scan in scans.items():
            if (not isinstance(tier, str) or not isinstance(scan, dict) or
                    scan.get("tier") != tier or scan.get("complete") is not True or
                    not isinstance(scan.get("snapshot"), dict)):
                raise StoreError("profile activation refuses an incomplete tier")
        now = time.time()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            for tier, scan in sorted(scans.items()):
                key = scan["baseline_key"]
                self.db.execute("DELETE FROM fim_baseline WHERE profile = ?", (key,))
                self.db.executemany(
                    "INSERT INTO fim_baseline(profile, path, entry) VALUES(?, ?, ?)",
                    ((key, path, canonical_json(entry))
                     for path, entry in scan["snapshot"].items()),
                )
                self.set_meta(f"fim_initialized:{key}", True)
                self.set_meta(f"fim_scan:{key}", {
                    "at": now, "complete": True,
                    "entries": len(scan["snapshot"]),
                    "duration": scan.get("duration", 0),
                    "bounds": scan.get("bounds", {}),
                })
            prior = self.get_meta("fim_active_profile")
            history = self.get_meta("fim_profile_history", [])
            if prior and prior != identity:
                history = [prior] + [item for item in history if item != prior]
                history = history[:8]
            self.set_meta("fim_profile_history", history)
            self.set_meta("fim_active_profile", identity)
            self.set_meta("fim_baseline_reason", str(reason)[:128])
            self.set_meta("fim_baseline_at", now)
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def initialize_fim_tier(self, identity, tier, scan, reason="re-enrollment"):
        """Seed exactly ONE tier's baseline, and only if it has never been seeded.

        Re-enrolling a node's content roots produces a new content baseline key
        with no baseline behind it. Without this the tier's first scan would
        diff a whole tenant estate against nothing and alarm on every file;
        with it, the new key is seeded silently and the host tiers are left
        completely alone.

        The refusal is the security property: seeding is only ever allowed to
        CREATE a baseline, never to overwrite one. An attacker who could
        re-seed an established tier could launder any change they had already
        made into the new "known good" state.
        """
        if (not isinstance(identity, dict) or
                set(identity) != {"name", "version", "digest"}):
            raise StoreError("tier initialization identity is invalid")
        if (not isinstance(tier, str) or not isinstance(scan, dict) or
                scan.get("tier") != tier or scan.get("complete") is not True or
                not isinstance(scan.get("snapshot"), dict) or
                not isinstance(scan.get("baseline_key"), str) or
                not scan["baseline_key"]):
            raise StoreError("tier initialization refuses an incomplete tier")
        if self.active_fim_profile() != identity:
            raise StoreError("tier initialization requires the active profile")
        key = scan["baseline_key"]
        if self.fim_initialized(key):
            raise StoreError(f"tier baseline is already initialized: {key}")
        superseded = [
            found for found in self.initialized_baseline_keys(f"{identity['name']}:")
            if found != key and found.split(":")[2:3] == [tier] and
            len(found.split(":")) > 3
        ]
        now = time.time()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute("DELETE FROM fim_baseline WHERE profile = ?", (key,))
            self.db.executemany(
                "INSERT INTO fim_baseline(profile, path, entry) VALUES(?, ?, ?)",
                ((key, path, canonical_json(entry))
                 for path, entry in scan["snapshot"].items()),
            )
            self.set_meta(f"fim_initialized:{key}", True)
            self.set_meta(f"fim_scan:{key}", {
                "at": now, "complete": True, "entries": len(scan["snapshot"]),
                "duration": scan.get("duration", 0),
                "bounds": scan.get("bounds", {}),
            })
            # The old root set is gone; its baseline can never be diffed again
            # and would otherwise retain a full copy of the retired estate.
            for stale in superseded:
                self.db.execute("DELETE FROM fim_baseline WHERE profile = ?", (stale,))
                self.db.execute("DELETE FROM meta WHERE key = ?",
                                (f"fim_initialized:{stale}",))
                self.db.execute("DELETE FROM meta WHERE key = ?", (f"fim_scan:{stale}",))
            self.set_meta(f"fim_tier_baseline_reason:{tier}", str(reason)[:128])
            self.set_meta(f"fim_tier_baseline_at:{tier}", now)
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        return {"baseline_key": key, "entries": len(scan["snapshot"]),
                "superseded": superseded}

    def active_fim_profile(self):
        return self.get_meta("fim_active_profile")

    def initialized_baseline_keys(self, prefix, limit=64):
        """Bounded scan of the baseline keys already seeded under one prefix."""
        pattern = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = self.db.execute(
            "SELECT key FROM meta WHERE key LIKE ? ESCAPE '\\' ORDER BY key LIMIT ?",
            (f"fim_initialized:{pattern}%", limit),
        ).fetchall()
        keys = [row["key"][len("fim_initialized:"):] for row in rows]
        return [key for key in keys if self.fim_initialized(key)]

    def rollback_fim_profile(self, digest):
        history = self.get_meta("fim_profile_history", [])
        identity = next((item for item in history if item.get("digest") == digest), None)
        if identity is None:
            raise StoreError("requested profile digest is not in intact rollback history")
        # Derive the tier set from what was actually seeded rather than from a
        # hard-coded fast/slow/rpm triple: a content profile carries a fourth
        # tier, and the literal would have rolled back to a profile whose
        # content baseline was never checked.
        keys = self.initialized_baseline_keys(f"{identity['name']}:{identity['digest']}:")
        tiers = {key.split(":")[2] for key in keys if len(key.split(":")) >= 3}
        if not {"fast", "slow", "rpm"} <= tiers:
            raise StoreError("requested rollback profile has an incomplete baseline")
        if not all(self.fim_initialized(key) for key in keys):
            raise StoreError("requested rollback profile has an incomplete baseline")
        current = self.active_fim_profile()
        now = time.time()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.set_meta("fim_active_profile", identity)
            self.set_meta("fim_profile_history", [current] + [
                item for item in history if item != identity and item != current
            ][:7])
            self.set_meta("fim_baseline_reason", "rollback")
            self.set_meta("fim_baseline_at", now)
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        return identity

    def fim_profile_health(self, identity, tier_keys):
        active = self.active_fim_profile()
        tiers = {}
        for tier, key in tier_keys.items():
            meta = self.get_meta(f"fim_scan:{key}", {})
            tiers[tier] = {
                "initialized": self.fim_initialized(key),
                "last_complete": meta.get("at"),
                "entries": meta.get("entries", 0),
                "duration": meta.get("duration", 0),
                "bounds": meta.get("bounds", {}),
            }
        return {
            "active": active == identity,
            "digest_drift": bool(active and active != identity),
            "identity": identity,
            "baseline_at": self.get_meta("fim_baseline_at"),
            "baseline_reason": self.get_meta("fim_baseline_reason", ""),
            "tiers": tiers,
        }

    def stats(self):
        events = self.db.execute("SELECT COUNT(*) AS count FROM events").fetchone()["count"]
        aggregates = self.db.execute("SELECT COUNT(*) AS count FROM aggregates").fetchone()["count"]
        process_nodes = self.db.execute(
            "SELECT COUNT(*) AS count FROM process_nodes").fetchone()["count"]
        engine_anchors = self.db.execute(
            "SELECT COUNT(*) AS count FROM process_nodes WHERE pinned=1").fetchone()["count"]
        pending_firewall = self.db.execute(
            "SELECT COUNT(*) AS count FROM pending_firewall WHERE state='pending'").fetchone()["count"]
        last_health = self.db.execute(
            "SELECT payload FROM audit_health_epochs ORDER BY observed_at DESC LIMIT 1").fetchone()
        return {
            "spooled_events": events,
            "pending_aggregates": aggregates,
            "deduped_events": int(self.get_meta("deduped_events", 0)),
            "dropped_capacity": int(self.get_meta("dropped_capacity", 0)),
            "dropped_aggregate_capacity": int(self.get_meta("dropped_aggregate_capacity", 0)),
            "aggregate_evicted_for_priority": int(self.get_meta("aggregate_evicted_for_priority", 0)),
            "delivery_accepted": int(self.get_meta("delivery_accepted", 0)),
            "delivery_duplicate": int(self.get_meta("delivery_duplicate", 0)),
            "delivery_rejected": int(self.get_meta("delivery_rejected", 0)),
            "delivery_retry": int(self.get_meta("delivery_retry", 0)),
            "local_only_observed": int(self.get_meta("local_only_observed", 0)),
            "local_only_diagnostic_delivered": int(self.get_meta(
                "local_only_diagnostic_delivered", 0)),
            "local_only_suppressed": int(self.get_meta("local_only_suppressed", 0)),
            "provenance": {
                "process_nodes": process_nodes,
                "engine_anchors": engine_anchors,
                "pending_firewall": pending_firewall,
                "audit_health": json.loads(last_health["payload"]) if last_health else None,
                "payload_budget_bytes": STATE_MAX_BYTES,
                "wal_checkpoint_target_bytes": WAL_MAX_BYTES,
                "pending_cap_flush": int(self.get_meta(
                    "provenance_pending_cap_flush", 0)),
                "pending_expired": int(self.get_meta(
                    "provenance_pending_expired", 0)),
                "proof_conflict": int(self.get_meta(
                    "provenance_proof_conflict", 0)),
                "health_fail_open": int(self.get_meta(
                    "provenance_health_fail_open", 0)),
                "pending_downgrade_flush": int(self.get_meta(
                    "provenance_pending_downgrade_flush", 0)),
            },
            "local_only_last_seen": self.get_meta("local_only_last_seen"),
            "local_only_diagnostic": dict(self.local_only_diagnostic),
        }
