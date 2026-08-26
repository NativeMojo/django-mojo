"""Pure pool identity, stats, state, aggregation, and signal contracts."""

import json
import os
import socket
import tempfile
import threading
import time
from pathlib import Path

from testit import helpers as th


def _identity(**overrides):
    value = {
        "project": "mojoland",
        "node": "node-a",
        "application": "abc123",
        "deployment": "lab-1",
    }
    value.update(overrides)
    return value


@th.unit_test("pool telemetry: application identity is strict, bounded, and stable")
def test_application_identity(opts):
    from mojo.db.pool_identity import PoolIdentityError, application_name

    value = application_name(_identity(), "api", "default", pid=42)
    assert value == "mojo|mojoland|node-a|api|default|abc123|lab-1|p42", \
        f"short identity must remain directly attributable, got {value!r}"
    long_value = application_name(_identity(
        project="project-" + "a" * 90,
        application="revision-" + "b" * 90,
    ), "api", "default", pid=42)
    assert len(long_value.encode("ascii")) <= 63, \
        f"PostgreSQL application_name must fit 63 ASCII bytes, got {long_value!r}"
    assert long_value == application_name(_identity(
        project="project-" + "a" * 90,
        application="revision-" + "b" * 90,
    ), "api", "default", pid=42), \
        "the same deployment identity must produce the same bounded name"

    try:
        application_name(_identity(node="node a"), "api", "default", pid=42)
    except PoolIdentityError:
        pass
    else:
        raise AssertionError("unsafe or ambiguous identity text must fail closed")


@th.unit_test("pool telemetry: public gauges distinguish full idle from exhaustion")
def test_truthful_public_states(opts):
    from mojo.db.pool_telemetry import classify, normalize_public_stats, pool_snapshot

    idle = normalize_public_stats({
        "pool_min": 1, "pool_max": 4, "pool_size": 4,
        "pool_available": 4, "requests_waiting": 0,
    })
    assert idle["full_sized_idle"] is True and idle["in_use_or_preparing"] == 0, \
        f"a full-sized idle pool must not look exhausted, got {idle!r}"
    assert classify(dict(idle, interval={})) == "healthy_idle", \
        f"full and available must classify healthy-idle, got {idle!r}"

    exhausted = normalize_public_stats({
        "pool_min": 1, "pool_max": 4, "pool_size": 4,
        "pool_available": 0, "requests_waiting": 2,
    })
    assert exhausted["in_use_or_preparing"] == 4, \
        f"allocated-minus-available is an upper bound, got {exhausted!r}"
    assert classify(dict(exhausted, interval={})) == "exhausted", \
        f"no availability plus real waiters must classify exhausted, got {exhausted!r}"

    class StatsOnlyPool:
        def get_stats(self):
            return {"pool_min": 1, "pool_max": 4, "pool_size": 0, "pool_available": 0}

        def open(self):
            raise AssertionError("sampling must not open Django's lazy pool")

        def getconn(self):
            raise AssertionError("sampling must not acquire a database lease")

    sampled = pool_snapshot(StatsOnlyPool(), now=10)
    assert sampled["state"] == "cold", \
        f"public-stat sampling must observe a closed lazy pool without opening it, got {sampled!r}"


@th.unit_test("pool telemetry: missing counters are zero and resets produce honest deltas")
def test_counter_normalization_and_reset(opts):
    from mojo.db.pool_telemetry import (
        normalize_public_stats, reset_safe_delta, should_emit_state_event)

    normalized = normalize_public_stats({"requests_num": 10, "future_key": 99})
    assert normalized["connections_lost"] == 0, \
        f"version-soft missing public counters must normalize to zero, got {normalized!r}"
    delta = reset_safe_delta(
        {"requests_num": 3, "requests_queued": 8},
        {"requests_num": 10, "requests_queued": 6},
    )
    assert delta["requests_num"] == 3 and delta["requests_queued"] == 2, \
        f"counter reset and ordinary growth must both be represented honestly, got {delta!r}"
    assert should_emit_state_event("exhausted", "busy", 99, 100), \
        "a state transition must emit immediately"
    assert not should_emit_state_event("exhausted", "exhausted", 99, 100), \
        "unchanged exhaustion must not emit on every sample"
    assert should_emit_state_event("exhausted", "exhausted", 100, 400), \
        "persistent exhaustion must emit only its bounded reminder"


