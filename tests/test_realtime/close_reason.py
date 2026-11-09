"""Every realtime socket logs why it closed (#10231).

cleanup_connection writes one `disconnected` line per socket with the cause,
the close code, who closed it, seconds connected and seconds since the last
inbound frame. It never carries frame contents or the token.

These drive the real WebSocketHandler in-process over a fake socket, as
keepalive.py does, and read the line back from the realtime log by the
socket's connection id.
"""
import asyncio
import json
import re
import time

from testit import helpers as th

USERNAME = "ws_close_reason_10231"
PASSWORD = "testit##mojo"
MARKER = "frame-payload-marker-10231"


@th.django_unit_setup()
def setup_close_reason_user(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils.jwtoken import JWToken

    User.objects.filter(username=USERNAME).delete()
    user = User(username=USERNAME, display_name=USERNAME,
                email=f"{USERNAME}@example.com", is_email_verified=True)
    user.save_password(PASSWORD)
    user.save()
    opts.cr_token = JWToken(user.get_auth_key()).create_access_token(uid=user.pk)


class _ClientSocket:
    """In-process stand-in for the ASGI socket wrapper, with its close code."""

    def __init__(self):
        self.inbound = asyncio.Queue()
        self.sent = []
        self.close_code = None

    def client_send(self, frame):
        self.inbound.put_nowait(json.dumps(frame))

    def client_hang_up(self, code=None):
        if self.close_code is None:
            self.close_code = code
        self.inbound.put_nowait(None)

    async def __aiter__(self):
        while True:
            message = await self.inbound.get()
            if message is None:
                return
            yield message

    async def send(self, message):
        self.sent.append(json.loads(message))

    async def close(self, code=1000):
        if self.close_code is None:
            self.close_code = code
        self.inbound.put_nowait(None)


def _session(token, client, **seams):
    """Authenticate one socket, send a frame carrying MARKER, then let
    `client(socket)` decide how it ends. Returns the handler."""
    from mojo.apps.realtime.handler import WebSocketHandler

    async def scenario():
        socket = _ClientSocket()
        handler = WebSocketHandler(socket, "/ws/realtime/", **seams)
        socket.client_send({"type": "authenticate", "token": token, "prefix": "bearer"})
        socket.client_send({"type": "rt10231", "note": MARKER})
        task = asyncio.create_task(handler.handle_connection())
        await client(socket)
        await asyncio.wait_for(task, timeout=15)
        assert any(f.get("type") == "auth_success" for f in socket.sent), \
            f"in-process auth failed: {socket.sent}"
        return handler

    return asyncio.run(scenario())


def _lines(handler):
    from mojo.helpers import paths

    text = (paths.LOG_ROOT / "realtime.log").read_text(errors="replace")
    return [line for line in text.splitlines() if handler.connection_id in line]


def _close_fields(handler, token):
    lines = _lines(handler)
    closes = [line for line in lines if "]: disconnected" in line]
    assert len(closes) == 1, (
        f"expected exactly one disconnected line for {handler.connection_id}, "
        f"got {len(closes)}: {closes}")
    for line in lines:
        assert MARKER not in line, f"a frame's contents reached the realtime log: {line}"
        assert token not in line, f"the bearer token reached the realtime log: {line}"
    fields = dict(re.findall(r"(\w+)=(\S+)", closes[0].split("]: disconnected", 1)[1]))
    for name in ("reason", "code", "initiator", "connected", "idle", "auth"):
        assert name in fields, f"the close line has no {name}=: {closes[0]}"
    return fields


def _seconds(value):
    assert value.endswith("s"), f"a duration must end in s, got {value!r}"
    return float(value[:-1])


@th.django_unit_test("an idle socket logs reason=idle_timeout, closed by the server")
def test_idle_timeout_close_line(opts):
    idle = 1.0

    async def wait_for_cull(socket):
        deadline = time.monotonic() + idle + 6
        while socket.close_code is None and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        socket.client_hang_up()

    handler = _session(opts.cr_token, wait_for_cull, idle_timeout=idle, ping_seconds=0)
    fields = _close_fields(handler, opts.cr_token)
    assert fields["reason"] == "idle_timeout", f"wrong reason: {fields}"
    assert fields["initiator"] == "server", f"the server closed it: {fields}"
    assert fields["code"] == "1000", f"the server closes with 1000: {fields}"
    assert fields["auth"] == "yes", f"the socket was authenticated: {fields}"
    assert _seconds(fields["idle"]) >= idle - 0.1, \
        f"idle must cover the {idle}s without frames: {fields}"
    assert _seconds(fields["connected"]) >= _seconds(fields["idle"]), \
        f"a socket cannot be idle longer than it was connected: {fields}"


@th.django_unit_test("a client hang-up logs reason=client_closed with the client's code")
def test_client_close_line(opts):
    async def hang_up(socket):
        await asyncio.sleep(0.5)
        socket.client_hang_up(code=1001)

    handler = _session(opts.cr_token, hang_up, idle_timeout=30, ping_seconds=0)
    fields = _close_fields(handler, opts.cr_token)
    assert fields["reason"] == "client_closed", f"wrong reason: {fields}"
    assert fields["initiator"] == "client", f"the client closed it: {fields}"
    assert fields["code"] == "1001", f"the client's close code must be logged: {fields}"
    assert _seconds(fields["idle"]) < 30, f"idle is since the last frame: {fields}"


@th.django_unit_test("the ASGI wrapper keeps an integer close code and nothing else")
def test_asgi_wrapper_close_code(opts):
    from mojo.apps.realtime.asgi import ASGIWebSocketWrapper

    async def drain(events):
        queue = list(events)
        sent = []

        async def receive():
            return queue.pop(0)

        async def send(message):
            sent.append(message)

        wrapper = ASGIWebSocketWrapper({"headers": []}, receive, send)
        frames = [frame async for frame in wrapper]
        return wrapper, frames, sent

    wrapper, frames, _ = asyncio.run(drain([
        {"type": "websocket.receive", "text": "{}"},
        {"type": "websocket.disconnect", "code": 1006, "reason": MARKER},
    ]))
    assert frames == ["{}"], f"the frame before the disconnect was lost: {frames}"
    assert wrapper.close_code == 1006, \
        f"the client's disconnect code was not kept: {wrapper.close_code!r}"
    assert MARKER not in repr(vars(wrapper)), \
        "the client's close reason text must not be kept"

    wrapper, _, _ = asyncio.run(drain([
        {"type": "websocket.disconnect", "code": "1006"}]))
    assert wrapper.close_code is None, \
        f"a non-integer code must not be kept: {wrapper.close_code!r}"

    async def server_close():
        sent = []

        async def receive():
            await asyncio.sleep(10)

        async def send(message):
            sent.append(message)

        wrapper = ASGIWebSocketWrapper({"headers": []}, receive, send)
        await wrapper.close(4001)
        return wrapper, sent

    wrapper, sent = asyncio.run(server_close())
    assert wrapper.close_code == 4001, \
        f"the server's own close code was not kept: {wrapper.close_code!r}"
    assert sent == [{"type": "websocket.close", "code": 4001}], f"unexpected sends: {sent}"
