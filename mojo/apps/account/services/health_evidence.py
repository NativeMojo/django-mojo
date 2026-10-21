"""Health snapshot observations, causal proof and bounded primary transactions.

Source observation time is independent of collection/publication time. These
pure projectors are also used by the scheduled collectors; HTTP reads never
invoke a collector.
"""
import copy
import hashlib
import ipaddress
import json
import math
import selectors
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

PROFILE_VERSION = 2
MAX_AGE_SECONDS = 420
FUTURE_TOLERANCE_SECONDS = 30
MAX_CONDITIONS = 1024
MAX_BASIS = 8
GRANTS = ("platform", "aws", "jobs")
GOOD_SOURCE_STATUSES = ("ok", "stale")


class EvidenceError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def utcnow():
    return datetime.now(timezone.utc)


def timestamp(value):
    return value.astimezone(timezone.utc).isoformat() if value is not None else None


def parse_time(value):
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(timezone.utc) if result.tzinfo is not None else None
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def valid_time(value, now):
    result = parse_time(value)
    return result if result is not None and result <= now + timedelta(seconds=30) else None


def slot(source_id, component_id, slot_id, *, source, required_grant,
         observed_at=None, collected_at=None, source_identity=None,
         status="ok", reason=None, value=None, unit=None, required=True,
         period_seconds=None, provenance=None, limits=None, truncated=False,
         previous=None, now=None):
    """Build one authoritative source slot; failed attempts cannot renew facts.

    A successful collection of an old provider sample is still a successful
    read, but its sample identity and provider deadline remain old. A proved
    target failure is an ordinary valid value (for example reachable=False),
    rather than a collector exception.
    """
    now = now or utcnow()
    if required_grant not in GRANTS or type(required) is not bool:
        raise EvidenceError("invalid_source_grant")
    if not isinstance(slot_id, str) or not slot_id or len(slot_id) > 256:
        raise EvidenceError("invalid_slot_id")
    sample = valid_time(observed_at, now)
    collected = valid_time(collected_at, now)
    valid = status in GOOD_SOURCE_STATUSES and sample is not None and collected is not None
    identity = copy.deepcopy(source_identity or {})
    output = dict(slot_id=slot_id, source=source, source_identity=identity,
                  status=status, reason=reason, required=required,
                  required_grant=required_grant, observed_at=None,
                  collected_at=timestamp(collected), last_success_at=None,
                  fresh_until=None, sample_id=None, value=None, unit=unit,
                  provenance=copy.deepcopy(provenance or {}),
                  limits=copy.deepcopy(limits or {}), truncated=bool(truncated))
    if not valid:
        if status in GOOD_SOURCE_STATUSES:
            output.update(status="unknown", reason="source_time_invalid")
        if previous:
            output["prior"] = {key: copy.deepcopy(previous.get(key)) for key in (
                "observed_at", "collected_at", "last_success_at", "fresh_until",
                "sample_id", "value", "unit", "source_identity")}
            for key in ("last_success_at", "fresh_until", "sample_id"):
                output[key] = previous.get(key)
        return output
    # The tolerance validates slight source skew; it grants no extra freshness.
    fresh_until = min(sample, now) + timedelta(seconds=MAX_AGE_SECONDS)
    output.update(observed_at=timestamp(sample), last_success_at=timestamp(collected),
                  value=copy.deepcopy(value), sample_id=digest([
                      source_id, component_id, slot_id, identity, timestamp(sample)]))
    if period_seconds is not None:
        if type(period_seconds) not in (int, float) or not math.isfinite(period_seconds) or period_seconds <= 0:
            raise EvidenceError("invalid_metric_period")
        fresh_until = min(min(sample, now) + timedelta(seconds=period_seconds * 3),
                          min(collected, now) + timedelta(seconds=MAX_AGE_SECONDS))
        output.update(period_seconds=period_seconds,
                      expected_cadence_seconds=period_seconds,
                      provider_lag_seconds=max(0, (collected - sample).total_seconds()))
    output["fresh_until"] = timestamp(fresh_until)
    if fresh_until <= now:
        output.update(status="stale", reason=reason or "sample_expired")
    return output


