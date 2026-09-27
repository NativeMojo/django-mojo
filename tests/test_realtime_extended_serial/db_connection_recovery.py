"""Realtime socket auth must recover after its database connection dies (#5736).

WMWX lost every WebSocket login for three days after a database writer
failover: realtime bearer auth ran on one long-lived sync thread, nothing on
that path returned its connection, and Django kept handing out the dead handle.

These tests kill the real PostgreSQL backend that served one auth call and
require the next auth call to succeed. They stay in this serial package because
they terminate backends and patch the process-wide bearer handler cache.
"""

import asyncio
import threading
import uuid

from testit import helpers as th

TESTIT_TIER = "bug"

PREFIX = "rt5736"


def _install_recording_handler():
    """Register a bearer handler that records which backend served it.

    The handler mirrors ``User.validate_jwt``'s ORM shape
    (``User.objects.filter(...).last()``) with the user id as the token, so the
    test exercises the realtime DB boundary rather than JWT signing.
    """
    from django.db import connection
    from mojo.apps.account.models import User
    from mojo.middleware.auth import AUTH_BEARER_HANDLERS_CACHE, AUTH_BEARER_NAME_MAP

    served = []

    def handler(token, request=None):
        user = User.objects.filter(id=int(token)).last()
        served.append({
            "backend_pid": connection.connection.info.backend_pid,
            "thread": threading.get_ident(),
        })
        if user is None:
            return None, "Invalid token user"
        return user, None

    AUTH_BEARER_HANDLERS_CACHE[PREFIX] = handler
    AUTH_BEARER_NAME_MAP[PREFIX] = "user"
    return served


def _remove_recording_handler():
    from mojo.middleware.auth import AUTH_BEARER_HANDLERS_CACHE, AUTH_BEARER_NAME_MAP

    AUTH_BEARER_HANDLERS_CACHE.pop(PREFIX, None)
    AUTH_BEARER_NAME_MAP.pop(PREFIX, None)


def _terminate_backend(pid):
    """Kill one server backend the way a writer failover does.

    Runs on its own short-lived thread (see ``asyncio.to_thread`` below), so it
    uses a fresh connection and closes it before returning.
    """
    from django.db import connection

    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_terminate_backend(%s)", [pid])
            return cursor.fetchone()[0]
    finally:
        connection.close()


def _make_user():
    from mojo.apps.account.models import User

    username = f"rt5736_{uuid.uuid4().hex[:10]}"
    user = User(username=username, display_name=username,
                email=f"{username}@example.com", is_email_verified=True)
    user.save()
    return user


@th.django_unit_test("realtime auth recovers after its database backend is terminated")
def test_realtime_auth_recovers_after_backend_terminated(opts):
    from mojo.apps.realtime.auth import async_validate_bearer_token

    user = _make_user()
    served = _install_recording_handler()
    try:
        async def scenario():
            # One event loop, like one mojo-asgi process.
            first = await async_validate_bearer_token(PREFIX, str(user.pk))
            assert first[1] is None, f"first auth must succeed, got {first!r}"
            dead_pid = served[-1]["backend_pid"]

            # False once a fix returns the connection after each auth: the
            # backend is already gone, which is the point.
            await asyncio.to_thread(_terminate_backend, dead_pid)

            results = []
            for _ in range(3):
                results.append(await async_validate_bearer_token(PREFIX, str(user.pk)))
            return dead_pid, results

        dead_pid, results = asyncio.run(scenario())
        errors = [error for _, error, _ in results]
        # Every auth after the kill must succeed, not just an eventual one: in
        # production one dead handle failed every login for three days.
        assert errors == [None, None, None], (
            f"auth after backend {dead_pid} was terminated must succeed, got {errors}"
        )
        later_pids = {row["backend_pid"] for row in served[1:]}
        assert dead_pid not in later_pids, (
            f"auth kept using terminated backend {dead_pid}: {served}"
        )
    finally:
        _remove_recording_handler()
        user.delete()


