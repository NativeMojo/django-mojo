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
