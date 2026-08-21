"""DM-042: websocket connection hardening.

- per-identity concurrency cap (live server, default WS_MAX_CONNECTIONS=10)
- short unauthenticated window advertised to clients (WS_UNAUTH_TIMEOUT)

The pre-accept per-IP connect-rate gate tests, which override
django.conf.settings in-process, live in
tests/test_realtime_extended_serial/connection_limits.py (maestro item #1839).
"""
import uuid as _uuid

from testit import helpers as th
from testit.ws_client import WsClient


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
