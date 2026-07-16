"""DM-042: websocket connection hardening.

- pre-accept per-IP connect-rate gate (unit + in-process ASGI; disabled
  suite-wide via WS_CONNECT_RATE_LIMIT=0 because every module shares 127.0.0.1)
- per-identity concurrency cap (live server, default WS_MAX_CONNECTIONS=10)
- short unauthenticated window advertised to clients (WS_UNAUTH_TIMEOUT)
"""
import asyncio
import time
import uuid as _uuid
from contextlib import contextmanager

from testit import helpers as th
from testit.ws_client import WsClient

WINDOW = 60  # WS_CONNECT_WINDOW_SECONDS


@contextmanager
def _override_setting(name, value):
    """In-process Django settings override (th.server_settings only affects the
    separate server process; override_settings is banned by testing rules)."""
    import django.conf
    sentinel = object()
    original = getattr(django.conf.settings, name, sentinel)
    setattr(django.conf.settings, name, value)
    try:
        yield
    finally:
        if original is sentinel:
            delattr(django.conf.settings, name)
        else:
            setattr(django.conf.settings, name, original)


def _fake_ip():
    return f"198.51.100.{int(_uuid.uuid4().int % 254) + 1}"


def _clean_ip_keys(ip):
    from mojo.helpers.redis import get_connection
    from mojo.apps.incident.models import Event
    r = get_connection()
    for k in r.scan_iter(f"rl:ws_connect:{ip}:*"):
        r.delete(k)
    for k in r.scan_iter(f"rl:ws_connect:blocked:{ip}:*"):
        r.delete(k)
    Event.objects.filter(category="traffic:ws_connect", source_ip=ip).delete()


@th.django_unit_test()
def test_connect_rate_check_blocks_and_reports_once(opts):
    from mojo.apps.realtime.handler import _connect_rate_check_sync
    from mojo.apps.incident.models import Event

    ip = _fake_ip()
    _clean_ip_keys(ip)
    with _override_setting("WS_CONNECT_RATE_LIMIT", 3):
        for i in range(3):
            assert _connect_rate_check_sync(ip) is True, (
                f"connect {i + 1}/3 is under the limit and must be allowed"
            )
        for i in range(3):
            assert _connect_rate_check_sync(ip) is False, (
                f"connect {i + 4} is over the limit of 3 and must be refused"
            )
    events = Event.objects.filter(category="traffic:ws_connect", source_ip=ip)
    assert events.count() == 1, (
        f"3 refused connects in one window must produce exactly 1 incident event, "
        f"got {events.count()}"
    )
    _clean_ip_keys(ip)


@th.django_unit_test()
def test_connect_rate_check_disabled_and_fail_open(opts):
    from mojo.apps.realtime import handler as rt_handler

    ip = _fake_ip()
    _clean_ip_keys(ip)
    # Limit <= 0 disables the gate entirely (the suite's own posture).
    with _override_setting("WS_CONNECT_RATE_LIMIT", 0):
        for i in range(5):
            assert rt_handler._connect_rate_check_sync(ip) is True, (
                "limit 0 must disable the connect-rate gate"
            )
    # Redis outage: fail open, never refuse all sockets.
    original = rt_handler.get_connection

    def broken_connection():
        raise RuntimeError("redis down (simulated)")

    rt_handler.get_connection = broken_connection
    try:
        with _override_setting("WS_CONNECT_RATE_LIMIT", 1):
            assert rt_handler._connect_rate_check_sync(ip) is True, (
                "connect-rate gate must fail open on Redis errors"
            )
    finally:
        rt_handler.get_connection = original
    _clean_ip_keys(ip)