@th.unit_test("pool telemetry: lab lease tracing pairs acquire and return without retaining connections")
def test_lab_lease_tracker(opts):
    from mojo.db.pool_telemetry import LeaseTracker

    class Connection:
        pass

    ticks = iter((10.0, 10.25, 11.0, 11.5))
    tracker = LeaseTracker(
        pid=42,
        monotonic_clock=lambda: next(ticks),
        wall_clock=lambda: 100.0,
        stack_factory=lambda: ("auth.py:validate:10", "base.py:get_new_connection:20"),
    )
    connection = Connection()
    with tempfile.TemporaryDirectory() as root:
        sink = Path(root) / "leases.jsonl"
        lease_id = tracker.acquired(
            connection, alias="default", label="group-apikey-me", sink_path=sink)
        active = tracker.snapshot()
        assert active["count"] == 1 and active["oldest_seconds"] == 0.25, \
            f"one checked-out lease must be visible without retaining its connection, got {active!r}"
        assert active["leases"][0]["lease_id"] == lease_id, \
            f"the active snapshot must correlate with the acquire event, got {active!r}"
        assert active["leases"][0]["label"] == "group-apikey-me", \
            f"the sandbox trace must identify the allowlisted DB-backed route, got {active!r}"

        tracker.returning(connection, sink_path=sink)
        tracker.returned(connection, sink_path=sink)
        released = tracker.snapshot()
        events = [json.loads(line) for line in sink.read_text().splitlines()]
        assert released["count"] == 0 and released["oldest_seconds"] == 0, \
            f"a matching Django close must remove the active lease, got {released!r}"
        assert [event["phase"] for event in events] == ["acquired", "returning", "returned"], \
            f"the local trace must prove the complete lifecycle, got {events!r}"
        assert all(event["lease_id"] == lease_id for event in events), \
            f"all phases must use one nonsecret correlation id, got {events!r}"
        assert all("connection" not in event for event in events), \
            f"lease evidence must not serialize connection objects or credentials, got {events!r}"
        assert all(event["thread_kind"] in {"main", "thread-pool", "mojo-runtime", "other"}
                   for event in events), \
            f"thread evidence must use bounded server-owned categories, got {events!r}"


@th.unit_test("pool telemetry: a returned connection cannot orphan a lease after immediate reuse")
def test_lab_lease_tracker_immediate_reuse(opts):
    from mojo.db.pool_telemetry import LeaseTracker

    connection = object()
    tracker = LeaseTracker(pid=44, stack_factory=lambda: ())
    first_lease = tracker.acquired(connection, alias="default", label="first")
    return_token = tracker.return_token(connection)
    tracker.returning(connection)

    second_lease = tracker.acquired(connection, alias="default", label="second")
    tracker.returned(connection, lease_id=return_token)
    active = tracker.snapshot()
    assert active["count"] == 1 and active["leases"][0]["lease_id"] == second_lease, \
        ("finishing the first return after psycopg immediately reuses the same "
         f"connection must preserve the second lease, got {active!r}")
    assert active["leases"][0]["phase"] == "acquired", \
        f"the replacement lease must remain active, got {active!r}"

    tracker.returning(connection)
    tracker.returned(connection, lease_id=second_lease)
    assert tracker.snapshot()["count"] == 0, \
        "the replacement lease must still complete normally"
    assert first_lease != second_lease, \
        "each checkout of a reused connection object needs a distinct correlation id"


@th.unit_test("pool telemetry: failed lease returns remain visible for diagnosis")
def test_lab_lease_tracker_failed_return(opts):
    from mojo.db.pool_telemetry import LeaseTracker

    connection = object()
    ticks = iter((20.0, 20.5, 21.0))
    tracker = LeaseTracker(
        pid=43,
        monotonic_clock=lambda: next(ticks),
        wall_clock=lambda: 200.0,
        stack_factory=lambda: (),
    )
    tracker.acquired(connection, alias="default")
    tracker.returning(connection)
    tracker.return_failed(connection, RuntimeError("secret database detail"))
    active = tracker.snapshot()
    assert active["count"] == 1 and active["leases"][0]["phase"] == "return_failed", \
        f"a failed return must stay active instead of manufacturing recovery, got {active!r}"
    assert active["leases"][0]["error_type"] == "RuntimeError", \
        f"the local failure must record only its class, got {active!r}"
    assert "secret database detail" not in json.dumps(active), \
        f"return exceptions must not expose database details, got {active!r}"


