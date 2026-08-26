"""Truthful public psycopg-pool telemetry with local, DB-independent sinks."""

import json
import os
import threading
import time
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
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.{process_uuid()}.tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, target)


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
