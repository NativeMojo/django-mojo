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
import uuid
import threading
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
    maximum = slot_deadline(value)
    return bool(value.get("status") == "ok" and not value.get("truncated")
                and observed is not None and until is not None and until > now
                and maximum is not None and until <= maximum
                and valid_time(value.get("collected_at"), now) is not None
                and valid_time(value.get("last_success_at"), now) is not None
                and isinstance(value.get("sample_id"), str))


def slot_deadline(value):
    sample, collected = parse_time(value.get("observed_at")), parse_time(value.get("collected_at"))
    if sample is None or collected is None:
        return None
    period = value.get("period_seconds")
    if period is None:
        return sample + timedelta(seconds=MAX_AGE_SECONDS)
    if type(period) not in (int, float) or not math.isfinite(period) or period <= 0:
        return None
    return min(sample + timedelta(seconds=3 * period), collected + timedelta(seconds=MAX_AGE_SECONDS))


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
              required=True, coverage_complete=True, reasons=None, now=None, identity_grant=None):
    """Conservative component rollup; a proved outage survives independent gaps."""
    now = now or utcnow()
    owning_grants = {row["required_grant"] for row in evidence}
    identity_grant = identity_grant or (next(iter(owning_grants)) if len(owning_grants) == 1 else "platform")
    if identity_grant not in GRANTS:
        raise EvidenceError("invalid_identity_grant")
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
            "identity_grant": identity_grant,
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
def primary_cursor(deadline, *, connect=None, clock=None, max_queries=16, budget=None):
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
            if budget is not None:
                budget.take("db_queries")
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


FAILURE_CURSOR_SALT = "mojo.health.failures.v2"
FAILURE_LIMITATIONS = ["best_effort_event_capture", "retention_unproven",
                      "old_job_prune_cascades_recent_events", "manual_purge_possible",
                      "commit_delay_beyond_600_seconds"]


def _failure_rows(phase, start, end, after, limit, deadline):
    from mojo.apps.jobs.models import Job, JobEvent
    if phase == "events":
        table, at = JobEvent._meta.db_table, "at"
        fields = "id, job_id, channel, event, at, attempt"
        columns = ("id", "job_id", "channel", "event", "at", "attempt")
        clause = "event IN ('failed', 'expired')"
    else:
        table, at = Job._meta.db_table, "finished_at"
        fields = "id, channel, status, attempt, finished_at"
        columns = ("id", "channel", "status", "attempt", "finished_at")
        clause = "status IN ('failed', 'expired')"
    params = [start, end]
    if after:
        clause += ' AND ("%s", id) > (%%s, %%s)' % at
        params += [parse_time(after[0]), after[1]]
    sql = 'SELECT %s FROM "%s" WHERE "%s">=%%s AND "%s"<=%%s AND %s ORDER BY "%s", id LIMIT %%s' % (fields, table, at, at, clause, at)
    with primary_cursor(deadline, max_queries=4) as cursor:
        cursor.execute(sql, params + [limit + 1])
        return [dict(zip(columns, row)) for row in cursor.fetchall()]


