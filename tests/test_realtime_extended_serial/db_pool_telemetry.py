"""Real tiny-pool acquisition, evidence, thread cleanup, and recovery proof."""

import copy
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from unittest import mock

from testit import helpers as th


@contextmanager
def _observed_pool(max_size=1, timeout=0.2):
    from django.db import connections
    from django.db.backends.postgresql.base import DatabaseWrapper as DjangoWrapper
    from mojo.db.backends.postgresql.base import DatabaseWrapper

    alias = f"observed_pool_{uuid.uuid4().hex}"
    config = copy.deepcopy(connections.databases["default"])
    config["ENGINE"] = "mojo.db.backends.postgresql"
    config["CONN_MAX_AGE"] = 0
    config["CONN_HEALTH_CHECKS"] = False
    config["OPTIONS"] = {
        "application_name": f"mojo-test-{alias[:20]}",
        "pool": {
            "min_size": 1,
            "max_size": max_size,
            "timeout": timeout,
            "max_waiting": 1,
        },
    }
    config["TEST"] = dict(config.get("TEST") or {})
    connections.databases[alias] = config
    DatabaseWrapper._connection_pools.pop(alias, None)
    DjangoWrapper._connection_pools.pop(alias, None)
    pool = connections[alias].pool
    pool.open()
    pool.wait(timeout=3)
    try:
        yield alias, pool
    finally:
        try:
            connections[alias].close()
        except Exception:
            pass
        try:
            pool.close()
        except Exception:
            pass
        try:
            del connections[alias]
        except Exception:
            pass
        connections.databases.pop(alias, None)
        DatabaseWrapper._connection_pools.pop(alias, None)
        DjangoWrapper._connection_pools.pop(alias, None)


def _query(alias, value=1):
    from django.db import connections

    with connections[alias].cursor() as cursor:
        cursor.execute("SELECT %s", [value])
        return cursor.fetchone()[0]


def _wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


@th.django_unit_test("observed backend records exact timeout and recovers without restart")
def test_observed_timeout_and_recovery(opts):
    from psycopg_pool import PoolTimeout
    from mojo.db.errors import is_pool_acquisition_error
    from mojo.db.pool_telemetry import observed_counters, pool_snapshot
    from mojo.helpers.async_db import database_thread_target

    with _observed_pool() as (alias, pool):
        before = observed_counters()
        held = pool.getconn(timeout=1)
        result = {}

        def wait_for_pool():
            try:
                _query(alias)
            except Exception as error:
                result["error"] = error

        worker = threading.Thread(target=database_thread_target(wait_for_pool))
        worker.start()
        assert _wait_for(lambda: pool.get_stats().get("requests_waiting", 0) == 1), \
            f"the tiny pool must expose one real waiter, got {pool.get_stats()!r}"
        waiting = pool_snapshot(pool)
        assert waiting["state"] == "exhausted", \
            f"no availability plus a waiter must be exhausted, got {waiting!r}"
        worker.join(timeout=3)
        assert not worker.is_alive(), "the bounded acquisition must time out"
        wrapped = result.get("error")
        assert is_pool_acquisition_error(wrapped), \
            f"Django's OperationalError wrapper must retain the exact pool cause, got {result!r}"
        assert isinstance(wrapped.__cause__ or wrapped.__context__, PoolTimeout), \
            f"the thin backend must re-raise PoolTimeout unchanged before Django wraps it, got {result!r}"
        after_timeout = observed_counters()
        assert after_timeout["observed_acquisition_timeouts"] == \
            before["observed_acquisition_timeouts"] + 1, \
            f"the thin backend must count the exact timeout once, got {after_timeout!r}"

        pool.putconn(held)
        recovered = {}

        def fresh_query():
            recovered["value"] = _query(alias, 7)

        success = threading.Thread(target=database_thread_target(fresh_query))
        success.start()
        success.join(timeout=3)
        assert not success.is_alive() and recovered.get("value") == 7, \
            f"a fresh query must recover without restart, got {recovered!r}"
        stats = pool.get_stats()
        assert stats.get("pool_available") == stats.get("pool_size"), \
            f"all leases must return after recovery, got {stats!r}"
        assert stats.get("requests_waiting", 0) == 0, \
            f"the recovered pool must have no stranded waiters, got {stats!r}"


