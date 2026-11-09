"""Every realtime socket logs why it closed (#10231).

cleanup_connection writes one `disconnected` line per socket with the cause,
the close code, who closed it, seconds connected and seconds since the last
inbound frame. It never carries frame contents or the token.

These drive the real WebSocketHandler over the real ASGIWebSocketWrapper
in-process, feeding ASGI events by hand, and read the line back from the
realtime log by the socket's connection id. ASGI sends websocket.disconnect
for a client close, a lost transport and a server shutdown alike, so only an
ordinary close-frame code may be called the client's.
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


class _Peer:
    """The ASGI server side of one socket: events in, messages out."""

    def __init__(self, fail_sends_after_auth=False):
        self.events = asyncio.Queue()
        self.sent = []
        self.fail_sends_after_auth = fail_sends_after_auth
        self.fail_sends = False
        self.handler = None

    def client_text(self, frame):
        self.events.put_nowait({"type": "websocket.receive", "text": json.dumps(frame)})

    def disconnect(self, code):
        self.events.put_nowait({"type": "websocket.disconnect", "code": code,
                                "reason": MARKER})

    async def receive(self):
        return await self.events.get()

    async def send(self, message):
        if self.fail_sends or (self.fail_sends_after_auth and self.frames("auth_success")):
            raise OSError("broken pipe")
        self.sent.append(message)

    def frames(self, kind):
        out = []
        for message in self.sent:
            if message.get("type") == "websocket.send":
                frame = json.loads(message["text"])
                if frame.get("type") == kind:
                    out.append(frame)
        return out

    async def wait_for(self, kind, seconds=5):
        deadline = time.monotonic() + seconds
        while not self.frames(kind) and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        assert self.frames(kind), f"no {kind} frame from the server: {self.sent}"


def _session(token, finish, fail_sends_after_auth=False, **seams):
    """Authenticate one socket, send a frame carrying MARKER, then let
    `finish(peer)` end it. Returns the handler once it has cleaned up."""
    from mojo.apps.realtime.asgi import ASGIWebSocketWrapper
    from mojo.apps.realtime.handler import WebSocketHandler

    async def scenario():
        peer = _Peer(fail_sends_after_auth)
        scope = {"type": "websocket", "path": "/ws/realtime/", "headers": [],
                 "client": ("127.0.0.1", 50000)}
        socket = ASGIWebSocketWrapper(scope, peer.receive, peer.send)
        handler = WebSocketHandler(socket, "/ws/realtime/", **seams)
        peer.handler = handler
        peer.client_text({"type": "authenticate", "token": token, "prefix": "bearer"})
        task = asyncio.create_task(handler.handle_connection())
        await peer.wait_for("auth_success")
        peer.client_text({"type": "rt10231", "note": MARKER})
        await finish(peer)
        await asyncio.wait_for(task, timeout=15)
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
        assert MARKER not in line, f"frame or close-reason text reached the log: {line}"
        assert token not in line, f"the bearer token reached the realtime log: {line}"
    fields = dict(re.findall(r"(\w+)=(\S+)", closes[0].split("]: disconnected", 1)[1]))
    for name in ("reason", "code", "initiator", "connected", "idle", "auth"):
        assert name in fields, f"the close line has no {name}=: {closes[0]}"
    return fields


def _seconds(value):
    assert value.endswith("s"), f"a duration must end in s, got {value!r}"
    return float(value[:-1])


def _ended_by_event(token, code):
    async def finish(peer):
        await asyncio.sleep(0.3)
        peer.disconnect(code)

    return _session(token, finish, idle_timeout=30, ping_seconds=0)


@th.django_unit_test("an idle socket logs reason=idle_timeout, closed by the server")
def test_idle_timeout_close_line(opts):
    idle = 1.0

    async def wait_for_cull(peer):
        deadline = time.monotonic() + idle + 6
        while (not any(m.get("type") == "websocket.close" for m in peer.sent)
               and time.monotonic() < deadline):
            await asyncio.sleep(0.05)

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


@th.django_unit_test("a client close frame logs reason=client_closed with its code")
def test_client_close_line(opts):
    handler = _ended_by_event(opts.cr_token, 1001)
    fields = _close_fields(handler, opts.cr_token)
    assert (fields["reason"], fields["initiator"], fields["code"]) == \
        ("client_closed", "client", "1001"), f"wrong close line: {fields}"
    assert _seconds(fields["idle"]) < 30, f"idle is since the last frame: {fields}"


@th.django_unit_test("a lost transport (1006) is connection_lost with no initiator")
def test_transport_loss_line(opts):
    handler = _ended_by_event(opts.cr_token, 1006)
    fields = _close_fields(handler, opts.cr_token)
    assert (fields["reason"], fields["initiator"], fields["code"]) == \
        ("connection_lost", "unknown", "1006"), \
        f"ASGI 1006 is a dropped transport, not a client close: {fields}"


@th.django_unit_test("a 1005 disconnect is no_status with no initiator")
def test_no_status_line(opts):
    handler = _ended_by_event(opts.cr_token, 1005)
    fields = _close_fields(handler, opts.cr_token)
    assert (fields["reason"], fields["initiator"], fields["code"]) == \
        ("no_status", "unknown", "1005"), \
        f"1005 is a code-less close frame or a wsproto transport drop: {fields}"


@th.django_unit_test("a 1012 disconnect is service_restart with no initiator")
def test_service_restart_line(opts):
    handler = _ended_by_event(opts.cr_token, 1012)
    fields = _close_fields(handler, opts.cr_token)
    assert (fields["reason"], fields["initiator"], fields["code"]) == \
        ("service_restart", "unknown", "1012"), \
        f"uvicorn sends 1012 on shutdown; it is not a client close: {fields}"


@th.django_unit_test("a failed ASGI send is send_failed, not a client close")
def test_send_failure_line(opts):
    async def finish(peer):
        # The pong to this ping is the send that fails.
        peer.client_text({"type": "ping"})

    handler = _session(opts.cr_token, finish, fail_sends_after_auth=True,
                       idle_timeout=30, ping_seconds=0)
    fields = _close_fields(handler, opts.cr_token)
    assert (fields["reason"], fields["initiator"], fields["code"]) == \
        ("send_failed", "unknown", "none"), \
        f"no client close happened, only a failed send: {fields}"


@th.django_unit_test("a send that fails while receive waits is send_failed, not the later timeout")
def test_send_failure_while_receive_pending(opts):
    async def fail_then_idle(peer):
        # Let the marker frame be handled, so receive is waiting for the next.
        await asyncio.sleep(0.3)
        peer.fail_sends = True
        # A send from outside the receive loop, as a ping or a pub/sub
        # delivery does; it fails, and the idle timeout closes the socket later.
        await peer.handler.send_message({"type": "rt10231"})
        assert peer.handler.websocket.ended_by == "send_error", \
            f"the wrapper must record the failed send: {peer.handler.websocket.ended_by}"

    handler = _session(opts.cr_token, fail_then_idle, idle_timeout=1.0, ping_seconds=0)
    fields = _close_fields(handler, opts.cr_token)
    assert (fields["reason"], fields["initiator"], fields["code"]) == \
        ("send_failed", "unknown", "none"), \
        f"the failed send came first; the idle timeout must not replace it: {fields}"


@th.django_unit_test("the ASGI wrapper keeps an integer close code and nothing else")
def test_asgi_wrapper_close_code(opts):
    from mojo.apps.realtime.asgi import ASGIWebSocketWrapper

    async def drain(events):
        queue = list(events)

        async def receive():
            return queue.pop(0)

        async def send(message):
            pass

        wrapper = ASGIWebSocketWrapper({"headers": []}, receive, send)
        frames = [frame async for frame in wrapper]
        return wrapper, frames

    wrapper, frames = asyncio.run(drain([
        {"type": "websocket.receive", "text": "{}"},
        {"type": "websocket.disconnect", "code": 1006, "reason": MARKER},
    ]))
    assert frames == ["{}"], f"the frame before the disconnect was lost: {frames}"
    assert (wrapper.ended_by, wrapper.close_code) == ("event", 1006), \
        f"the disconnect event was not kept: {wrapper.ended_by!r} {wrapper.close_code!r}"
    assert MARKER not in repr(vars(wrapper)), \
        "the client's close reason text must not be kept"

    wrapper, _ = asyncio.run(drain([{"type": "websocket.disconnect", "code": "1006"}]))
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
    assert (wrapper.ended_by, wrapper.close_code) == ("server", 4001), \
        f"the server's own close was not kept: {wrapper.ended_by!r} {wrapper.close_code!r}"
    assert sent == [{"type": "websocket.close", "code": 4001}], f"unexpected sends: {sent}"