def failure_page(source_id, cursor=None, *, deadline, now=None, reader=None):
    """Frozen failure-only keyset windows; a completed cursor opens overlap."""
    from django.core import signing
    now, reader = now or utcnow(), reader or _failure_rows
    start, end, phase, position, gaps = now - timedelta(hours=24), now, "events", None, []
    expires = now + timedelta(hours=1)
    if cursor is not None:
        if not isinstance(cursor, str) or not 0 < len(cursor) <= 4096:
            raise EvidenceError("invalid_evidence_cursor")
        try:
            saved = signing.loads(cursor, salt=FAILURE_CURSOR_SALT)
            if (not isinstance(saved, dict) or saved.get("source") != source_id
                    or saved.get("profile") != PROFILE_VERSION or saved.get("query") != "failed_expired_v2"
                    or saved.get("phase") not in ("events", "fallback", "complete")):
                raise ValueError()
            start, end = parse_time(saved.get("start")), parse_time(saved.get("end"))
            expires = parse_time(saved.get("expires"))
            if (start is None or end is None or expires is None or start > end
                    or end > now + timedelta(seconds=30) or end - start > timedelta(hours=24)):
                raise ValueError()
            phase, position = saved["phase"], saved.get("position")
            if position is not None and (not isinstance(position, list) or len(position) != 2
                    or parse_time(position[0]) is None or not start <= parse_time(position[0]) <= end
                    or (phase == "events" and (type(position[1]) is not int or position[1] < 1))
                    or (phase == "fallback" and (not isinstance(position[1], str) or len(position[1]) != 32
                                                or uuid.UUID(position[1]).hex != position[1]))):
                raise ValueError()
            if expires <= now:
                gaps.append("cursor_expired")
                start, end, phase, position = now - timedelta(hours=24), now, "events", None
                expires = now + timedelta(hours=1)
            elif phase == "complete":
                start, end, phase, position = end - timedelta(seconds=600), now, "events", None
                expires = now + timedelta(hours=1)
                if now - start > timedelta(hours=24):
                    start = now - timedelta(hours=24)
                    gaps.append("consumer_interval_beyond_24_hours")
        except (ValueError, TypeError, signing.BadSignature):
            raise EvidenceError("invalid_evidence_cursor") from None
    result = {"window_start": timestamp(start), "window_end": timestamp(end),
              "capture_status": "best_effort", "limitations": list(FAILURE_LIMITATIONS),
              "retention_basis": "jobs_created_before_7_days_may_be_pruned_with_events",
              "events": [], "terminal_fallback": [], "has_more": False,
              "next_cursor": None, "gaps": gaps, "counts_scope": "returned_page",
              "total": None}
    result["checkpoint_cursor"] = signing.dumps({"source": source_id, "profile": PROFILE_VERSION,
        "query": "failed_expired_v2", "start": timestamp(start), "end": timestamp(end),
        "phase": phase, "position": position, "expires": timestamp(expires)}, salt=FAILURE_CURSOR_SALT, compress=True)
    remaining = 200
    try:
        while phase != "complete" and remaining:
            rows = reader(phase, start, end, position, remaining, deadline)
            more = len(rows) > remaining
            selected = rows[:remaining]
            destination = result["events" if phase == "events" else "terminal_fallback"]
            for row in selected:
                row = dict(row)
                if phase == "events":
                    if type(row.get("id")) is not int or row["id"] < 1:
                        raise EvidenceError("invalid_durable_evidence")
                    row["job_id"] = uuid.UUID(str(row["job_id"])).hex
                else:
                    row["id"] = uuid.UUID(str(row["id"])).hex
                at_key = "at" if phase == "events" else "finished_at"
                at = parse_time(row.get(at_key))
                if at is None or not start <= at <= end:
                    raise EvidenceError("invalid_durable_evidence")
                destination.append({**row, at_key: timestamp(at)})
            remaining -= len(selected)
            if more:
                last = destination[-1]
                position = [timestamp(parse_time(last["at" if phase == "events" else "finished_at"])), last["id"]]
                result["has_more"] = True
                result["gaps"].append("page_backlog")
                break
            phase, position = ("fallback" if phase == "events" else "complete"), None
        if not remaining and phase != "complete":
            result["has_more"] = True
            result["gaps"].append("page_backlog")
    except Exception:
        result["gaps"].append("durable_read_unavailable")
        result["has_more"] = True
    # Even a failed initial page checkpoints the original closed lower bound.
    # Continuation never extends its expiry, including after database errors.
    result["next_cursor"] = signing.dumps({"source": source_id, "profile": PROFILE_VERSION,
        "query": "failed_expired_v2", "start": timestamp(start), "end": timestamp(end),
        "phase": phase, "position": position, "expires": timestamp(expires)},
        salt=FAILURE_CURSOR_SALT, compress=True)
    result["gaps"] = sorted(set(result["gaps"]))
    return result


class CycleBudget:
    LIMITS = {"provider_operations": 32, "inventory_pages": 4, "metric_discovery_pages": 4,
              "cloudwatch_series": 256, "checked_broadcasts": 4, "checked_targets": 128,
              "redis_commands": 256, "db_queries": 16, "api_services": 24, "channels": 32}
    def __init__(self, deadline, *, limits=None, clock=None):
        self.clock, self.deadline = clock or time.monotonic, deadline
        self.limits = {**self.LIMITS, **(limits or {})}
        self.counts, self.lock = {}, threading.Lock()
    def take(self, name, count=1):
        with self.lock:
            if self.clock() >= self.deadline or self.counts.get(name, 0) + count > self.limits[name]:
                raise TimeoutError("not_collected_budget")
            self.counts[name] = self.counts.get(name, 0) + count


