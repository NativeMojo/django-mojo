"""Crash-safe MojoSec state, aggregation, FIM baseline, and delivery spool."""

import json
import os
import sqlite3
import stat
import time

from .aggregation import merge, should_flush
from .protocol import canonical_json, make_event


SCHEMA_VERSION = 1
ANNOTATION_GRACE_SECONDS = 120
# The producer caps an active operation at 15 minutes. Retain exact active
# paths for that ceiling plus the five-minute post-completion match window.
ANNOTATION_MAX_GRACE_SECONDS = 20 * 60


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
    def __init__(self, state_dir, sensor_id, aggregation_config, delivery_config):
        _private_directory(state_dir)
        self.sensor_id = sensor_id
        self.aggregation_config = aggregation_config
        self.delivery_config = delivery_config
        self.path = os.path.join(state_dir, "state.sqlite3")
        self.db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=30000")
        self._create_schema()
        os.chmod(self.path, 0o600)
        stored_sensor_id = self.get_meta("sensor_id")
        if stored_sensor_id is None:
            self.set_meta("sensor_id", sensor_id)
        elif stored_sensor_id != sensor_id:
            self.db.close()
            raise StoreError(
                f"state belongs to sensor {stored_sensor_id!r}, not configured sensor {sensor_id!r}"
            )

    def _create_schema(self):
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                severity TEXT NOT NULL,
                created REAL NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt REAL NOT NULL DEFAULT 0,
                annotation_deadline REAL NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS events_delivery
                ON events(next_attempt, created);
            CREATE TABLE IF NOT EXISTS aggregates (
                fingerprint TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                severity TEXT NOT NULL,
                count INTEGER NOT NULL,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                flush_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS aggregates_due ON aggregates(flush_at);
            CREATE TABLE IF NOT EXISTS fim_baseline (
                profile TEXT NOT NULL,
                path TEXT NOT NULL,
                entry TEXT NOT NULL,
                PRIMARY KEY(profile, path)
            );
        """)
        aggregate_columns = {
            row["name"] for row in self.db.execute("PRAGMA table_info(aggregates)").fetchall()
        }
        if "severity" not in aggregate_columns:
            self.db.execute(
                "ALTER TABLE aggregates ADD COLUMN severity TEXT NOT NULL DEFAULT 'warning'"
            )
            rows = self.db.execute("SELECT fingerprint, payload FROM aggregates").fetchall()
            for row in rows:
                try:
                    severity = json.loads(row["payload"]).get("severity", "warning")
                except (AttributeError, json.JSONDecodeError):
                    severity = "warning"
                if severity not in ("info", "warning", "high", "critical"):
                    severity = "warning"
                self.db.execute(
                    "UPDATE aggregates SET severity = ? WHERE fingerprint = ?",
                    (severity, row["fingerprint"]),
                )
        event_columns = {
            row["name"] for row in self.db.execute("PRAGMA table_info(events)").fetchall()
        }
        if "annotation_deadline" not in event_columns:
            self.db.execute(
                "ALTER TABLE events ADD COLUMN annotation_deadline REAL NOT NULL DEFAULT 0")
        version = self.get_meta("schema_version")
        if version is None:
            self.set_meta("schema_version", SCHEMA_VERSION)
        elif version != SCHEMA_VERSION:
            raise StoreError(f"unsupported state schema version: {version}")

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

    def _enqueue(self, event, now=None):
        now = now if now is not None else time.time()
        if self.db.execute("SELECT 1 FROM events WHERE id = ?", (event["id"],)).fetchone():
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
            "id, payload, severity, created, next_attempt, annotation_deadline) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (event["id"], canonical_json(event), event["severity"], now,
             now + grace, now + grace if grace else 0),
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

    def _ingest_one(self, observation, now):
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

    def ingest(self, observations, cursor_key=None, cursor=None):
        """Durably queue observations and advance their collector cursor atomically."""
        now = time.time()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            for found in observations:
                self._ingest_one(found, now)
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

    def annotate_pending_fim(self, expected_changes_path, active_paths=None, now=None):
        """Enrich already-durable FIM evidence during its bounded delivery grace."""
        from .expected_changes import ExpectedChangeError, annotation, load_manifest

        try:
            entries = load_manifest(expected_changes_path)
        except ExpectedChangeError:
            entries = []
        now = now if now is not None else time.time()
        active_paths = set(active_paths or ())
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
                if (attributes.get("path") in active_paths and
                        now < row["created"] + ANNOTATION_MAX_GRACE_SECONDS):
                    deadline = min(
                        row["created"] + ANNOTATION_MAX_GRACE_SECONDS,
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
        rows = self.db.execute(
            "SELECT id, payload FROM events WHERE next_attempt <= ? ORDER BY created LIMIT ?",
            (time.time(), max_events),
        ).fetchall()
        events = []
        used = 1024
        for row in rows:
            event = json.loads(row["payload"])
            if event.get("kind") == "fim.change":
                attributes = event.get("attributes", {})
                for field in (
                        "mtime_ns", "ctime_ns", "device", "inode",
                        "sha256", "target_sha256"):
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

    def active_fim_profile(self):
        return self.get_meta("fim_active_profile")

    def rollback_fim_profile(self, digest):
        history = self.get_meta("fim_profile_history", [])
        identity = next((item for item in history if item.get("digest") == digest), None)
        if identity is None:
            raise StoreError("requested profile digest is not in intact rollback history")
        keys = [f"{identity['name']}:{identity['digest']}:{tier}"
                for tier in ("fast", "slow", "rpm")]
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
        return {
            "spooled_events": events,
            "pending_aggregates": aggregates,
            "dropped_capacity": int(self.get_meta("dropped_capacity", 0)),
            "dropped_aggregate_capacity": int(self.get_meta("dropped_aggregate_capacity", 0)),
            "aggregate_evicted_for_priority": int(self.get_meta("aggregate_evicted_for_priority", 0)),
            "delivery_accepted": int(self.get_meta("delivery_accepted", 0)),
            "delivery_duplicate": int(self.get_meta("delivery_duplicate", 0)),
            "delivery_rejected": int(self.get_meta("delivery_rejected", 0)),
            "delivery_retry": int(self.get_meta("delivery_retry", 0)),
        }