@th.django_unit_test()
def test_preaccept_close_4429(opts):
    """Drive the real ASGI app in-process: an over-limit IP is closed with
    code 4429 BEFORE websocket.accept — the storm never reaches the handler."""
    from mojo.apps.realtime.asgi import ASGIApplication
    from mojo.helpers.redis import get_connection

    ip = _fake_ip()
    _clean_ip_keys(ip)
    # Pre-seed the current window over the limit.
    window_start = int(time.time()) // WINDOW * WINDOW
    r = get_connection()
    r.set(f"rl:ws_connect:{ip}:{window_start}", 99, ex=WINDOW * 2)

    sent = []

    async def fake_send(message):
        sent.append(message)

    async def fake_receive():
        raise AssertionError("receive must never be called for a refused handshake")

    scope = {
        "type": "websocket",
        "path": "/ws/realtime/",
        "headers": [(b"x-real-ip", ip.encode())],
        "client": ("127.0.0.1", 54321),
    }
    with _override_setting("WS_CONNECT_RATE_LIMIT", 3):
        asyncio.run(ASGIApplication().websocket_application(scope, fake_receive, fake_send))

    assert len(sent) == 1, f"expected exactly one close frame, got {sent}"
    assert sent[0]["type"] == "websocket.close", f"expected websocket.close, got {sent[0]}"
    assert sent[0]["code"] == 4429, (
        f"refused handshake must use close code 4429 (deliberate rejection), got {sent[0]['code']}"
    )
    _clean_ip_keys(ip)


@th.django_unit_setup()
def setup_ws_limits_user(opts):
    from mojo.apps.account.models import User
    email = f"dm042_ws_{_uuid.uuid4().hex[:8]}@limits.test"
    password = "Dm042##wslimits"
    User.objects.filter(username=email).delete()
    user = User.objects.create_user(username=email, email=email, password=password)
    user.is_active = True
    user.is_email_verified = True
    user.requires_mfa = False
    user.save()
    opts.ws_email = email
    opts.ws_password = password
    opts.ws_uid = user.pk


@th.django_unit_test()
def test_auth_required_advertises_short_timeout(opts):
    ws_url = WsClient.build_url_from_host(opts.host, path="ws/realtime/")
    ws = WsClient(ws_url, logger=opts.logger)
    try:
        ws.connect(timeout=5.0)
        msg = ws.wait_for_type("auth_required", timeout=5.0)
        advertised = msg.data.get("timeout")
        assert advertised == 10, (
            f"unauthenticated sockets get the short WS_UNAUTH_TIMEOUT window "
            f"(default 10s), server advertised {advertised!r}"
        )
    finally:
        ws.close()


@th.django_unit_test()
def test_ws_max_connections_cap(opts):
    """The 11th concurrent socket for one identity is rejected at auth
    (default WS_MAX_CONNECTIONS=10)."""
    assert opts.client.login(opts.ws_email, opts.ws_password), (
        f"login failed: {opts.client.last_response.body}"
    )
    token = opts.client.access_token
    ws_url = WsClient.build_url_from_host(opts.host, path="ws/realtime/")

    sockets = []
    try:
        for i in range(10):
            ws = WsClient(ws_url, logger=opts.logger)
            ws.connect(timeout=10.0)
            sockets.append(ws)
            auth = ws.authenticate(token, wait=True, timeout=10.0)
            assert auth.get("type") == "auth_success", (
                f"socket {i + 1}/10 must authenticate (under the cap), got {auth}"
            )

        extra = WsClient(ws_url, logger=opts.logger)
        extra.connect(timeout=10.0)
        sockets.append(extra)
        extra.send_json({"type": "authenticate", "token": token, "prefix": "bearer"})
        msg = extra.wait_for_types({"auth_success", "error", "auth_timeout"}, timeout=10.0)
        assert msg.data.get("type") == "error", (
            f"11th concurrent socket must be rejected at auth, got {msg.data}"
        )
        assert "too many connections" in str(msg.data.get("message", "")).lower(), (
            f"rejection should say why, got {msg.data.get('message')!r}"
        )
    finally:
        for ws in sockets:
            try:
                ws.close()
            except Exception:
                pass
        opts.client.logout()