class BudgetRedis:
    def __init__(self, client, budget):
        self.client, self.budget = client, budget
    def __enter__(self):
        self.client.__enter__()
        return self
    def __exit__(self, *args):
        return self.client.__exit__(*args)
    def __getattr__(self, name):
        target = getattr(self.client, name)
        if not callable(target):
            return target
        def call(*args, **kwargs):
            if name not in ("close", "reset"):
                self.budget.take("redis_commands")
            result = target(*args, **kwargs)
            return BudgetRedis(result, self.budget) if name in ("pipeline", "pubsub") else result
        return call


def source_configuration(deadline, *, get_static=None, cursor_context=None, budget=None):
    """One bounded primary query for frozen settings and the declared API roster."""
    from mojo.apps.account.models import Setting
    from mojo.apps.account.services import system_settings, health_snapshot
    from mojo.apps.aws.services import fleet_health
    from django.apps import apps as django_apps
    from mojo.helpers.settings import settings
    from urllib.parse import urlsplit
    get_static = get_static or settings.get_static
    keys = [system_settings.INSTALLATION_UUID, system_settings.INSTALLATION_SLUG,
            system_settings.BASE_URL, system_settings.EXPECTED_EDGE_TOPOLOGY]
    webapps_sql = "NULL"
    if django_apps.is_installed("mojo.apps.edge"):
        from mojo.apps.edge.models import WebApp, Vhost
        webapps_sql = '(SELECT jsonb_agg(jsonb_build_object(\'id\', w.id, \'origin\', v.server_name)) FROM (SELECT id, vhost_id FROM "%s" ORDER BY id LIMIT 25) w LEFT JOIN "%s" v ON v.id=w.vhost_id)' % (WebApp._meta.db_table, Vhost._meta.db_table)
    with (cursor_context or (lambda end: primary_cursor(end, budget=budget)))(deadline) as cursor:
        cursor.execute('SELECT (SELECT jsonb_object_agg(key, value) FROM "%s" WHERE group_id IS NULL AND key=ANY(%%s)), %s' % (Setting._meta.db_table, webapps_sql), [keys])
        stored, apps = cursor.fetchone()
    stored, apps = stored or {}, apps or []
    pair = system_settings._validate_identity_pair(stored.get(system_settings.INSTALLATION_UUID), stored.get(system_settings.INSTALLATION_SLUG))
    identity = health_snapshot.resolve_identity(identity_reader=lambda: pair, get_static=get_static)
    topology = stored.get(system_settings.EXPECTED_EDGE_TOPOLOGY)
    if isinstance(topology, str):
        topology = json.loads(topology)
    expected = fleet_health._read_expected({"platform": True, "aws": True, "jobs": True}, deadline,
        get_static=get_static, topology_reader=lambda key: topology)
    services = [{"resource_id": "primary", "service_id": "primary", "origin": stored.get(system_settings.BASE_URL), "path": "/api/version"}]
    for app in apps[:23]:
        services.append({"resource_id": "webapp-%s" % app["id"], "service_id": "webapp-%s" % app["id"],
                         "origin": "https://" + app["origin"] if app.get("origin") else None, "path": "/"})
    channels = get_static("JOBS_CHANNELS", [])
    if isinstance(channels, str):
        channels = [part.strip() for part in channels.split(",") if part.strip()]
    channels_valid = isinstance(channels, (list, tuple)) and all(isinstance(channel, str) and 0 < len(channel) <= 100 for channel in channels)
    channels = sorted(set(channels)) if channels_valid else []
    from django.db import connections
    redis_origin = str(get_static("REDIS_URL", "") or "")
    binding = {"database_host": connections["default"].settings_dict.get("HOST"),
               "redis_host": urlsplit(redis_origin).hostname if redis_origin else get_static("REDIS_SERVER", "localhost")}
    revision = health_snapshot.configuration_revision(identity, {"roster_scope": fleet_health.roster_scope(expected),
        "services": services, "channels": channels, "bindings": binding})
    return {**identity, "config_revision": revision, "expected": expected, "services": services,
            "services_omitted": max(0, len(apps) - 23), "channels": channels[:32],
            "channels_complete": bool(channels_valid and channels and len(channels) <= 32), "bindings": binding}