# ---------------------------------------------------------------------------
# T2: the same recovery with a psycopg pool, and no lease left checked out
# ---------------------------------------------------------------------------

POOL_ALIAS = "rt5736_pool"


def _add_pooled_alias():
    """Register a pooled copy of the default database for this test only.

    The test process runs without a pool; a separate alias gives the pool
    path without restarting anything. ``close_old_connections()`` covers every
    initialized alias, so the boundary under test is the same code.
    """
    import copy
    from django.db import connections

    cfg = copy.deepcopy(connections.settings["default"])
    cfg["CONN_MAX_AGE"] = 0
    cfg["CONN_HEALTH_CHECKS"] = True
    cfg.setdefault("OPTIONS", {})
    cfg["OPTIONS"]["pool"] = {"min_size": 0, "max_size": 2, "timeout": 5}
    connections.settings[POOL_ALIAS] = cfg


def _remove_pooled_alias():
    from django.db import connections

    try:
        connections[POOL_ALIAS].close_pool()
    except Exception:
        pass
    try:
        del connections[POOL_ALIAS]
    except Exception:
        pass
    connections.settings.pop(POOL_ALIAS, None)


def _checked_out():
    from django.db import connections

    stats = connections[POOL_ALIAS].pool.get_stats()
    return stats.get("pool_size", 0) - stats.get("pool_available", 0)


def _install_pool_handler(mode):
    """Bearer handler on the pooled alias. ``mode`` selects ok, error or slow."""
    import time
    from django.db import connections
    from mojo.apps.account.models import User
    from mojo.middleware.auth import AUTH_BEARER_HANDLERS_CACHE, AUTH_BEARER_NAME_MAP

    served = []

    def handler(token, request=None):
        user = User.objects.using(POOL_ALIAS).filter(id=int(token)).last()
        served.append(connections[POOL_ALIAS].connection.info.backend_pid)
        if mode["value"] == "error":
            raise RuntimeError("rt5736 handler failure")
        if mode["value"] == "slow":
            time.sleep(0.5)
        return user, None

    AUTH_BEARER_HANDLERS_CACHE[PREFIX] = handler
    AUTH_BEARER_NAME_MAP[PREFIX] = "user"
    return served


@th.django_unit_test("pooled realtime auth recovers and returns every lease")
def test_pooled_realtime_auth_recovers_and_returns_leases(opts):
    from mojo.apps.realtime.auth import async_validate_bearer_token

    user = _make_user()
    _add_pooled_alias()
    mode = {"value": "ok"}
    served = _install_pool_handler(mode)
    try:
        async def scenario():
            first = await async_validate_bearer_token(PREFIX, str(user.pk))
            assert first[1] is None, f"first pooled auth must succeed, got {first!r}"
            out_after_success = await asyncio.to_thread(_checked_out)

            await asyncio.to_thread(_terminate_backend, served[-1])
            after_kill = [await async_validate_bearer_token(PREFIX, str(user.pk))
                          for _ in range(3)]

            mode["value"] = "error"
            errored = await async_validate_bearer_token(PREFIX, str(user.pk))
            out_after_error = await asyncio.to_thread(_checked_out)

            # Cancel the awaiter while the handler still holds its lease. The
            # thread keeps running; the lease must come back when it exits.
            mode["value"] = "slow"
            task = asyncio.ensure_future(async_validate_bearer_token(PREFIX, str(user.pk)))
            await asyncio.sleep(0.1)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            await asyncio.sleep(0.8)
            out_after_cancel = await asyncio.to_thread(_checked_out)
            return out_after_success, after_kill, errored, out_after_error, out_after_cancel

        (out_after_success, after_kill, errored, out_after_error,
         out_after_cancel) = asyncio.run(scenario())
        assert out_after_success == 0, f"lease kept after a successful auth: {out_after_success}"
        errors = [error for _, error, _ in after_kill]
        assert errors == [None, None, None], f"pooled auth after backend kill failed: {errors}"
        assert errored[1] == "handler error", f"error path must still report, got {errored!r}"
        assert out_after_error == 0, f"lease kept after a handler error: {out_after_error}"
        assert out_after_cancel == 0, f"lease kept after a cancelled auth: {out_after_cancel}"
    finally:
        _remove_recording_handler()
        _remove_pooled_alias()
        user.delete()