def slot_is_fresh(value, now):
    until = parse_time(value.get("fresh_until"))
    observed = valid_time(value.get("observed_at"), now)
    return bool(value.get("status") == "ok" and not value.get("truncated")
                and observed is not None and until is not None and until > now
                and valid_time(value.get("collected_at"), now) is not None
                and valid_time(value.get("last_success_at"), now) is not None
                and isinstance(value.get("sample_id"), str))


def condition(component_id, rule, resource_key, outcome, basis, *, impact="operational", reason=None, now=None):
    """Typed predicate proof; only fresh authoritative source slots form a basis."""
    if outcome not in ("pass", "fail", "unknown", "not_applicable") or impact not in ("availability", "operational"):
        raise EvidenceError("invalid_condition")
    if not isinstance(basis, list) or len(basis) > MAX_BASIS:
        raise EvidenceError("condition_basis_limit")
    now = now or utcnow()
    if outcome in ("pass", "fail") and (not basis or not all(slot_is_fresh(row, now) for row in basis)):
        outcome, reason = "unknown", "causal_basis_unavailable"
    return {"condition_key": digest([component_id, PROFILE_VERSION, rule, resource_key]),
            "outcome": outcome, "impact": impact, "reason": reason,
            "basis": [{"slot_id": row["slot_id"], "resource_key": resource_key,
                       "sample_id": row.get("sample_id"), "observed_at": row.get("observed_at"),
                       "fresh_until": row.get("fresh_until")} for row in basis]}


def component(source_id, kind, resource_id, evidence, conditions, *, service_id=None,
              resource_scope="installation", resource_owner_source_id=None,
              required=True, coverage_complete=True, reasons=None, now=None):
    """Conservative component rollup; a proved outage survives independent gaps."""
    now = now or utcnow()
    component_id = kind + ":" + resource_id
    required_slots = [row for row in evidence if row.get("required", True)
                      and row.get("status") != "not_applicable"]
    fresh = [row for row in required_slots if slot_is_fresh(row, now)]
    complete = bool(coverage_complete and required_slots and len(fresh) == len(required_slots))
    failures = [row for row in conditions if row["outcome"] == "fail"]
    availability = ("down" if any(row["impact"] == "availability" for row in failures) else
                    "up" if any(row["outcome"] == "pass" and row["impact"] == "availability" for row in conditions) else "unknown")
    status = ("unhealthy" if availability == "down" else "degraded" if failures else
              "healthy" if complete and all(row["outcome"] in ("pass", "not_applicable") for row in conditions)
              else "stale" if any(row.get("status") == "stale" for row in required_slots) else "unknown")
    observations = [parse_time(row.get("observed_at")) for row in required_slots]
    deadlines = [parse_time(row.get("fresh_until")) for row in required_slots]
    successes = [parse_time(row.get("last_success_at")) for row in required_slots]
    all_known = lambda values: bool(values) and all(value is not None for value in values)
    return {"component_id": component_id, "kind": kind, "resource_scope": resource_scope,
            "resource_id": resource_id, "resource_owner_source_id": resource_owner_source_id or source_id,
            "service_id": service_id, "required": required, "status": status, "availability": availability,
            "observed_at": timestamp(min(observations)) if all_known(observations) else None,
            "last_success_at": timestamp(min(successes)) if all_known(successes) else None,
            "fresh_until": timestamp(min(deadlines)) if all_known(deadlines) else None,
            "sample_id": digest([source_id, component_id, sorted((row["slot_id"], row.get("sample_id")) for row in evidence)]),
            "reasons": list(reasons or []), "evidence": copy.deepcopy(evidence),
            "conditions": copy.deepcopy(conditions), "coverage": {
                "expected_total": len(required_slots), "returned_total": len(required_slots),
                "observed_total": sum(value is not None for value in observations),
                "fresh_total": len(fresh), "unknown_total": len(required_slots) - len(fresh),
                "omitted_total": 0, "complete": complete, "truncated": False, "reasons": []}}


