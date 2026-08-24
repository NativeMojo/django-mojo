"""DM-042: websocket connection hardening.

- short unauthenticated window advertised to clients (WS_UNAUTH_TIMEOUT)

The per-identity concurrency-cap test (ten sockets held open together — it
needs the server lock quiet) moved to
tests/test_realtime_extended_serial/connection_limits.py (maestro #2789),
alongside the pre-accept per-IP connect-rate gate tests that were already
there (maestro item #1839).
"""

TESTIT_TIER = "core"
from testit import helpers as th
from testit.ws_client import WsClient


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