def database_and_job_facts(channels, deadline, *, budget=None, cursor_context=None):
    """Grouped primary facts: no query text, usernames or job payload/error fields."""
    from mojo.apps.jobs.models import Job, JobEvent
    sql = '''SELECT
        (SELECT jsonb_build_object('reachable', true, 'commits', xact_commit, 'rollbacks', xact_rollback,
          'deadlocks', deadlocks, 'stats_reset', stats_reset, 'incarnation', pg_postmaster_start_time(),
          'active', (SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND state='active'),
          'idle_in_transaction', (SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND state='idle in transaction'),
          'oldest_transaction_seconds', (SELECT extract(epoch FROM CURRENT_TIMESTAMP-min(xact_start)) FROM pg_stat_activity WHERE datname=current_database()))
         FROM pg_stat_database WHERE datname=current_database()),
        (SELECT jsonb_agg(row_to_json(q)) FROM (SELECT channel,
          count(*) FILTER (WHERE status='pending' AND (run_at IS NULL OR run_at<=CURRENT_TIMESTAMP)) AS due_pending,
          min(COALESCE(run_at,created)) FILTER (WHERE status='pending' AND (run_at IS NULL OR run_at<=CURRENT_TIMESTAMP)) AS oldest_due,
          count(*) FILTER (WHERE status='running') AS running, min(started_at) FILTER (WHERE status='running') AS oldest_running,
          max(finished_at) FILTER (WHERE status='completed') AS latest_completed,
          count(*) FILTER (WHERE status='running' AND max_exec_seconds IS NOT NULL AND started_at+max_exec_seconds*interval '1 second'<CURRENT_TIMESTAMP) AS overruns
          FROM "%s" WHERE channel=ANY(%%s) GROUP BY channel) q),
        (SELECT jsonb_agg(row_to_json(e)) FROM (SELECT channel, event, count(*) AS count, max(at) AS latest
          FROM "%s" WHERE channel=ANY(%%s) AND at>=CURRENT_TIMESTAMP-interval '5 minutes'
          AND event IN ('completed','failed','expired','retry') GROUP BY channel,event) e), CURRENT_TIMESTAMP, 1''' % (Job._meta.db_table, JobEvent._meta.db_table)
    started = time.monotonic()
    with (cursor_context or (lambda end: primary_cursor(end, budget=budget)))(deadline) as cursor:
        cursor.execute(sql, [channels, channels])
        database, jobs, events, observed, one = cursor.fetchone()
    return {"database": database or {}, "jobs": jobs or [], "events": events or [],
            "observed_at": timestamp(parse_time(observed)), "latency_seconds": time.monotonic() - started, "reachable": one == 1}


def redis_facts(deadline, *, connect=None, budget=None):
    from mojo.helpers.redis import get_bounded_connection
    started = time.monotonic()
    with (connect or get_bounded_connection)(timeout=0.5, max_connections=1, read_from_replicas=False, deadline=deadline) as raw:
        client = BudgetRedis(raw, budget) if budget is not None else raw
        reachable = bool(client.ping())
        latency = time.monotonic() - started
        info = client.info()
    keys = ("used_memory", "maxmemory", "evicted_keys", "rejected_connections", "connected_clients",
            "blocked_clients", "uptime_in_seconds", "run_id")
    return {"reachable": reachable, "latency_seconds": latency, "observed_at": timestamp(utcnow()),
            "scope": "default_node", **{key: info.get(key) for key in keys}}