# ---------------------------------------------------------------------------
# T3: identity hooks and subscribe permission recover on executor threads
# ---------------------------------------------------------------------------

HOOK_PREFIX = "rt5736hook"


class _Socket:
    scope = {"headers": [], "client": ("127.0.0.1", 12345)}

    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(payload)


class _PubSub:
    def subscribe(self, *args):
        pass

    def unsubscribe(self, *args):
        pass

    def close(self):
        pass


class _HookIdentity:
    """A bearer identity whose hooks read the database like real app hooks."""

    served = []

    def __init__(self, user_id):
        self.id = user_id
        self.pk = user_id

    def _query(self, hook):
        from django.db import connection
        from mojo.apps.account.models import User

        User.objects.filter(id=self.id).last()
        self.served.append((hook, connection.connection.info.backend_pid))

    def on_realtime_connection(self, data):
        self._query("connect")
        return None

    def on_realtime_can_subscribe(self, topic):
        self._query("subscribe")
        return True

    def on_realtime_message(self, data):
        self._query("message")
        return {"response": {"type": "rt5736_ack"}}


def _install_hook_handler():
    from mojo.middleware.auth import AUTH_BEARER_HANDLERS_CACHE, AUTH_BEARER_NAME_MAP

    def handler(token, request=None):
        return _HookIdentity(int(token)), None

    AUTH_BEARER_HANDLERS_CACHE[HOOK_PREFIX] = handler
    AUTH_BEARER_NAME_MAP[HOOK_PREFIX] = "rt5736hook"


def _remove_hook_handler():
    from mojo.middleware.auth import AUTH_BEARER_HANDLERS_CACHE, AUTH_BEARER_NAME_MAP

    AUTH_BEARER_HANDLERS_CACHE.pop(HOOK_PREFIX, None)
    AUTH_BEARER_NAME_MAP.pop(HOOK_PREFIX, None)


async def _socket_session(user_id):
    """Authenticate, subscribe and send one message on an in-process handler."""
    import json
    from mojo.apps.realtime.handler import WebSocketHandler

    handler = WebSocketHandler(_Socket(), "/ws/realtime/")

    async def fake_pubsub():
        handler.pubsub = _PubSub()

    handler.start_redis_messages = fake_pubsub
    try:
        await handler.handle_authenticate({"token": str(user_id), "prefix": HOOK_PREFIX})
        await handler.handle_subscribe({"topic": "rt5736:topic"})
        await handler.handle_custom_message({"type": "rt5736"})
        return [json.loads(item) if isinstance(item, str) else item
                for item in handler.websocket.sent]
    finally:
        await handler.cleanup_connection()


@th.django_unit_test("realtime hooks and subscribe checks recover after their backend is terminated")
def test_realtime_hooks_recover_after_backend_terminated(opts):
    from concurrent.futures import ThreadPoolExecutor

    user = _make_user()
    _install_hook_handler()
    _HookIdentity.served = []
    try:
        async def scenario():
            # One worker makes every executor call reuse one thread, the way a
            # busy process reuses its default-executor threads.
            executor = ThreadPoolExecutor(max_workers=1)
            asyncio.get_running_loop().set_default_executor(executor)
            first = await _socket_session(user.pk)
            pids = {pid for _, pid in _HookIdentity.served}
            for pid in pids:
                await asyncio.to_thread(_terminate_backend, pid)
            _HookIdentity.served = []
            second = await _socket_session(user.pk)
            return first, pids, second

        first, dead_pids, second = asyncio.run(scenario())
        types_first = [m.get("type") for m in first]
        assert "auth_success" in types_first and "subscribed" in types_first, types_first
        types = [m.get("type") for m in second]
        assert "auth_success" in types, f"login after backend kill failed: {second}"
        assert "subscribed" in types, f"subscribe after backend kill failed: {second}"
        assert "rt5736_ack" in types, f"message hook after backend kill failed: {second}"
        hooks = [hook for hook, _ in _HookIdentity.served]
        assert hooks == ["connect", "subscribe", "message"], hooks
        reused = {pid for _, pid in _HookIdentity.served} & dead_pids
        assert not reused, f"hooks kept using terminated backends {reused}"
    finally:
        _remove_hook_handler()
        user.delete()


