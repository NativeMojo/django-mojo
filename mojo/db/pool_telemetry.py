"""Truthful public psycopg-pool telemetry with local, DB-independent sinks."""

import json
import os
import threading
import time
import traceback
import uuid
from pathlib import Path

from mojo.db.errors import is_pool_acquisition_error


PUBLIC_GAUGES = (
    "pool_min", "pool_max", "pool_size", "pool_available", "requests_waiting",
)
PUBLIC_COUNTERS = (
    "requests_num", "requests_queued", "requests_wait_ms", "requests_errors",
    "usage_ms", "returns_bad", "connections_num", "connections_ms",
    "connections_errors", "connections_lost",
)
OBSERVED_COUNTERS = (
    "observed_acquisitions", "observed_acquisition_ms",
    "observed_acquisition_timeouts", "observed_acquisition_errors",
)
PROCESS_UUID = uuid.uuid4().hex
_PROCESS_PID = os.getpid()
_OBSERVED = {name: 0 for name in OBSERVED_COUNTERS}
_OBSERVED_LOCK = threading.Lock()


class LeaseTracker:
    """Lab-only acquire/return correlation without retaining DB connections."""

    def __init__(self, pid=None, monotonic_clock=time.monotonic,
                 wall_clock=time.time, stack_factory=None):
        self.pid = os.getpid() if pid is None else int(pid)
        self.monotonic_clock = monotonic_clock
        self.wall_clock = wall_clock
        self.stack_factory = stack_factory or self._bounded_stack
        self.lock = threading.Lock()
        self.sequence = 0
        self.by_connection = {}
        self.active = {}

    @staticmethod
    def _bounded_stack():
        frames = traceback.extract_stack(limit=14)[:-2]
        return tuple(
            f"{Path(frame.filename).name}:{frame.name}:{frame.lineno}"
            for frame in frames[-8:]
        )

    def _event(self, record, phase, sink_path=None, error=None):
        payload = {
            "schema": 1,
            "event": "database_pool_lease",
            "at": self.wall_clock(),
            "pid": self.pid,
            "lease_id": record["lease_id"],
            "alias": record["alias"],
            "phase": phase,
            "path": record.get("path"),
            "thread_id": record["thread_id"],
            "thread_name": record["thread_name"],
            "stack": record["stack"],
        }
        if error:
            payload["error"] = str(error)[:160]
        if sink_path is not None:
            append_bounded_event(sink_path, payload)
        return payload

    def acquired(self, connection, alias, path=None, sink_path=None):
        now = self.monotonic_clock()
        thread = threading.current_thread()
        connection_key = id(connection)
        with self.lock:
            self.sequence += 1
            lease_id = f"p{self.pid}-l{self.sequence}"
            record = {
                "lease_id": lease_id,
                "alias": str(alias)[:64],
                "path": str(path)[:240] if path else None,
                "thread_id": threading.get_ident(),
                "thread_name": thread.name[:80],
                "stack": tuple(self.stack_factory())[:8],
                "acquired_at": now,
                "phase": "acquired",
            }
            self.by_connection[connection_key] = lease_id
            self.active[lease_id] = record
        self._event(record, "acquired", sink_path=sink_path)
        return lease_id

    def _find(self, connection):
        with self.lock:
            lease_id = self.by_connection.get(id(connection))
            return self.active.get(lease_id) if lease_id else None

    def returning(self, connection, sink_path=None):
        record = self._find(connection)
        if record is None:
            return None
        with self.lock:
            record["phase"] = "returning"
        return self._event(record, "returning", sink_path=sink_path)

    def returned(self, connection, sink_path=None):
        connection_key = id(connection)
        with self.lock:
            lease_id = self.by_connection.pop(connection_key, None)
            record = self.active.pop(lease_id, None) if lease_id else None
        if record is None:
            return None
        return self._event(record, "returned", sink_path=sink_path)

    def return_failed(self, connection, error, sink_path=None):
        record = self._find(connection)
        if record is None:
            return None
        with self.lock:
            record["phase"] = "return_failed"
            record["error"] = str(error)[:160]
        return self._event(
            record, "return_failed", sink_path=sink_path, error=error)

    def snapshot(self):
        now = self.monotonic_clock()
        with self.lock:
            records = sorted(
                (dict(record) for record in self.active.values()),
                key=lambda record: record["acquired_at"],
            )
        leases = []
        for record in records[:8]:
            leases.append({
                "lease_id": record["lease_id"],
                "alias": record["alias"],
                "path": record.get("path"),
                "thread_id": record["thread_id"],
                "thread_name": record["thread_name"],
                "stack": record["stack"],
                "phase": record["phase"],
                "age_seconds": round(max(0.0, now - record["acquired_at"]), 3),
                **({"error": record["error"]} if record.get("error") else {}),
            })
        return {
            "count": len(records),
            "oldest_seconds": leases[0]["age_seconds"] if leases else 0,
            "leases": leases,
        }


