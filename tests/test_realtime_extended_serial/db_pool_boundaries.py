"""Pool lifecycle regressions for long-lived realtime ASGI workers.

These tests deliberately use real PostgreSQL pools and process-local executor
state, so they live in the existing opt-in serial realtime package.
"""

import asyncio
import copy
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from testit import helpers as th


@contextmanager
def _pooled_alias(max_size):
    from django.db import connections
    from django.db.backends.postgresql.base import DatabaseWrapper

    alias = f"realtime_pool_{uuid.uuid4().hex}"
    config = copy.deepcopy(connections.databases["default"])
    config["CONN_MAX_AGE"] = 0
    config["CONN_HEALTH_CHECKS"] = False
    config["OPTIONS"] = {
        "pool": {
            "min_size": 1,
            "max_size": max_size,
            "timeout": 2,
        },
    }
    config["TEST"] = dict(config.get("TEST") or {})
    connections.databases[alias] = config
    DatabaseWrapper._connection_pools.pop(alias, None)
    pool = connections[alias].pool
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


def _query(alias, value=1):
    from django.db import connections

    with connections[alias].cursor() as cursor:
        cursor.execute("SELECT %s", [value])
        return cursor.fetchone()[0]


def _pool_stats(pool):
    pool.wait(timeout=3)
    stats = pool.get_stats()
    size = stats.get("pool_size", 0)
    available = stats.get("pool_available", 0)
    return {
        "pool_size": size,
        "pool_available": available,
        "checked_out": size - available,
        "requests_waiting": stats.get("requests_waiting", 0),
    }


def _wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


@th.django_unit_test()
def test_database_executor_returns_pool_leases_on_success_and_error(opts):
    from mojo.helpers.async_db import DatabaseExecutor

    executor = DatabaseExecutor(max_workers=1)
    try:
        with _pooled_alias(1) as (alias, pool):
            result = asyncio.run(executor.run(_query, alias, 7))
            assert result == 7, f"database worker must return the query result, got {result!r}"
            success_stats = _pool_stats(pool)
            assert success_stats["checked_out"] == 0, (
                f"successful work must return its lease, got {success_stats}"
            )
            assert success_stats["requests_waiting"] == 0, (
                f"successful work must leave no pool waiters, got {success_stats}"
            )

            def query_then_fail():
                _query(alias)
                raise RuntimeError("expected worker failure")

            try:
                asyncio.run(executor.run(query_then_fail))
            except RuntimeError as exc:
                assert str(exc) == "expected worker failure", (
                    f"worker exception must propagate unchanged, got {exc!r}"
                )
            else:
                raise AssertionError("database worker must propagate the callable's exception")

            error_stats = _pool_stats(pool)
            assert error_stats["checked_out"] == 0, (
                f"failed work must return its lease in finally, got {error_stats}"
            )
            assert error_stats["requests_waiting"] == 0, (
                f"failed work must leave no pool waiters, got {error_stats}"
            )
    finally:
        executor.shutdown()


@th.django_unit_test()
def test_database_executor_is_bounded_without_global_serialization(opts):
    from django.db import connections
    from mojo.helpers.async_db import DatabaseExecutor

    executor = DatabaseExecutor(max_workers=2)
    release = threading.Event()
    started = []
    started_lock = threading.Lock()

    try:
        with _pooled_alias(2) as (alias, pool):
            def blocking_query(value):
                with connections[alias].cursor() as cursor:
                    cursor.execute("SELECT %s", [value])
                    with started_lock:
                        started.append((value, threading.get_ident()))
                    release.wait(timeout=3)
                    return cursor.fetchone()[0]

            async def exercise_bound():
                first = asyncio.create_task(executor.run(blocking_query, 1))
                second = asyncio.create_task(executor.run(blocking_query, 2))
                assert await asyncio.to_thread(_wait_for, lambda: len(started) == 2), (
                    f"two dedicated DB workers must enter concurrently, got {started}"
                )
                third = asyncio.create_task(executor.run(blocking_query, 3))
                await asyncio.sleep(0.1)
                assert len(started) == 2, (
                    f"a two-worker executor must queue excess work, got {started}"
                )
                release.set()
                return await asyncio.gather(first, second, third)

            results = asyncio.run(exercise_bound())
            assert sorted(results) == [1, 2, 3], (
                f"all queued database work must eventually complete, got {results}"
            )
            first_two_threads = {thread_id for _value, thread_id in started[:2]}
            assert len(first_two_threads) == 2, (
                f"dedicated DB work must not serialize on one global thread, got {started}"
            )
            stats = _pool_stats(pool)
            assert stats["checked_out"] == 0 and stats["requests_waiting"] == 0, (
                f"concurrent work must leave the pool quiescent, got {stats}"
            )
    finally:
        release.set()
        executor.shutdown()