def probe_service(service, deadline, *, resolver=None, connector=None, ssl_context=None, http_factory=None):
    """Fixed-origin, DNS-pinned HTTP/version probe with one absolute1.5s budget."""
    import http.client
    import ssl
    from urllib.parse import urlsplit
    from mojo.apps.account.services.system_settings import validate_base_url
    from mojo.helpers.bounded_transport import connect_tcp, DeadlineSocket, remaining
    from mojo.helpers.dns.probe import resolve_addresses
    deadline = min(deadline, time.monotonic() + 1.5)
    origin = validate_base_url(service["origin"])
    parsed = urlsplit(origin)
    addresses = (resolver or resolve_addresses)(parsed.hostname, deadline)
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise EvidenceError("unsafe_service_origin")
    class Connection(http.client.HTTPSConnection):
        def connect(self):
            raw = (connector or connect_tcp)(self.host, self.port, deadline,
                resolver=lambda hostname, end: addresses)
            try:
                raw.settimeout(remaining(deadline))
                tls = (ssl_context or ssl.create_default_context()).wrap_socket(raw, server_hostname=self.host)
                self.sock = DeadlineSocket(tls, deadline)
            except BaseException:
                raw.close()
                raise
    connection = (http_factory or Connection)(parsed.hostname, parsed.port or 443, timeout=remaining(deadline))
    started = time.monotonic()
    try:
        connection.request("GET", service["path"], headers={"Host": parsed.netloc, "Accept": "application/json" if service["path"] == "/api/version" else "text/html", "Connection": "close"})
        response = connection.getresponse()
        raw = response.read(4097 if service["path"] == "/api/version" else 65537)
        body_limit = 4096 if service["path"] == "/api/version" else 65536
        version = None
        if service["path"] == "/api/version" and len(raw) <= body_limit:
            try:
                body = json.loads(raw)
                body = body.get("data", body) if isinstance(body, dict) else {}
                version = str(body.get("version") or "")[:64]
            except (ValueError, TypeError, AttributeError):
                pass
        return {"reachable": 200 <= response.status < 300, "http_status": response.status,
                "version": version, "body_truncated": len(raw) > body_limit,
                "latency_seconds": time.monotonic() - started, "observed_at": timestamp(utcnow())}
    finally:
        connection.close()


def managed_series(configuration, deadline, *, budget, provider_factory=None):
    """Exact declared RDS/cache resources bound to the actual connection host."""
    from mojo.deploy.provision.spec import names
    from mojo.helpers.aws.provider_call import DeadlineProviderClient
    spec = configuration["expected"].get("spec")
    result = {"series": [], "database": {"status": "unknown", "reason": "managed_resource_unresolved"},
              "redis": {"status": "unknown", "reason": "managed_resource_unresolved"}}
    if spec is None:
        return result
    resolved = names(spec)
    factory = provider_factory or (lambda service: DeadlineProviderClient(service, deadline, region=spec.region))
    def call(service, operation, **params):
        budget.take("provider_operations")
        return getattr(factory(service), operation)(**params)
    def add(kind, resource, metric, unit, dimensions, namespace):
        result["series"].append({"key": kind + ":" + resource + ":" + metric, "kind": kind, "resource": resource,
            "metric": metric, "unit": unit, "namespace": namespace, "dimensions": dimensions, "period_seconds": 300})
    try:
        rows = call("rds", "describe_db_instances", DBInstanceIdentifier=resolved["db_writer"]).get("DBInstances") or []
        if len(rows) == 1 and rows[0].get("DBInstanceIdentifier") == resolved["db_writer"]:
            row = rows[0]
            host = (row.get("Endpoint") or {}).get("Address")
            bound = str(configuration["bindings"].get("database_host") or "").rstrip(".").lower()
            # An Aurora cluster's writer endpoint is an exact declared binding.
            cluster_endpoint = None
            if row.get("DBClusterIdentifier") == resolved["db_cluster"] and host != bound:
                clusters = call("rds", "describe_db_clusters", DBClusterIdentifier=resolved["db_cluster"]).get("DBClusters") or []
                cluster_endpoint = clusters[0].get("Endpoint") if len(clusters) == 1 else None
            if bound and bound in (host, cluster_endpoint):
                result["database"] = {"status": "ok", "resource": row["DBInstanceIdentifier"], "binding": "validated_endpoint"}
                for metric, unit in (("CPUUtilization", "Percent"), ("DatabaseConnections", "Count"), ("FreeableMemory", "Bytes"),
                                     ("FreeStorageSpace", "Bytes"), ("ReadLatency", "Seconds"), ("WriteLatency", "Seconds")):
                    add("database", row["DBInstanceIdentifier"], metric, unit, [{"Name": "DBInstanceIdentifier", "Value": row["DBInstanceIdentifier"]}], "AWS/RDS")
    except Exception:
        result["database"] = {"status": "unknown", "reason": "managed_database_unavailable"}
    try:
        rows = call("elasticache", "describe_replication_groups", ReplicationGroupId=resolved["cache_group"]).get("ReplicationGroups") or []
        row = rows[0] if len(rows) == 1 and rows[0].get("ReplicationGroupId") == resolved["cache_group"] else {}
        nodes = row.get("NodeGroups") or []
        endpoint = (nodes[0].get("PrimaryEndpoint") or {}).get("Address") if len(nodes) == 1 else None
        bound = str(configuration["bindings"].get("redis_host") or "").rstrip(".").lower()
        if endpoint and bound == endpoint:
            result["redis"] = {"status": "ok", "resource": resolved["cache_group"], "binding": "validated_endpoint"}
            for member in (row.get("MemberClusters") or [])[:4]:
                clusters = call("elasticache", "describe_cache_clusters", CacheClusterId=member, ShowCacheNodeInfo=True).get("CacheClusters") or []
                cluster = clusters[0] if len(clusters) == 1 and clusters[0].get("CacheClusterId") == member else {}
                for node in (cluster.get("CacheNodes") or [])[:4]:
                    node_id = node.get("CacheNodeId")
                    if not node_id:
                        continue
                    for metric, unit in (("CPUUtilization", "Percent"), ("EngineCPUUtilization", "Percent"),
                                         ("DatabaseMemoryUsagePercentage", "Percent"), ("CurrConnections", "Count"),
                                         ("Evictions", "Count"), ("ReplicationLag", "Seconds")):
                        add("redis", member + ":" + node_id, metric, unit,
                            [{"Name": "CacheClusterId", "Value": member}, {"Name": "CacheNodeId", "Value": node_id}], "AWS/ElastiCache")
    except Exception:
        result["redis"] = {"status": "unknown", "reason": "managed_cache_unavailable"}
    return result