@th.unit_test("pool telemetry: route labels and activation are sandbox allowlisted")
def test_lab_lease_trace_allowlist(opts):
    from mojo.db.pool_telemetry import lease_trace_allowed, request_trace_label

    assert request_trace_label("/api/group/apikey/me") == "group-apikey-me", \
        "the API-key identity exercise must have a stable server-owned label"
    assert request_trace_label("/api/group/123") == "group-detail", \
        "numeric group detail must omit the caller-controlled id"
    assert request_trace_label("/api/filevault/download/secret-token") is None, \
        "non-exercise paths must never enter lease evidence"
    plan = {"enabled": True, "valid": True, "role": "api", "launcher": "asgi",
            "identity": {"project": "mojoland"}}
    assert lease_trace_allowed(True, True, plan), \
        "the exact MojoLand API/probe lab identity must enable tracing"
    for candidate, probe, changed in (
        (1, True, {}), (True, False, {}), (True, True, {"enabled": False}),
        (True, True, {"project": "customer"}), (True, True, {"role": "jobs"}),
    ):
        value = dict(plan)
        if "project" in changed:
            value["identity"] = {"project": changed["project"]}
        else:
            value.update(changed)
        assert not lease_trace_allowed(candidate, probe, value), \
            f"non-lab trace activation must fail closed: {candidate!r}, {probe!r}, {value!r}"


@th.unit_test("pool telemetry: trace sink failures never escape into database lifecycle")
def test_lab_lease_trace_is_fail_open(opts):
    from mojo.db.pool_telemetry import (
        active_lease_snapshot,
        record_lease_acquired,
        record_lease_returned,
        record_lease_return_failed,
        record_lease_returning,
    )

    class BrokenTracker:
        def acquired(self, *args, **kwargs):
            raise OSError("trace sink unavailable")

        def returning(self, *args, **kwargs):
            raise OSError("trace sink unavailable")

        def returned(self, *args, **kwargs):
            raise OSError("trace sink unavailable")

        def return_failed(self, *args, **kwargs):
            raise OSError("trace sink unavailable")

        def snapshot(self):
            raise OSError("trace sink unavailable")

    tracker = BrokenTracker()
    connection = object()
    assert record_lease_acquired(connection, "default", tracker=tracker) is None, \
        "an acquire trace failure must disappear instead of leaking a checked-out connection"
    assert record_lease_returning(connection, tracker=tracker) is None, \
        "a pre-return trace failure must never prevent the real pool return"
    assert record_lease_returned(connection, tracker=tracker) is None, \
        "a post-return trace failure must not change a successful close"
    assert record_lease_return_failed(connection, "return error", tracker=tracker) is None, \
        "a failed-return trace failure must preserve the original database error"
    assert active_lease_snapshot(tracker=tracker) == {
        "count": 0, "oldest_seconds": 0, "leases": [], "trace_error": True,
    }, "a snapshot trace failure must degrade locally without blocking ASGI startup"


@th.unit_test("pool telemetry: private event append refuses symlink targets")
def test_private_event_sink_refuses_symlink(opts):
    from mojo.db.pool_telemetry import append_bounded_event

    with tempfile.TemporaryDirectory() as root:
        protected = Path(root) / "protected"
        protected.write_text("unchanged")
        sink = Path(root) / "events.jsonl"
        sink.symlink_to(protected)
        try:
            append_bounded_event(sink, {"event": "test"})
        except OSError:
            pass
        else:
            raise AssertionError("a private event sink must never follow a pre-created symlink")
        assert protected.read_text() == "unchanged", \
            "rejecting the event sink must not alter the symlink target"


