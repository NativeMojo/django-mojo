from testit import helpers as th
from testit.ws_client import WsClient
import threading
import time

TEST_USER = "ws_test"
TEST_PWORD = "testit##mojo"


@th.django_unit_setup()
def setup_realtime_user(opts):
    from mojo.apps.account.models import User
    user = User.objects.filter(username=TEST_USER).last()
    if user is None:
        user = User(username=TEST_USER, display_name=TEST_USER, email=f"{TEST_USER}@example.com")
        user.save()
    user.save_password(TEST_PWORD)
    user.remove_all_permissions()
    user.is_staff = False
    user.is_superuser = False
    user.save()
    opts.ws_user_id = user.id


@th.django_unit_test("ws_request_response")
def test_ws_request_response(opts):
    """Send request via realtime.request(), client responds, Django gets the response."""
    from mojo.apps import realtime

    assert opts.client.login(TEST_USER, TEST_PWORD), "login failed"
    uid = opts.client.jwt_data.uid
    assert uid is not None, "missing uid from jwt"

    ws_url = WsClient.build_url_from_host(opts.host, path="ws/realtime/")
    ws = WsClient(ws_url, logger=opts.logger)
    try:
        ws.connect(timeout=10.0)
        auth = ws.authenticate(opts.client.access_token, wait=True, timeout=10.0)
        assert auth.get("type") == "auth_success", f"auth failed: {auth}"

        # Start request() in a background thread (it blocks on BLPOP)
        result = [None]

        def do_request():
            result[0] = realtime.request("user", uid, {"action": "confirm_delete"}, timeout=10)

        t = threading.Thread(target=do_request)
        t.start()

        # Client receives the request wrapped in a direct_message
        msg = ws.wait_for_type("message", timeout=5.0)
        inner = msg.data.get("data", {})
        assert inner.get("type") == "request", f"expected type=request, got {inner.get('type')}"

        request_id = inner.get("request_id")
        assert request_id, "missing request_id in request message"

        payload = inner.get("data", {})
        assert payload.get("action") == "confirm_delete", f"unexpected payload: {payload}"

        # Client responds with the same request_id
        ws.send_json({
            "type": "response",
            "request_id": request_id,
            "data": {"confirmed": True, "reason": "user approved"}
        })

        t.join(timeout=10)
        assert not t.is_alive(), "request thread did not finish"

        # Verify Django got the response
        assert result[0] is not None, "request() returned None instead of response"
        assert result[0].get("confirmed") is True, f"expected confirmed=True, got {result[0]}"
        assert result[0].get("reason") == "user approved", f"unexpected reason: {result[0].get('reason')}"

    finally:
        ws.close()


@th.django_unit_test("ws_request_timeout")
def test_ws_request_timeout(opts):
    """realtime.request() returns None when client doesn't respond within timeout."""
    from mojo.apps import realtime

    assert opts.client.login(TEST_USER, TEST_PWORD), "login failed"
    uid = opts.client.jwt_data.uid
    assert uid is not None, "missing uid from jwt"

    ws_url = WsClient.build_url_from_host(opts.host, path="ws/realtime/")
    ws = WsClient(ws_url, logger=opts.logger)
    try:
        ws.connect(timeout=10.0)
        auth = ws.authenticate(opts.client.access_token, wait=True, timeout=10.0)
        assert auth.get("type") == "auth_success", f"auth failed: {auth}"

        # Request with short timeout — client never responds
        start = time.time()
        response = realtime.request("user", uid, {"action": "will_timeout"}, timeout=2)
        elapsed = time.time() - start

        assert response is None, f"expected None on timeout, got {response}"
        assert elapsed >= 1.5, f"timed out too fast ({elapsed:.1f}s), expected ~2s"
        assert elapsed < 5.0, f"timed out too slow ({elapsed:.1f}s), expected ~2s"

    finally:
        ws.close()