_LEASE_TRACKER = LeaseTracker()


def lease_trace_enabled():
    try:
        from django.conf import settings
        return bool(getattr(settings, "DATABASE_POOL_LAB_TRACE_LEASES", False))
    except Exception:
        return False


def _lease_sink_path():
    root = Path(os.environ.get("MOJO_POOL_TELEMETRY_ROOT", "/tmp/mojo-pool"))
    return root / f"worker-{os.getpid()}-leases.jsonl"


def _active_request_path():
    try:
        from mojo.models.rest import ACTIVE_REQUEST
        request = ACTIVE_REQUEST.get()
        path = getattr(request, "path", None) if request is not None else None
        return str(path)[:240] if path else None
    except Exception:
        return None


def _trace_best_effort(method, *args, **kwargs):
    try:
        return method(*args, **kwargs)
    except Exception:
        return None


def record_lease_acquired(connection, alias, *, tracker=None):
    owner = tracker or _LEASE_TRACKER
    return _trace_best_effort(
        owner.acquired, connection, alias=alias, path=_active_request_path(),
        sink_path=_lease_sink_path())


def record_lease_returning(connection, *, tracker=None):
    owner = tracker or _LEASE_TRACKER
    return _trace_best_effort(
        owner.returning, connection, sink_path=_lease_sink_path())


def record_lease_returned(connection, *, tracker=None):
    owner = tracker or _LEASE_TRACKER
    return _trace_best_effort(
        owner.returned, connection, sink_path=_lease_sink_path())


def record_lease_return_failed(connection, error, *, tracker=None):
    owner = tracker or _LEASE_TRACKER
    return _trace_best_effort(
        owner.return_failed, connection, error, sink_path=_lease_sink_path())


def active_lease_snapshot(*, tracker=None):
    value = _trace_best_effort((tracker or _LEASE_TRACKER).snapshot)
    if value is None:
        return {"count": 0, "oldest_seconds": 0, "leases": [], "trace_error": True}
    return value


def process_uuid():
    """Return a process-unique identity even when the module was pre-forked."""
    global PROCESS_UUID, _PROCESS_PID
    pid = os.getpid()
    if pid != _PROCESS_PID:
        PROCESS_UUID = uuid.uuid4().hex
        _PROCESS_PID = pid
    return PROCESS_UUID


def _number(value):
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)) and value >= 0:
        return value
    return 0


def normalize_public_stats(stats):
    source = stats if isinstance(stats, dict) else {}
    normalized = {}
    for name in PUBLIC_GAUGES + PUBLIC_COUNTERS:
        normalized[name] = _number(source.get(name, 0))
    allocated = normalized["pool_size"]
    available = min(normalized["pool_available"], allocated)
    normalized["pool_available"] = available
    normalized["in_use_or_preparing"] = max(0, allocated - available)
    normalized["full_sized_idle"] = bool(
        allocated == normalized["pool_max"] and available == allocated)
    return normalized


def record_acquisition(elapsed, error=None):
    milliseconds = max(0, int(float(elapsed) * 1000))
    with _OBSERVED_LOCK:
        _OBSERVED["observed_acquisitions"] += 1
        _OBSERVED["observed_acquisition_ms"] += milliseconds
        if error is not None:
            _OBSERVED["observed_acquisition_errors"] += 1
            if is_pool_acquisition_error(error):
                _OBSERVED["observed_acquisition_timeouts"] += 1


def observed_counters():
    with _OBSERVED_LOCK:
        return dict(_OBSERVED)


def reset_safe_delta(current, previous):
    old = previous if isinstance(previous, dict) else {}
    delta = {}
    for name in PUBLIC_COUNTERS + OBSERVED_COUNTERS:
        now_value = _number(current.get(name, 0))
        old_value = _number(old.get(name, 0))
        delta[name] = now_value - old_value if now_value >= old_value else now_value
    return delta