# ---------------------------------------------------------------------------
# T4: every executor call that can reach the database is wrapped
# ---------------------------------------------------------------------------

# Callables that only touch Redis. Anything new passed to run_in_executor must
# either be wrapped in database_thread_target or be added here on purpose.
REDIS_ONLY_EXECUTOR_CALLS = {
    ("register_connection", "lambda"),
    ("update_connection_auth", "lambda"),
    ("register_user_online", "get_and_update"),
    ("start_redis_messages", "create_pubsub"),
    ("handle_redis_messages", "get_message"),
    ("handle_redis_messages", "self.pubsub.close"),
    ("handle_authenticate", "count_connections"),
    ("handle_authenticate", "report_once"),
    ("handle_response", "push_response"),
    ("check_waiters", "do_check"),
    ("subscribe_to_topic", "subscribe"),
    ("unsubscribe_from_topic", "unsubscribe"),
    ("refresh_presence", "do_refresh"),
    ("cleanup_connection", "cleanup"),
    ("cleanup_connection", "self.pubsub.close"),
}

# Callables that can reach the database, by enclosing function and callable.
DATABASE_EXECUTOR_CALLS = {
    ("check_connect_rate", "_connect_rate_check_sync"),
    ("handle_authenticate", "call_hook"),
    ("handle_subscribe", "check_permission"),
    ("handle_custom_message", "call_hook"),
    ("_can_access_protected_group_topic", "can_access_group_topic"),
    ("process_redis_message", "self._can_receive_chat"),
    ("report_incident", "lambda"),
    ("cleanup_connection", "call_hook"),
}


@th.django_unit_test("every realtime executor call that can reach the database is wrapped")
def test_realtime_executor_calls_are_wrapped(opts):
    import ast
    import inspect
    from mojo.apps.realtime import handler as handler_module

    tree = ast.parse(inspect.getsource(handler_module))
    unwrapped = []
    wrapped = []
    seen_redis_only = set()

    def enclosing_function(node, parents):
        while node in parents:
            node = parents[node]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return node.name
        return "<module>"

    parents = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "run_in_executor"):
            continue
        target = node.args[1]
        is_wrapped = (isinstance(target, ast.Call) and isinstance(target.func, ast.Name)
                      and target.func.id == "database_thread_target")
        inner = target.args[0] if is_wrapped else target
        name = "lambda" if isinstance(inner, ast.Lambda) else ast.unparse(inner)
        key = (enclosing_function(node, parents), name)
        if is_wrapped:
            wrapped.append(key)
            continue
        if key in REDIS_ONLY_EXECUTOR_CALLS:
            seen_redis_only.add(key)
        else:
            unwrapped.append(f"line {node.lineno}: {key}")

    assert not unwrapped, (
        "executor calls neither wrapped in database_thread_target nor listed "
        f"as Redis-only: {unwrapped}")
    stale = REDIS_ONLY_EXECUTOR_CALLS - seen_redis_only
    assert not stale, f"Redis-only list names calls that no longer exist: {stale}"
    missing = DATABASE_EXECUTOR_CALLS - set(wrapped)
    assert not missing, f"database-capable executor calls not wrapped: {missing}"