@th.django_unit_test("ws_request_user_offline")
def test_ws_request_user_offline(opts):
    """realtime.request() returns None immediately when user has no connections."""
    from mojo.apps import realtime
    from mojo.helpers.redis.client import get_connection

    assert opts.client.login(TEST_USER, TEST_PWORD), "login failed"
    uid = opts.client.jwt_data.uid
    assert uid is not None, "missing uid from jwt"

    # Ensure user has no active connections
    redis_client = get_connection()
    redis_client.delete(f"realtime:online:user:{uid}")

    start = time.time()
    response = realtime.request("user", uid, {"action": "offline_test"}, timeout=5)
    elapsed = time.time() - start

    assert response is None, f"expected None for offline user, got {response}"
    assert elapsed < 1.0, f"should return immediately for offline user, took {elapsed:.1f}s"


@th.django_unit_test("ws_wait_for_event")
def test_ws_wait_for_event(opts):
    """wait_for_event() captures a matching message from the client."""
    from mojo.apps import realtime

    assert opts.client.login(TEST_USER, TEST_PWORD), "login failed"
    uid = opts.client.jwt_data.uid
    assert uid is not None, "missing uid from jwt"

    ws_url = WsClient.build_url_from_host(opts.host, path="ws/realtime/")
    ws = WsClient(ws_url, logger=opts.logger)
    try:
        ws.connect(timeout=10.0)
        auth = ws.authenticate(opts.client.access_token, wait=True, timeout=10.0)
        assert auth.get("type") == "auth_success", f"auth failed: {auth}"

        # Start wait_for_event in background thread (it blocks on BLPOP)
        result = [None]

        def do_wait():
            result[0] = realtime.wait_for_event(
                "user", uid,
                match={"message_type": "location_update"},
                timeout=10
            )

        t = threading.Thread(target=do_wait)
        t.start()

        # Give waiter time to register in Redis
        time.sleep(0.5)

        # Client sends a matching message
        ws.send_json({
            "message_type": "location_update",
            "lat": 40.7128,
            "lng": -74.0060
        })

        t.join(timeout=10)
        assert not t.is_alive(), "wait_for_event thread did not finish"

        # Verify Django captured the message
        assert result[0] is not None, "wait_for_event() returned None instead of matched message"
        assert result[0].get("message_type") == "location_update", \
            f"unexpected message_type: {result[0].get('message_type')}"
        assert result[0].get("lat") == 40.7128, f"unexpected lat: {result[0].get('lat')}"
        assert result[0].get("lng") == -74.0060, f"unexpected lng: {result[0].get('lng')}"

    finally:
        ws.close()


@th.django_unit_test("ws_wait_for_event_no_match")
def test_ws_wait_for_event_no_match(opts):
    """wait_for_event() ignores non-matching messages and times out."""
    from mojo.apps import realtime

    assert opts.client.login(TEST_USER, TEST_PWORD), "login failed"
    uid = opts.client.jwt_data.uid
    assert uid is not None, "missing uid from jwt"

    ws_url = WsClient.build_url_from_host(opts.host, path="ws/realtime/")
    ws = WsClient(ws_url, logger=opts.logger)
    try:
        ws.connect(timeout=10.0)
        auth = ws.authenticate(opts.client.access_token, wait=True, timeout=10.0)
        assert auth.get("type") == "auth_success", f"auth failed: {auth}"

        # Start wait_for_event looking for a specific message_type
        result = [None]

        def do_wait():
            result[0] = realtime.wait_for_event(
                "user", uid,
                match={"message_type": "target_event"},
                timeout=3
            )

        t = threading.Thread(target=do_wait)
        t.start()

        # Give waiter time to register in Redis
        time.sleep(0.5)

        # Send non-matching messages — these should NOT satisfy the waiter
        ws.send_json({"message_type": "wrong_type", "value": 1})
        ws.send_json({"message_type": "also_wrong", "value": 2})

        t.join(timeout=10)
        assert not t.is_alive(), "wait_for_event thread did not finish"

        assert result[0] is None, f"expected None when no matching message sent, got {result[0]}"

    finally:
        ws.close()