@th.unit_test("pool telemetry: aggregation ignores stale and duplicate worker snapshots")
def test_snapshot_aggregation(opts):
    from mojo.db.pool_telemetry import aggregate_snapshots, atomic_write

    with tempfile.TemporaryDirectory() as root:
        root_path = Path(root)
        atomic_write(root_path / "a.json", {
            "process_uuid": "worker-a", "at": 100, "state": "healthy_idle",
            "gauges": {"pool_min": 1, "pool_max": 4, "pool_size": 4,
                       "pool_available": 4, "requests_waiting": 0},
            "in_use_or_preparing": 0,
        })
        atomic_write(root_path / "a-new.json", {
            "process_uuid": "worker-a", "at": 101, "state": "exhausted",
            "gauges": {"pool_min": 1, "pool_max": 4, "pool_size": 4,
                       "pool_available": 0, "requests_waiting": 1},
            "in_use_or_preparing": 4,
        })
        atomic_write(root_path / "stale.json", {
            "process_uuid": "worker-old", "at": 20, "state": "healthy",
            "gauges": {}, "in_use_or_preparing": 0,
        })
        result = aggregate_snapshots(root_path.glob("*.json"), max_age=10, now=105)
        assert os.stat(root_path / "a.json").st_mode & 0o777 == 0o640, \
            "worker snapshots must be readable by the dedicated observer group only"
        assert result["workers"] == 1 and result["stale_files"] == 1, \
            f"fresh process UUIDs must aggregate once and stale files must be named, got {result!r}"
        assert result["exhausted"] is True and result["gauges"]["pool_available"] == 0, \
            f"the newest worker snapshot must drive aggregate state, got {result!r}"


@th.unit_test("pool telemetry: ASGI startup requires one synchronous real snapshot")
def test_runtime_startup_evidence_gate(opts):
    from mojo.db import pool_runtime

    events = []

    class Wrapper:
        pool = object()

    class Thread:
        def __init__(self, *args, **kwargs):
            pass

        def is_alive(self):
            return False

        def start(self):
            events.append("thread")

    class Runtime(pool_runtime.PoolRuntime):
        def enabled(self):
            return True

        def sample_once(self):
            events.append("sample")

    class BadRuntime(Runtime):
        def sample_once(self):
            raise RuntimeError("bad stats")

    with tempfile.TemporaryDirectory() as root:
        runtime = Runtime(
            root=root, connection_handler={"default": Wrapper()},
            thread_factory=Thread)
        assert runtime.start() is True
        assert events == ["sample", "thread"], \
            f"a valid snapshot must precede the sampler thread and startup success, got {events!r}"

        events.clear()
        runtime = BadRuntime(
            root=root, connection_handler={"default": Wrapper()},
            thread_factory=Thread)
        try:
            runtime.start()
        except RuntimeError as error:
            assert str(error) == "bad stats"
        else:
            raise AssertionError("invalid initial telemetry must fail ASGI startup")
        assert events == [], "a failed initial snapshot must not start a background sampler"


@th.unit_test("pool telemetry: acquisition errors emit once without a database logger")
def test_pool_error_signal_is_bounded_and_deduplicated(opts):
    from psycopg_pool import PoolTimeout, TooManyRequests
    from mojo.db.errors import emit_pool_error, is_pool_acquisition_error

    with tempfile.TemporaryDirectory() as root:
        target = Path(root) / "pool-error.json"
        error = PoolTimeout("secret-ish " + "x" * 400)
        first = emit_pool_error(error, path="/api/test", sink_path=target)
        second = emit_pool_error(error, path="/api/test", sink_path=target)
        payload = json.loads(target.read_text())
        assert first is True and second is False, \
            "one exception crossing several handlers must emit only once"
        assert payload["event"] == "database_pool_acquisition_error", \
            f"the local signal must identify the bounded event, got {payload!r}"
        assert len(payload["error"]) <= 160 and "traceback" not in payload, \
            f"the DB-independent signal must stay bounded and omit internals, got {payload!r}"
        assert is_pool_acquisition_error(TooManyRequests("queue is full")), \
            "the public bounded-queue failure must use the same nonrecursive error path"


