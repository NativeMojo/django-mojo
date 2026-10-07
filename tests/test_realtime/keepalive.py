"""Realtime keepalive and the cost of a connect (#6562).

- The idle cull is WS_IDLE_TIMEOUT (default 90 s), not a hard-coded 30 s.
- The server pings every authenticated socket every WS_SERVER_PING_SECONDS
  with an application frame; a client's `pong` (like any inbound frame) resets
  the idle clock and is answered with nothing, so a socket that only listens
  stays open for as long as it answers.
- A connect or disconnect touches Redis, not the User row. Products hear it
  through `realtime_connection_changed`.

Most of these drive the real WebSocketHandler in-process over a fake socket,
shrinking the timeouts through the handler's keyword seams: the live test
server runs the 90 s / 20 s defaults, and changing those there would mean a
process-wide settings reload.
"""
import asyncio
import json
import time

from testit import helpers as th
from testit.ws_client import WsClient

USERNAME = "ws_keepalive_6562"
PASSWORD = "testit##mojo"


@th.django_unit_setup()
def setup_keepalive_user(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils.jwtoken import JWToken

    User.objects.filter(username=USERNAME).delete()
    user = User(username=USERNAME, display_name=USERNAME,
                email=f"{USERNAME}@example.com", is_email_verified=True)
    user.save_password(PASSWORD)
    user.save()
    opts.ka_uid = user.pk
    # Minted in-process so the in-process sessions never depend on a REST
    # login (and its writes to the row) having settled.
    opts.ka_token = JWToken(user.get_auth_key()).create_access_token(uid=user.pk)


class _ClientSocket:
    """In-process stand-in for the ASGI socket wrapper.

    Frames the client sends come from a queue; frames the server sends are
    recorded. With `answer_pings` the client echoes every server ping as a
    pong and sends nothing else, like a page frozen in the background whose
    socket handler still runs."""

    def __init__(self, answer_pings=False):
        self.inbound = asyncio.Queue()
        self.sent = []
        self.answer_pings = answer_pings
        self.server_closed_at = None

    def client_send(self, frame):
        self.inbound.put_nowait(json.dumps(frame))

    def client_hang_up(self):
        self.inbound.put_nowait(None)

    async def __aiter__(self):
        while True:
            message = await self.inbound.get()
            if message is None:
                return
            yield message

    async def send(self, message):
        frame = json.loads(message)
        self.sent.append(frame)
        if self.answer_pings and frame.get("type") == "ping":
            self.client_send({"type": "pong", "ts": frame.get("ts")})

    async def close(self, code=1000):
        if self.server_closed_at is None:
            self.server_closed_at = time.monotonic()
        self.client_hang_up()

    def frames(self, kind):
        return [frame for frame in self.sent if frame.get("type") == kind]


def _run_session(token, hold, answer_pings=False, **seams):
    """Authenticate one in-process socket and keep the client side open for
    `hold` seconds or until the server closes it, then hang up and let the
    handler clean up. Returns (socket, handler, seconds from the authenticate
    frame to the server's close, or None when the server never closed it)."""
    from mojo.apps.realtime.handler import WebSocketHandler

    async def scenario():
        socket = _ClientSocket(answer_pings=answer_pings)
        handler = WebSocketHandler(socket, "/ws/realtime/", **seams)
        started = time.monotonic()
        socket.client_send({"type": "authenticate", "token": token, "prefix": "bearer"})
        task = asyncio.create_task(handler.handle_connection())
        await asyncio.wait({task}, timeout=hold)
        if not task.done():
            socket.client_hang_up()
        await asyncio.wait_for(task, timeout=15)
        closed = socket.server_closed_at
        return socket, handler, (closed - started) if closed is not None else None

    return asyncio.run(scenario())


@th.django_unit_test()
def test_idle_cull_uses_the_configured_timeout(opts):
    """A socket that never answers is culled at its idle timeout — and the
    server's own pings do not count as its activity."""
    idle = 1.0
    socket, _, culled_after = _run_session(
        opts.ka_token, hold=idle + 4.0, idle_timeout=idle, ping_seconds=0.25)

    assert socket.frames("auth_success"), f"in-process auth failed: {socket.sent}"
    assert culled_after is not None, (
        f"a silent authenticated socket must be culled after the {idle}s idle "
        f"timeout; the server never closed it: {socket.sent}")
    assert culled_after >= idle - 0.01, (
        f"culled after {culled_after:.2f}s, before the configured {idle}s idle timeout")
    assert culled_after <= idle + 3.0, (
        f"culled after {culled_after:.2f}s, long past the configured {idle}s idle timeout")
    pings = socket.frames("ping")
    assert pings, f"the server must ping an authenticated socket: {socket.sent}"
    assert all(isinstance(p.get("ts"), int) and abs(p["ts"] - time.time()) < 60 for p in pings), (
        f"a server ping carries its epoch seconds as ts: {pings}")


@th.django_unit_test()
def test_pings_keep_a_listen_only_socket_open(opts):
    """A client that sends nothing but its pongs survives three idle windows,
    and its pongs draw no reply."""
    idle = 1.0
    socket, _, culled_after = _run_session(
        opts.ka_token, hold=3 * idle, answer_pings=True, idle_timeout=idle, ping_seconds=0.2)

    assert socket.frames("auth_success"), f"in-process auth failed: {socket.sent}"
    assert culled_after is None, (
        f"a socket answering every ping was culled after {culled_after:.2f}s "
        f"(idle timeout {idle}s): {socket.sent}")
    assert len(socket.frames("ping")) >= 5, (
        f"expected a ping every 0.2s over {3 * idle}s, got {socket.frames('ping')}")
    replies = [f for f in socket.sent if f.get("type") in ("ack", "error", "pong")]
    assert not replies, f"a client pong must be absorbed silently, got {replies}"


@th.django_unit_test()
def test_server_pings_can_be_disabled(opts):
    """WS_SERVER_PING_SECONDS = 0 turns the server ping off."""
    socket, _, culled_after = _run_session(
        opts.ka_token, hold=0.6, idle_timeout=5, ping_seconds=0)

    assert socket.frames("auth_success"), f"in-process auth failed: {socket.sent}"
    assert culled_after is None, f"culled inside a 5s idle timeout: {socket.sent}"
    assert not socket.frames("ping"), f"pings were disabled but sent: {socket.sent}"


@th.django_unit_test()
def test_pong_resets_idle_without_a_reply_or_the_user_hook(opts):
    from mojo.apps.realtime.handler import WebSocketHandler

    class _Identity:
        id = opts.ka_uid
        messages = []

        def on_realtime_message(self, data):
            self.messages.append(data)
            return {"response": {"type": "ack"}}

    async def scenario():
        socket = _ClientSocket()
        handler = WebSocketHandler(socket, "/ws/realtime/")
        handler.user = _Identity()
        handler.user_type = "keepalive6562"
        handler.authenticated = True
        handler.last_activity = time.time() - 600
        await handler.process_client_message({"type": "pong", "ts": int(time.time())})
        return socket, handler

    socket, handler = asyncio.run(scenario())
    assert time.time() - handler.last_activity < 5, (
        "a pong must reset the idle clock like any inbound frame")
    assert socket.sent == [], f"a pong must not be answered, got {socket.sent}"
    assert handler.user.messages == [], (
        f"a pong must not fall through to on_realtime_message: {handler.user.messages}")


@th.django_unit_test()
def test_connect_and_disconnect_leave_the_user_row_alone(opts):
    """A socket's connect and disconnect write nothing to account_user (any
    UPDATE gives the row a new xmin) and send realtime_connection_changed
    after the Redis presence set changed — even when a receiver raises."""
    from django.db import connection
    from mojo.apps import realtime
    from mojo.apps.account.models import User
    from mojo.apps.realtime.signals import realtime_connection_changed
    from mojo.helpers import dates

    uid = opts.ka_uid
    events = []

    def record(sender, user, connected, connection_id, **kwargs):
        if getattr(user, "pk", None) == uid:
            events.append((sender, connected, connection_id,
                           bool(realtime.is_online("user", uid))))

    def explode(sender, user, **kwargs):
        if getattr(user, "pk", None) == uid:
            raise RuntimeError("a broken receiver must not reach the socket")

    def row_version():
        with connection.cursor() as cursor:
            cursor.execute(
                f'SELECT xmin::text FROM "{User._meta.db_table}" WHERE id = %s', [uid])
            return cursor.fetchone()[0]

    # Bearer validation touches last_activity at most every
    # USER_LAST_ACTIVITY_FREQ; stamp it now so this session's auth is a read.
    User.objects.filter(pk=uid).update(last_activity=dates.utcnow())
    before = row_version()
    realtime_connection_changed.connect(record, weak=False, dispatch_uid="test6562.record")
    realtime_connection_changed.connect(explode, weak=False, dispatch_uid="test6562.explode")
    try:
        socket, handler, _ = _run_session(opts.ka_token, hold=0.3, idle_timeout=5, ping_seconds=0)
    finally:
        realtime_connection_changed.disconnect(dispatch_uid="test6562.record")
        realtime_connection_changed.disconnect(dispatch_uid="test6562.explode")
    after = row_version()

    assert socket.frames("auth_success"), (
        f"auth must succeed although a receiver raised: {socket.sent}")
    assert after == before, (
        f"a realtime connect/disconnect updated account_user row {uid} "
        f"(xmin {before} -> {after})")
    cid = handler.connection_id
    assert events == [(User, True, cid, True), (User, False, cid, False)], (
        "expected one connected and one disconnected event for this socket, each "
        f"sent after the presence set changed (sender, connected, id, online): {events}")


@th.unit_test("ws_pong_is_absorbed_by_the_live_server")
def test_live_server_absorbs_a_client_pong(opts):
    """Over the real server: a client pong draws no reply (it used to reach
    the user hook and come back as an ack), and the socket still works."""
    assert opts.client.login(USERNAME, PASSWORD), "authentication failed"
    ws = WsClient(WsClient.build_url_from_host(opts.host, path="ws/realtime/"), logger=opts.logger)
    try:
        ws.connect(timeout=10.0)
        auth = ws.authenticate(opts.client.access_token, wait=True, timeout=10.0)
        assert auth.get("type") == "auth_success", f"unexpected auth response: {auth}"
        ws.send_json({"type": "pong", "ts": int(time.time())})
        ws.send_json({"type": "ping"})
        reply = ws.wait_for_types({"pong", "ack", "error"}, timeout=5.0)
        assert reply.data.get("type") == "pong", (
            f"the first reply after a client pong must be the server's pong, got {reply.data}")
    finally:
        ws.close()
        opts.client.logout()