@th.django_unit_test()
def test_cancelled_waiter_returns_lease_after_worker_actually_exits(opts):
    from django.db import connections
    from mojo.helpers.async_db import DatabaseExecutor

    executor = DatabaseExecutor(max_workers=1)
    entered = threading.Event()
    release = threading.Event()
    exited = threading.Event()

    try:
        with _pooled_alias(1) as (alias, pool):
            def blocking_query():
                try:
                    with connections[alias].cursor() as cursor:
                        cursor.execute("SELECT 1")
                        entered.set()
                        release.wait(timeout=3)
                        return cursor.fetchone()[0]
                finally:
                    exited.set()

            async def cancel_waiter():
                task = asyncio.create_task(executor.run(blocking_query))
                assert await asyncio.to_thread(entered.wait, 3), (
                    "database callable must enter before its awaiter is cancelled"
                )
                busy = pool.get_stats()
                checked_out = busy.get("pool_size", 0) - busy.get("pool_available", 0)
                assert checked_out == 1, (
                    f"the running callable must still own its lease, got {busy}"
                )
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                else:
                    raise AssertionError("cancelling the awaiter must raise CancelledError")
                assert not exited.is_set(), (
                    "awaiter cancellation must not pretend the running sync callable was preempted"
                )
                release.set()
                assert await asyncio.to_thread(exited.wait, 3), (
                    "the test callable must really exit before pool quiescence is asserted"
                )

            asyncio.run(cancel_waiter())
            stats = _pool_stats(pool)
            assert stats["checked_out"] == 0 and stats["requests_waiting"] == 0, (
                f"the lease must return after the worker's finally runs, got {stats}"
            )
    finally:
        release.set()
        executor.shutdown()


@th.django_unit_test()
def test_realtime_identity_is_plain_and_rehydrated_for_each_hook(opts):
    from django.db.models import Model
    from mojo.apps.account.models import User
    from mojo.apps.realtime.db import run_identity_hook, serialize_identity

    email = f"pool_identity_{uuid.uuid4().hex[:10]}@example.test"
    User.objects.filter(username=email).delete()
    user = User.objects.create_user(username=email, email=email, password="PoolTest##1")
    try:
        descriptor = serialize_identity(user)
        assert isinstance(descriptor, dict), (
            f"the event loop must receive a plain identity dictionary, got {type(descriptor)!r}"
        )
        assert not isinstance(descriptor, Model), (
            f"a Django model must never cross back to the event loop, got {descriptor!r}"
        )
        assert descriptor["pk"] == str(user.pk), (
            f"identity descriptor must preserve the primary key, got {descriptor!r}"
        )
        allowed = asyncio.run(run_identity_hook(
            descriptor, "on_realtime_can_subscribe", f"user:{user.pk}"))
        assert allowed is True, (
            f"the hook must rehydrate the model inside the DB unit, got {allowed!r}"
        )

        unsaved = User(username=f"unsaved_{uuid.uuid4().hex}")
        try:
            serialize_identity(unsaved)
        except ValueError:
            pass
        else:
            raise AssertionError("unsaved identities must be rejected before reaching the event loop")
    finally:
        User.objects.filter(pk=user.pk).delete()


@th.django_unit_test()
def test_db_backed_realtime_setting_forces_real_sql(opts):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext
    from mojo.apps.account.models.setting import Setting
    from mojo.apps.realtime.db import run_database
    from mojo.helpers.redis import get_connection
    from mojo.helpers.settings import settings

    key = f"TESTIT_REALTIME_POOL_{uuid.uuid4().hex}"
    row = Setting(key=key, value="17", group=None)
    row.save(_skip_cache=True)
    redis = get_connection()
    redis.hdel(Setting._redis_key(), key)

    def read_uncached_setting():
        with CaptureQueriesContext(connection) as captured:
            value = settings.get(key, 0, kind="int")
        return value, [entry["sql"] for entry in captured.captured_queries]

    try:
        value, queries = asyncio.run(run_database(read_uncached_setting))
        assert value == 17, f"DB-backed setting must resolve its stored value, got {value!r}"
        selects = [sql for sql in queries if "SELECT" in sql.upper()]
        assert selects, f"forced Redis miss must execute a real SELECT, got {queries!r}"
        assert any("account_setting" in sql.lower() for sql in selects), (
            f"the real SELECT must target the Setting table, got {selects!r}"
        )
    finally:
        redis.hdel(Setting._redis_key(), key)
        Setting.objects.filter(pk=row.pk).delete()


@th.django_unit_test()
def test_worker_count_and_realtime_boundary_audit(opts):
    from mojo.apps.realtime.db import resolve_database_workers

    assert resolve_database_workers(None) == 4, "missing WS_DATABASE_WORKERS must default to 4"
    assert resolve_database_workers("garbage") == 4, "invalid WS_DATABASE_WORKERS must default to 4"
    assert resolve_database_workers(-10) == 1, "WS_DATABASE_WORKERS must clamp to at least 1"
    assert resolve_database_workers(99) == 32, "WS_DATABASE_WORKERS must clamp to at most 32"
    assert resolve_database_workers("8") == 8, "valid WS_DATABASE_WORKERS must preserve its value"

    root = Path(__file__).resolve().parents[2]
    auth_source = (root / "mojo/apps/realtime/auth.py").read_text()
    handler_source = (root / "mojo/apps/realtime/handler.py").read_text()
    assert "sync_to_async" not in auth_source, (
        "realtime authentication must use the bounded database executor, not sync_to_async"
    )
    assert handler_source.count("run_in_executor") == 1, (
        "handler.py may contain run_in_executor only inside its Redis-only worker helper"
    )
    assert "await run_database(_connect_rate_check_sync" in handler_source, (
        "the connect-rate path can read settings/report incidents and must use run_database"
    )
    assert "await run_database(_report_incident_sync" in handler_source, (
        "incident reporting must execute inside a pool-safe database unit"
    )
    assert handler_source.count("await run_identity_hook(") >= 4, (
        "connected, subscribe, message and disconnect hooks must all rehydrate in DB units"
    )