@th.django_unit_test("pool telemetry: HTTP error boundaries return 503 without ORM recursion")
def test_http_pool_timeout_returns_503(opts):
    from django.test import RequestFactory
    from objict import objict
    from psycopg_pool import PoolTimeout
    from mojo.decorators.http import dispatch_error_handler
    from mojo.middleware.logging import LoggerMiddleware
    from mojo.middleware.database_pool import DatabasePoolErrorMiddleware

    request = RequestFactory().get("/api/pool-test", HTTP_ACCEPT="application/json")
    request.DATA = objict()
    request._raw_body = b""

    def fail(_request):
        error = PoolTimeout("expected test timeout")
        error._mojo_pool_reported = True
        raise error

    response = dispatch_error_handler(fail)(request)
    payload = json.loads(response.content)
    assert response.status_code == 503 and payload["code"] == 503, \
        f"REST acquisition timeout must return bounded 503, got {response.status_code}: {payload!r}"
    assert request._mojo_pool_acquisition_error is True, \
        "the request must suppress ORM response logging after pool exhaustion"

    del request._mojo_pool_acquisition_error
    response = LoggerMiddleware(fail)(request)
    payload = json.loads(response.content)
    assert response.status_code == 503 and payload["code"] == 503, \
        f"outer middleware acquisition timeout must return bounded 503, got {response.status_code}: {payload!r}"
    assert request._mojo_pool_acquisition_error is True, \
        "the outer boundary must suppress ORM response logging after pool exhaustion"

    del request._mojo_pool_acquisition_error
    view_error = PoolTimeout("expected view timeout")
    view_error._mojo_pool_reported = True
    response = DatabasePoolErrorMiddleware(lambda _request: None).process_exception(
        request, view_error)
    payload = json.loads(response.content)
    assert response.status_code == 503 and payload["code"] == 503, \
        f"view-time acquisition timeout must return 503, got {response.status_code}: {payload!r}"
    assert response["Retry-After"] == "1", \
        "bounded acquisition failures must tell clients when to retry"
    assert request._mojo_pool_acquisition_error is True, \
        "the dedicated outer boundary must mark the acquisition failure"

    from django.core.handlers.exception import convert_exception_to_response
    from mojo.middleware.auth import AuthenticationMiddleware

    def fail_auth(_token, _request):
        error = PoolTimeout("expected authentication timeout")
        error._mojo_pool_reported = True
        raise error

    auth_request = RequestFactory().get(
        "/api/group/apikey/me",
        HTTP_ACCEPT="application/json",
        HTTP_AUTHORIZATION="apikey test-token",
    )
    authentication = AuthenticationMiddleware(
        lambda _request: None,
        handler_cache={"apikey": fail_auth},
        handler_paths={},
        bearer_name_map={"apikey": "user"},
    )
    response = convert_exception_to_response(authentication)(auth_request)
    payload = json.loads(response.content)
    assert response.status_code == 503 and payload["code"] == 503, \
        f"Django's middleware exception wrapper must receive a 503 from authentication, got {response.status_code}: {payload!r}"
    assert response["Retry-After"] == "1", \
        "the real authentication response must carry bounded retry guidance"

    html_request = RequestFactory().get("/html", HTTP_ACCEPT="text/html")
    html_error = PoolTimeout("expected HTML timeout")
    html_error._mojo_pool_reported = True
    response = DatabasePoolErrorMiddleware(lambda _request: None).process_exception(
        html_request, html_error)
    assert response["Content-Type"].startswith("application/json"), \
        "the emergency response must bypass DB-backed HTML branding and templates"

    import mojo.middleware.logging as logging_middleware
    now = time.monotonic()
    logging_middleware._suspend_db_logging(now=now)
    assert logging_middleware._db_logging_suspended(now=now + 29), \
        "a DB logger pool failure must suppress repeated queue acquisitions"
    assert not logging_middleware._db_logging_suspended(now=now + 30), \
        "the DB logger circuit breaker must recover without restart"