def classify(stats, previous_state=None):
    allocated = stats.get("pool_size", 0)
    available = stats.get("pool_available", 0)
    maximum = stats.get("pool_max", 0)
    waiters = stats.get("requests_waiting", 0)
    recent_errors = stats.get("interval", {}).get("requests_errors", 0)
    recent_timeouts = stats.get("interval", {}).get("observed_acquisition_timeouts", 0)
    if available == 0 and (waiters > 0 or recent_errors > 0 or recent_timeouts > 0):
        return "exhausted"
    if previous_state == "exhausted" and available > 0:
        return "recovering"
    if maximum and allocated == maximum and available == allocated:
        return "healthy_idle"
    if available == 0 and allocated:
        return "saturated"
    if allocated == 0:
        return "cold"
    return "busy" if available < allocated else "healthy"


def pool_snapshot(pool, identity=None, previous=None, now=None):
    timestamp = time.time() if now is None else now
    public = normalize_public_stats(pool.get_stats() if pool is not None else {})
    public.update(observed_counters())
    prior_counters = (previous or {}).get("counters", {})
    interval = reset_safe_delta(public, prior_counters)
    payload = {
        "schema": 1,
        "at": timestamp,
        "pid": os.getpid(),
        "process_uuid": process_uuid(),
        "identity": dict(identity or {}),
        "gauges": {name: public[name] for name in PUBLIC_GAUGES},
        "counters": {
            name: public[name] for name in PUBLIC_COUNTERS + OBSERVED_COUNTERS
        },
        "interval": interval,
        "in_use_or_preparing": public["in_use_or_preparing"],
        "full_sized_idle": public["full_sized_idle"],
    }
    state_input = dict(public)
    state_input["interval"] = interval
    payload["state"] = classify(state_input, (previous or {}).get("state"))
    return payload


def atomic_write(path, payload):
    target = Path(path)
    # Snapshots contain no credentials and are group-readable so a separate
    # local observer identity need not share the application UID.
    target.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.{process_uuid()}.tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    os.chmod(temporary, 0o640)
    os.replace(temporary, target)


def append_bounded_event(path, payload, max_bytes=262144):
    """Append one private JSON event, atomically rotating before the size cap."""
    target = Path(path)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    line = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    if len(line) > 4096:
        raise ValueError("pool telemetry event exceeds 4096 bytes")
    try:
        rotate = target.stat().st_size + len(line) > max_bytes
    except FileNotFoundError:
        rotate = False
    if rotate:
        temporary = target.with_name(
            f".{target.name}.{os.getpid()}.{process_uuid()}.tmp")
        temporary.write_bytes(line)
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
        return
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(descriptor, line)
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)


def should_emit_state_event(state, previous_state, last_event_at, now, reminder=300):
    """Emit transitions immediately and unhealthy reminders at a bounded rate."""
    if state != previous_state:
        return True
    return bool(
        state in {"saturated", "exhausted", "recovering"}
        and now - (last_event_at or 0) >= reminder
    )


def read_snapshot(path):
    try:
        value = json.loads(Path(path).read_text())
        return value if isinstance(value, dict) else None
    except Exception:
        return None


def aggregate_snapshots(paths, max_age, now=None):
    timestamp = time.time() if now is None else now
    fresh = {}
    stale = 0
    for path in paths:
        snapshot = read_snapshot(path)
        if not snapshot or timestamp - _number(snapshot.get("at")) > max_age:
            stale += 1
            continue
        process_uuid = snapshot.get("process_uuid")
        if not isinstance(process_uuid, str) or not process_uuid:
            stale += 1
            continue
        current = fresh.get(process_uuid)
        if current is None or snapshot.get("at", 0) > current.get("at", 0):
            fresh[process_uuid] = snapshot
    totals = {name: 0 for name in PUBLIC_GAUGES}
    totals["in_use_or_preparing"] = 0
    states = {}
    for snapshot in fresh.values():
        gauges = snapshot.get("gauges") or {}
        for name in PUBLIC_GAUGES:
            totals[name] += _number(gauges.get(name))
        totals["in_use_or_preparing"] += _number(snapshot.get("in_use_or_preparing"))
        state = str(snapshot.get("state") or "unknown")
        states[state] = states.get(state, 0) + 1
    return {
        "schema": 1,
        "at": timestamp,
        "workers": len(fresh),
        "stale_files": stale,
        "gauges": totals,
        "states": states,
        "exhausted": states.get("exhausted", 0) > 0,
    }
