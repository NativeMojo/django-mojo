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