def build_components(configuration, native, redis, fleet, probes, *, managed=None, previous=None, now=None):
    """Profile2 current obligations and causal-slot proof from independent sources."""
    now, source_id = now or utcnow(), configuration["source_id"]
    components = []
    old = {row["component_id"]: row for row in (previous or {}).get("components", [])}
    def make(kind, resource, slot_id, raw, *, grant="platform", source=None, value=None, identity=None):
        previous_slot = next((row for row in old.get(kind + ":" + resource, {}).get("evidence", []) if row["slot_id"] == slot_id), None)
        raw = raw or {}
        return slot(source_id, kind + ":" + resource, slot_id, source=source or grant, required_grant=grant,
            source_identity=identity or {}, observed_at=raw.get("observed_at"), collected_at=raw.get("collected_at") or timestamp(now),
            status=raw.get("status", "ok" if raw.get("observed_at") else "unknown"), reason=raw.get("reason"),
            value=raw if value is None else value, unit=raw.get("unit"), period_seconds=raw.get("period_seconds"),
            truncated=raw.get("truncated", False), provenance={key: raw[key] for key in ("namespace", "metric", "dimensions", "start", "end", "aggregation") if key in raw},
            previous=previous_slot, now=now)
    def predicate(kind, resource, rule, value, predicate_value, *, impact="operational", resource_key=None):
        outcome = "unknown" if predicate_value is None else "fail" if predicate_value else "pass"
        return condition(kind + ":" + resource, rule, resource_key or resource, outcome, [value], impact=impact, now=now)
    def pressured(value, threshold=90):
        return value >= threshold if type(value) in (int, float) and math.isfinite(value) else None
    for service in configuration["services"]:
        raw = probes.get(service["resource_id"]) or {"status": "unknown", "reason": "service_probe_unavailable"}
        sample = make("api", service["resource_id"], "http", raw, source="http", value={key: raw.get(key) for key in ("reachable", "http_status", "version", "latency_seconds", "body_truncated")})
        checks = [predicate("api", service["resource_id"], "http_availability", sample,
                            not raw["reachable"] if type(raw.get("reachable")) is bool else None, impact="availability")]
        components.append(component(source_id, "api", service["resource_id"], [sample], checks,
                                    service_id=service["service_id"], resource_scope="service", now=now))
    for kind, raw in (("database", native.get("database", {})), ("redis", redis)):
        raw = dict(raw or {})
        if kind == "database" and native.get("observed_at"):
            raw.update(observed_at=native["observed_at"], latency_seconds=native["latency_seconds"], reachable=native["reachable"])
        sample = make(kind, "default", "native", raw, source=kind)
        slots = [sample]
        checks = [predicate(kind, "default", "availability", sample, not raw["reachable"] if type(raw.get("reachable")) is bool else None, impact="availability"),
                  predicate(kind, "default", "latency_ge_1_second", sample, pressured(raw.get("latency_seconds"), 1))]
        if kind == "redis":
            total, used = raw.get("maxmemory"), raw.get("used_memory")
            ratio = used * 100 / total if type(total) in (int, float) and total > 0 and type(used) in (int, float) else None
            checks.append(predicate(kind, "default", "memory_ge_90_percent", sample, pressured(ratio)))
        prior = next((item for item in old.get(kind + ":default", {}).get("evidence", []) if item["slot_id"] == "native"), {})
        for counter in (("deadlocks",) if kind == "database" else ("evicted_keys", "rejected_connections")):
            previous_value, current_value = (prior.get("value") or {}).get(counter), raw.get(counter)
            incarnation_key = "incarnation" if kind == "database" else "run_id"
            same = raw.get(incarnation_key) and raw.get(incarnation_key) == (prior.get("value") or {}).get(incarnation_key)
            ordered = parse_time(prior.get("observed_at")) is not None and parse_time(raw.get("observed_at")) is not None and parse_time(raw["observed_at"]) > parse_time(prior["observed_at"])
            delta = current_value - previous_value if same and ordered and type(current_value) in (int, float) and type(previous_value) in (int, float) and current_value >= previous_value else None
            checks.append(predicate(kind, "default", counter + "_positive_delta", sample, delta > 0 if delta is not None else None))
        requests = [item for item in (managed or {}).get("series", []) if item["kind"] == kind]
        for request in requests:
            metric = ((fleet.get("cloud") or {}).get("extra") or {}).get(request["key"]) or {"status": "unknown", "reason": "managed_metric_unavailable"}
            metric_slot = make(kind, "default", "managed:" + digest(request["key"])[:32], metric, grant="aws", source="cloudwatch", value=metric.get("value"), identity={"resource": request["resource"], "metric": request["metric"]})
            slots.append(metric_slot)
            if request["unit"] == "Percent":
                checks.append(predicate(kind, "default", request["metric"] + "_ge_90_percent", metric_slot, pressured(metric.get("value")), resource_key=request["resource"]))
        if not requests:
            slots.append(make(kind, "default", "managed_pressure", {"status": "unknown", "reason": "managed_resource_unresolved"}, grant="aws", source="cloudwatch"))
        components.append(component(source_id, kind, "default", slots, checks, now=now, identity_grant="platform"))
    slots, checks = [], []
    for row in fleet.get("rows", []):
        if not row["expected"]:
            continue
        node_id = row["node_id"]
        detail = fleet.get("evidence", {}).get(node_id, {})
        current = detail.get("current") or {}
        families = [("cpu", current.get("cpu")), ("memory", current.get("memory"))]
        fs = current.get("filesystems") or {}
        families.append(("filesystem_inventory", fs))
        for mount in fs.get("mounts", []) + fs.get("missing", []):
            families.append(("filesystem:" + mount["path"], mount))
            families.append(("inodes:" + mount["path"], mount.get("inodes") or {"status": "unknown", "reason": "inodes_unavailable"}))
        for key, raw in families:
            raw = raw or {"status": "unknown", "reason": current.get("reason", "collector_missing")}
            sample = make("fleet", "installation", "runtime:" + digest([node_id, key])[:32], raw, grant="jobs", source="runtime", identity={"node_id": node_id, "measurement": key})
            slots.append(sample)
            percent = raw.get("percent")
            if key.startswith("inodes"):
                total, free = raw.get("total"), raw.get("free")
                percent = (total - free) * 100 / total if total and type(free) in (int, float) else None
            if key != "filesystem_inventory":
                checks.append(predicate("fleet", "installation", key.split(":")[0] + "_ge_90_percent", sample, pressured(percent), resource_key=digest([node_id, key])))
        trends = detail.get("trends") or {}
        for key, raw in [("cpu", trends.get("cpu")), ("memory", trends.get("memory"))] + [("filesystem:" + item.get("path", "/"), item) for item in trends.get("filesystems", [])]:
            raw = raw or {"status": "unknown", "reason": "cloudwatch_unavailable"}
            sample = make("fleet", "installation", "cloud:" + digest([node_id, key])[:32], raw, grant="aws", source="cloudwatch", value=raw.get("value"), identity={"node_id": node_id, "measurement": key})
            slots.append(sample)
            checks.append(predicate("fleet", "installation", "cloud_" + key.split(":")[0] + "_ge_90_percent", sample, pressured(raw.get("value")), resource_key=digest([node_id, key])))
        presence = make("fleet", "installation", "presence:" + digest(node_id)[:32], {"status": "ok", "observed_at": fleet.get("inventory", {}).get("observed_at"), "value": row.get("provider_state")}, grant="aws", source="ec2", identity={"node_id": node_id, "instance_id": row.get("instance_id")})
        slots.append(presence)
        checks.append(predicate("fleet", "installation", "provider_stopped", presence, row.get("provider_state") in ("stopped", "stopping", "terminated", "shutting-down") if row.get("provider_state") else None, impact="availability", resource_key=digest(node_id)))
    if not slots:
        slots.append(make("fleet", "installation", "roster", {"status": "unknown", "reason": "expected_roster_unresolved"}))
    fleet_component = component(source_id, "fleet", "installation", slots, checks,
        coverage_complete=bool(fleet.get("expected", {}).get("complete") and not fleet.get("overflow")), now=now, identity_grant="platform")
    components.append(fleet_component)
    job_slots, job_checks = [], []
    by_channel = {row["channel"]: row for row in native.get("jobs", [])}
    for channel in configuration["channels"]:
        raw = by_channel.get(channel) or {"channel": channel, "due_pending": 0, "running": 0, "overruns": 0}
        raw = {**raw, "observed_at": native.get("observed_at"), "status": "ok" if native.get("observed_at") else "unknown"}
        queue = make("jobman", "installation", "queue:" + digest(channel)[:32], raw, grant="jobs", source="job_queue", identity={"channel": channel})
        job_slots.append(queue)
        job_checks.append(predicate("jobman", "installation", "runtime_overrun", queue, raw.get("overruns", 0) > 0 if raw.get("observed_at") else None, resource_key=channel))
        prior = next((row for row in old.get("jobman:installation", {}).get("evidence", []) if row["slot_id"] == queue["slot_id"]), None)
        progress = None
        if prior and raw.get("due_pending") and (prior.get("value") or {}).get("due_pending") and parse_time(prior.get("observed_at")) and parse_time(raw.get("observed_at")):
            duration = (parse_time(raw["observed_at"]) - parse_time(prior["observed_at"])).total_seconds()
            if duration >= 300:
                current_completed, previous_completed = parse_time(raw.get("latest_completed")), parse_time((prior.get("value") or {}).get("latest_completed"))
                progress = not current_completed or (previous_completed is not None and current_completed <= previous_completed)
        job_checks.append(predicate("jobman", "installation", "due_backlog_no_progress_300_seconds", queue, progress, resource_key=channel))
    for row in fleet.get("rows", []):
        if row["expected"]:
            heartbeat = make("jobman", "installation", "worker:" + digest(row["node_id"])[:32],
                {"status": "ok" if row.get("last_heartbeat") else "unknown", "observed_at": row.get("last_heartbeat"), "runner_id": row.get("runner_id")},
                source="heartbeat", identity={"node_id": row["node_id"], "runner_id": row.get("runner_id")})
            job_slots.append(heartbeat)
    job_slots.append(make("jobman", "installation", "scheduler_detector", {"status": "unknown", "reason": "detector_evidence_unavailable"}, source="independent_detector"))
    components.append(component(source_id, "jobman", "installation", job_slots, job_checks,
                                coverage_complete=configuration["channels_complete"], now=now, identity_grant="platform"))
    producer = make("monitoring", "producer", "attempt", {"observed_at": timestamp(now), "status": "ok"}, source="snapshot_producer",
                    value={"profile_version": PROFILE_VERSION, "roster_revision": fleet.get("roster_revision")})
    components.append(component(source_id, "monitoring", "producer", [producer], [],
        coverage_complete=all(row["coverage"]["complete"] for row in components), now=now, identity_grant="platform"))
    return components