@th.unit_test("pool telemetry: local exercise is cancellable and returns every lease")
def test_local_probe_cancel_and_recovery(opts):
    import mojo.db.pool_runtime as pool_runtime

    class Pool:
        returned = 0

        def __init__(self):
            self.owned = 0

        def get_stats(self):
            return {
                "pool_max": 2,
                "pool_size": 2,
                "pool_available": 2 - self.owned,
                "requests_waiting": 0,
            }

        def getconn(self, timeout=None):
            from psycopg_pool import PoolTimeout
            if self.owned >= 2:
                raise PoolTimeout("expected controlled waiter timeout")
            self.owned += 1
            return object()

        def putconn(self, connection):
            self.owned -= 1
            self.returned += 1

    class Wrapper:
        pool = Pool()

    def request(path, payload):
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(3)
        try:
            client.connect(str(path))
            client.sendall(json.dumps(payload).encode("utf-8"))
            return json.loads(client.recv(4096).decode("utf-8"))
        finally:
            client.close()

    with tempfile.TemporaryDirectory() as root:
        path = Path(root) / "probe.sock"
        probe = pool_runtime.LocalPoolProbe(
            path, deadline=10, connection_handler={"default": Wrapper()})
        probe._query = lambda: 1
        server = threading.Thread(target=probe.serve_forever, daemon=True)
        server.start()
        for _attempt in range(100):
            if path.exists():
                break
            time.sleep(0.01)
        result = {}
        exercise = threading.Thread(
            target=lambda: result.update(request(path, {
                "command": "exercise", "leases": 2, "hold_seconds": 5,
            })),
            daemon=True,
        )
        exercise.start()
        time.sleep(0.05)
        cancelled = request(path, {"command": "cancel"})
        exercise.join(timeout=2)
        probe.stop()
        server.join(timeout=2)

    assert cancelled == {"ok": True, "cancelled": True}, \
        f"cancel must be accepted while exercise holds leases, got {cancelled!r}"
    assert result.get("recovered_without_restart") is True, \
        f"exercise must prove recovery without a process restart, got {result!r}"
    assert result.get("waiter_timeout") is True, \
        f"exercise must create exactly one bounded waiter timeout, got {result!r}"
    assert result.get("pool_stats_recovered") is True and result.get("return_errors") == [], \
        f"recovery must include confirmed public stats and every successful return, got {result!r}"
    assert Wrapper.pool.returned == 2, \
        f"every deliberately held lease must return, got {Wrapper.pool.returned}"

    class FailingReturnPool:
        acquired = False

        def get_stats(self):
            return {
                "pool_max": 1, "pool_size": 1, "pool_available": 1,
                "requests_waiting": 0,
            }

        def getconn(self, timeout=None):
            from psycopg_pool import PoolTimeout
            if self.acquired:
                raise PoolTimeout("expected controlled waiter timeout")
            self.acquired = True
            return object()

        def putconn(self, connection):
            raise RuntimeError("deliberate return failure")

    class FailingWrapper:
        pool = FailingReturnPool()

    failed_probe = pool_runtime.LocalPoolProbe(
        "/tmp/unused-pool-probe.sock", deadline=1,
        connection_handler={"default": FailingWrapper()})
    failed_probe._query = lambda: 1
    failed = failed_probe.exercise(1, hold_seconds=0)
    assert failed["ok"] is False and failed["return_errors"], \
        f"a failed lease return must never produce false-green recovery, got {failed!r}"


@th.unit_test("pool telemetry: local SQL failure fixtures recover without leaking details")
def test_local_probe_sql_failure_fixtures(opts):
    from django.db import DatabaseError
    from mojo.db.pool_runtime import LocalPoolProbe

    class Cursor:
        def __init__(self, error=False):
            self.error = error
            self.executed = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, statement, params=None):
            self.executed.append((statement, params))
            if self.error:
                raise DatabaseError("deliberate secret fixture detail")

    class Wrapper:
        def __init__(self):
            self.cursors = []
            self.fail_next = False
            self.rollbacks = 0

        def cursor(self):
            cursor = Cursor(error=self.fail_next)
            self.fail_next = False
            self.cursors.append(cursor)
            return cursor

        def rollback(self):
            self.rollbacks += 1

    wrapper = Wrapper()
    probe = LocalPoolProbe(
        "/tmp/unused-pool-fixture.sock", deadline=3,
        connection_handler={"default": wrapper})
    probe._query = lambda: 1

    slow = probe.slow_query(0.05)
    assert slow["ok"] is True and slow["fixture"] == "slow_query", \
        f"the bounded slow query must recover cleanly, got {slow!r}"
    assert wrapper.cursors[0].executed == [("SELECT pg_sleep(%s)", [0.05])], \
        f"the fixture must use a parameterized bounded sleep, got {wrapper.cursors[0].executed!r}"

    wrapper.fail_next = True
    failed = probe.transaction_error()
    assert failed["ok"] is True and failed["expected_database_error"] is True, \
        f"the deliberate statement error must be observed and recovered, got {failed!r}"
    assert failed["recovered_without_restart"] is True and wrapper.rollbacks == 1, \
        f"the failed transaction must roll back before the fresh query, got {failed!r}"
    assert "secret fixture detail" not in json.dumps(failed), \
        f"the local response must expose error classes only, got {failed!r}"