@th.django_unit_test("raw ORM thread boundary returns leases after success and failure")
def test_raw_thread_boundary_returns_every_lease(opts):
    from mojo.helpers.async_db import database_thread_target

    with _observed_pool(max_size=2) as (alias, pool):
        failures = []
        for index in range(8):
            def work(value=index):
                result = _query(alias, value)
                if value % 2:
                    raise RuntimeError(f"expected-{value}")
                return result

            def run():
                try:
                    work()
                except RuntimeError as error:
                    failures.append(str(error))

            worker = threading.Thread(target=database_thread_target(run))
            worker.start()
            worker.join(timeout=3)
            assert not worker.is_alive(), f"ORM worker {index} failed to exit"

        stats = pool.get_stats()
        assert len(failures) == 4, f"all deliberate failures must execute, got {failures!r}"
        assert stats.get("pool_available") == stats.get("pool_size"), \
            f"reused raw threads must return every pool lease, got {stats!r}"
        assert stats.get("requests_waiting", 0) == 0, \
            f"repeated thread work must not leave waiters, got {stats!r}"


@th.django_unit_test(
    "HTTP pool boundary returns a 404 lease before the ASGI sync thread is reused")
def test_http_request_boundary_returns_lease_on_response(opts):
    from django.http import HttpResponseNotFound
    from django.test import RequestFactory
    from mojo.middleware.database_pool import DatabasePoolErrorMiddleware

    with _observed_pool() as (alias, pool):
        request = RequestFactory().get("/wp-admin/install.php?step=1")

        def missing(_request):
            assert _query(alias) == 1, \
                "the representative 404 request must execute real PostgreSQL work"
            return HttpResponseNotFound("missing")

        boundary = DatabasePoolErrorMiddleware(missing)
        with ThreadPoolExecutor(max_workers=1) as executor:
            response = executor.submit(boundary, request).result(timeout=3)
            stats = pool.get_stats()
            assert response.status_code == 404, \
                f"the request must retain its original response, got {response.status_code}"
            assert stats.get("pool_available") == stats.get("pool_size"), \
                f"the outer HTTP boundary must return its same-thread lease, got {stats!r}"
            assert stats.get("requests_waiting", 0) == 0, \
                f"the completed 404 must not strand a waiter, got {stats!r}"


@th.unit_test("API-resident ORM thread sites use the shared lifecycle boundary")
def test_api_thread_source_audit(opts):
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    handler = (root / "mojo/apps/assistant/handler.py").read_text()
    agent = (root / "mojo/apps/assistant/services/agent.py").read_text()
    logging = (root / "mojo/middleware/logging.py").read_text()
    assert handler.count("target=database_thread_target(") == 2, \
        "both assistant-owned raw ORM threads must use the shared lifecycle boundary"
    assert agent.count("submit_database_work(") == 2, \
        "both assistant tool executor paths must wrap ORM-capable submissions"
    assert "with database_connection_boundary():" in logging, \
        "the long-lived request DB logger must close connections around every queue item"


@th.unit_test("realtime acquisition timeout closes WebSocket with retry-later 1013")
def test_websocket_pool_timeout_is_1013(opts):
    import asyncio
    from psycopg_pool import PoolTimeout
    from mojo.apps.realtime.asgi import ASGIApplication
    from mojo.apps.realtime import handler as realtime_handler

    class FailingHandler:
        def __init__(self, websocket, path):
            self.websocket = websocket
            self.path = path

        async def handle_connection(self):
            error = PoolTimeout("expected socket timeout")
            error._mojo_pool_reported = True
            raise error

    async def allowed(_scope):
        return True

    received = [{"type": "websocket.disconnect"}]
    sent = []

    async def receive():
        return received.pop(0)

    async def send(message):
        sent.append(message)

    scope = {"type": "websocket", "path": "/ws/realtime/", "headers": [],
             "client": ("127.0.0.1", 1234)}
    with mock.patch.object(realtime_handler, "WebSocketHandler", FailingHandler), \
            mock.patch.object(realtime_handler, "check_connect_rate", allowed):
        asyncio.run(ASGIApplication()(scope, receive, send))
    closes = [message for message in sent if message.get("type") == "websocket.close"]
    assert closes and closes[-1]["code"] == 1013, \
        f"pool acquisition failure must tell WebSocket clients to retry later, got {sent!r}"