def _drive(generator, deadline, *, clock=None, fileno=None):
    """Consume psycopg's nonblocking protocol without an uncancellable worker."""
    clock = clock or time.monotonic
    try:
        state = next(generator)
        while True:
            fd, wanted = state if fileno is None else (fileno, state)
            remaining = deadline - clock()
            if remaining <= 0:
                raise EvidenceError("database_deadline_exceeded")
            with selectors.DefaultSelector() as selector:
                selector.register(fd, int(wanted))
                ready = selector.select(min(remaining, 0.1))
            state = generator.send(ready[0][1] if ready else 0)
    except StopIteration as stopped:
        return stopped.value
    finally:
        generator.close()


def _primary_connection(deadline, *, resolver=None, clock=None):
    """Private primary PostgreSQL connection, bounded numeric DNS + libpq poll.

    Never edits the shared Django connection, pool or transaction. Engines and
    connection modes whose acquisition cannot be bounded are explicit gaps.
    """
    from django.db import connections
    import psycopg
    from psycopg import pq
    from psycopg.conninfo import make_conninfo
    from mojo.helpers.dns.probe import resolve_addresses
    clock = clock or time.monotonic
    primary = connections["default"]
    if primary.vendor != "postgresql":
        raise EvidenceError("database_engine_unsupported")
    if not primary.settings_dict.get("NAME"):
        raise EvidenceError("database_connection_mode_unsupported")
    params = dict(primary.get_connection_params())
    for key in ("cursor_factory", "row_factory", "context", "prepare_threshold"):
        params.pop(key, None)
    host = params.get("host")
    if (not isinstance(host, str) or not host or "," in host or params.get("service")
            or "," in str(params.get("port", ""))):
        raise EvidenceError("database_connection_mode_unsupported")
    if not host.startswith("/"):
        addresses = (resolver or resolve_addresses)(host, deadline)
        if not addresses:
            raise EvidenceError("database_dns_unavailable")
        params["hostaddr"] = str(ipaddress.ip_address(addresses[0]))
    params.update(gssencmode="disable", target_session_attrs="any")
    params["options"] = str(params.get("options") or "") + " -c statement_timeout=750 -c lock_timeout=750"
    raw = pq.PGconn.connect_start(make_conninfo(**params).encode())
    try:
        while True:
            if clock() >= deadline:
                raise EvidenceError("database_deadline_exceeded")
            state = raw.connect_poll()
            if state == pq.PollingStatus.OK:
                break
            if state not in (pq.PollingStatus.READING, pq.PollingStatus.WRITING):
                raise EvidenceError("database_connection_unavailable")
            with selectors.DefaultSelector() as selector:
                selector.register(raw.socket, selectors.EVENT_READ if state == pq.PollingStatus.READING else selectors.EVENT_WRITE)
                selector.select(min(0.1, max(0, deadline - clock())))
        class DeadlineConnection(psycopg.Connection):
            def wait(self, generator, interval=0.1):
                return _drive(generator, min(deadline, self.operation_deadline), clock=clock, fileno=self.pgconn.socket)
        raw.nonblocking = 1
        result = DeadlineConnection(raw)
        result.operation_deadline = deadline
        result.autocommit = True
        return result
    except BaseException:
        raw.finish()
        raise


@contextmanager
def primary_cursor(deadline, *, connect=None, clock=None, max_queries=16):
    """A short-lived primary transaction; every SQL operation has <=750ms."""
    clock = clock or time.monotonic
    connection = (connect or _primary_connection)(deadline)
    raw = connection.cursor()
    count = 0
    class Cursor:
        def execute(self, sql, params=None):
            nonlocal count
            if clock() >= deadline or count >= max_queries:
                raise EvidenceError("database_budget_exceeded")
            count += 1
            connection.operation_deadline = min(deadline, clock() + 0.75)
            return raw.execute(sql, params)
        def fetchone(self):
            return raw.fetchone()
        def fetchall(self):
            return raw.fetchall()
    cursor = Cursor()
    try:
        cursor.execute("BEGIN")
        yield cursor
        cursor.execute("COMMIT")
    finally:
        # Closing libpq locally also aborts an unfinished transaction. No
        # rollback network operation may outlive the claimed deadline.
        raw.close()
        connection.close()
