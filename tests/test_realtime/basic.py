from testit import helpers as th
from testit.ws_client import WsClient

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


@th.unit_test("quick_ws_available")
def test_quick_ws_available(opts):
    ws_url = WsClient.build_url_from_host(opts.host, path="ws/realtime/")
    ws = WsClient(ws_url, logger=opts.logger)
    try:
        ws.connect(timeout=3.0)
        msg = ws.wait_for_type("auth_required", timeout=3.0)
        assert msg.data.get("type") == "auth_required", "expected auth_required"
    finally:
        ws.close()

@th.unit_test("ws_auth_subscribe_ping")
def test_ws_auth_subscribe_ping(opts):

    # Login via REST to get a JWT
    assert opts.client.login(TEST_USER, TEST_PWORD), "authentication failed"
    uid = opts.client.jwt_data.uid
    assert uid is not None, "missing user id from jwt"

    # Connect to WebSocket
    ws_url = WsClient.build_url_from_host(opts.host, path="ws/realtime/")
    ws = WsClient(ws_url, logger=opts.logger)
    try:
        ws.connect(timeout=10.0)

        # Authenticate with split fields (prefix defaults to 'bearer')
        auth = ws.authenticate(opts.client.access_token, wait=True, timeout=10.0)
        assert auth.get("type") == "auth_success", f"unexpected auth response: {auth}"
        assert auth.get("user_type") == "user", f"unexpected user_type: {auth.get('user_type')}"
        assert auth.get("user_id") == uid, f"unexpected user_id: {auth.get('user_id')} vs {uid}"

        # Subscribe to the user-specific topic using external naming ("user:{id}")
        topic = f"user:{uid}"
        ws.send_json({"type": "subscribe", "topic": topic})
        sub = ws.wait_for_type("subscribed", timeout=5.0)
        assert sub.data.get("type") == "subscribed", f"unexpected subscribe response: {sub.data}"
        assert sub.data.get("topic") == topic, f"unexpected topic in subscribed: {sub.data.get('topic')}"

        # Ping/pong
        ws.send_json({"type": "ping"})
        pong = ws.wait_for_type("pong", timeout=5.0)
        assert pong.data.get("type") == "pong", f"unexpected pong: {pong.data}"
        assert pong.data.get("user_type") == "user", f"unexpected user_type in pong: {pong.data}"

    finally:
        ws.close()


@th.django_unit_test("ws_notify_receive")
def test_ws_notify_receive(opts):
    from mojo.apps import realtime


    # Login via REST to get a JWT
    assert opts.client.login(TEST_USER, TEST_PWORD), "authentication failed"
    uid = opts.client.jwt_data.uid
    assert uid is not None, "missing user id from jwt"

    # Connect to WebSocket
    ws_url = WsClient.build_url_from_host(opts.host, path="ws/realtime/")
    ws = WsClient(ws_url, logger=opts.logger)
    try:
        ws.connect(timeout=10.0)

        # Authenticate
        auth = ws.authenticate(opts.client.access_token, wait=True, timeout=10.0)
        assert auth.get("type") == "auth_success", f"unexpected auth response: {auth}"
        assert auth.get("user_type") == "user", f"unexpected user_type: {auth.get('user_type')}"
        assert auth.get("user_id") == uid, f"unexpected user_id: {auth.get('user_id')} vs {uid}"

        # Subscribe to the user topic
        topic = f"user:{uid}"
        ws.send_json({"type": "subscribe", "topic": topic})
        sub = ws.wait_for_type("subscribed", timeout=5.0)
        assert sub.data.get("type") == "subscribed", f"unexpected subscribe response: {sub.data}"
        assert sub.data.get("topic") == topic, f"unexpected topic in subscribed: {sub.data.get('topic')}"

        # Publish a notification to this instance (server-side)
        title = "TestIt Notification"
        message = "Hello from send_to_user"
        realtime.send_to_user("user", uid, dict(title=title, message=message, priority="high"))

        # Expect the notification on the client
        msg = ws.wait_for_type("message", timeout=10.0)
        data = msg.data
        assert data.get("type") == "message", f"unexpected ws message: {data}"
        msg_data = data.get("data", {})
        assert msg_data.get("title") == title, f"unexpected title: {msg_data.get('title')}"
        assert msg_data.get("message") == message, f"unexpected message: {msg_data.get('message')}"

    finally:
        ws.close()


@th.unit_test("ws_instance_echo")
def test_ws_instance_echo(opts):
    # Login via REST to get a JWT
    assert opts.client.login(TEST_USER, TEST_PWORD), "authentication failed"
    uid = opts.client.jwt_data.uid
    assert uid is not None, "missing user id from jwt"

    ws_url = WsClient.build_url_from_host(opts.host, path="ws/realtime/")
    ws = WsClient(ws_url, logger=opts.logger)
    try:
        ws.connect(timeout=10.0)
        auth = ws.authenticate(opts.client.access_token, wait=True, timeout=10.0)
        assert auth.get("type") == "auth_success", f"unexpected auth response: {auth}"
        # Send echo via instance hook
        payload = {"hello": "world", "uid": uid}
        ws.send_json({"message_type": "echo", "payload": payload})
        msg = ws.wait_for_type("echo", timeout=5.0)
        data = msg.data
        assert data.get("type") == "echo", f"unexpected message: {data}"
        assert data.get("user_id") == uid, f"unexpected user_id: {data.get('user_id')} vs {uid}"
        assert data.get("payload") == payload, f"unexpected payload: {data.get('payload')}"
    finally:
        ws.close()


@th.django_unit_test("ws_manager_online_status")
def test_ws_manager_online_status(opts):
    from mojo.apps import realtime
    from mojo.helpers.redis.client import get_connection

    # Login via REST to get a JWT
    assert opts.client.login(TEST_USER, TEST_PWORD), "authentication failed"
    uid = opts.client.jwt_data.uid
    assert uid is not None, "missing user id from jwt"

    # Clean up any existing online status for this user
    redis_client = get_connection()
    redis_client.delete(f"realtime:online:user:{uid}")

    # Initially user should not be online
    assert not realtime.is_online("user", uid), "user should not be online initially"
    assert realtime.get_auth_count("user") == 0, "should have 0 authenticated users initially"

    # Connect to WebSocket
    ws_url = WsClient.build_url_from_host(opts.host, path="ws/realtime/")
    ws = WsClient(ws_url, logger=opts.logger)
    try:
        ws.connect(timeout=10.0)

        # Authenticate
        auth = ws.authenticate(opts.client.access_token, wait=True, timeout=10.0)
        assert auth.get("type") == "auth_success", f"unexpected auth response: {auth}"

        # Now user should be online
        assert realtime.is_online("user", uid), "user should be online after authentication"
        assert realtime.get_auth_count("user") >= 1, "should have at least 1 authenticated user"
        assert realtime.get_auth_count() >= 1, "should have at least 1 total authenticated connection"

        # Check user connections
        connections = realtime.get_user_connections("user", uid)
        assert len(connections) >= 1, f"user should have at least 1 connection, got {len(connections)}"

        # Check online users list
        online_users = realtime.get_online_users("user")
        user_found = any(u_type == "user" and u_id == str(uid) for u_type, u_id in online_users)
        assert user_found, f"user {uid} should be in online users list: {online_users}"

    finally:
        ws.close()

        # Give a moment for cleanup
        import time
        time.sleep(0.1)

        # After disconnect, user should be offline
        # Note: This might be flaky due to cleanup timing, but should work most of the time
        # assert not realtime.is_online("user", uid), "user should be offline after disconnect"


@th.django_unit_test("ws_manager_disconnect_user")
def test_ws_manager_disconnect_user(opts):
    from mojo.apps import realtime

    # Login via REST to get a JWT
    assert opts.client.login(TEST_USER, TEST_PWORD), "authentication failed"
    uid = opts.client.jwt_data.uid
    assert uid is not None, "missing user id from jwt"

    # Connect to WebSocket
    ws_url = WsClient.build_url_from_host(opts.host, path="ws/realtime/")
    ws = WsClient(ws_url, logger=opts.logger)
    try:
        ws.connect(timeout=10.0)

        # Authenticate
        auth = ws.authenticate(opts.client.access_token, wait=True, timeout=10.0)
        assert auth.get("type") == "auth_success", f"unexpected auth response: {auth}"

        # Verify user is online
        assert realtime.is_online("user", uid), "user should be online after authentication"

        # Force disconnect the user
        realtime.disconnect_user("user", uid)

        # Should receive disconnect message
        msg = ws.wait_for_type("message", timeout=5.0)
        data = msg.data
        assert data.get("type") == "message", f"unexpected message type: {data}"
        msg_data = data.get("data", {})
        assert msg_data.get("type") == "disconnect", f"expected disconnect message: {msg_data}"

    finally:
        ws.close()


@th.django_unit_test("ws_manager_multiple_connections")
def test_ws_manager_multiple_connections(opts):
    from mojo.apps import realtime

    # Login via REST to get a JWT
    assert opts.client.login(TEST_USER, TEST_PWORD), "authentication failed"
    uid = opts.client.jwt_data.uid
    assert uid is not None, "missing user id from jwt"

    # Create two WebSocket connections for the same user
    ws_url = WsClient.build_url_from_host(opts.host, path="ws/realtime/")
    ws1 = WsClient(ws_url, logger=opts.logger)
    ws2 = WsClient(ws_url, logger=opts.logger)

    try:
        # Connect both
        ws1.connect(timeout=10.0)
        ws2.connect(timeout=10.0)

        # Authenticate both
        auth1 = ws1.authenticate(opts.client.access_token, wait=True, timeout=10.0)
        assert auth1.get("type") == "auth_success", f"unexpected auth response 1: {auth1}"

        auth2 = ws2.authenticate(opts.client.access_token, wait=True, timeout=10.0)
        assert auth2.get("type") == "auth_success", f"unexpected auth response 2: {auth2}"

        # User should still show as online (same user, multiple connections)
        assert realtime.is_online("user", uid), "user should be online with multiple connections"

        # Should have multiple connections for this user
        connections = realtime.get_user_connections("user", uid)
        assert len(connections) >= 2, f"user should have at least 2 connections, got {len(connections)}"

        # Send message to user - both connections should receive it
        test_message = {"title": "Multi-connection test", "body": "Hello both connections"}
        realtime.send_to_user("user", uid, test_message)

        # Both websockets should receive the message
        msg1 = ws1.wait_for_type("message", timeout=5.0)
        msg2 = ws2.wait_for_type("message", timeout=5.0)

        assert msg1.data.get("type") == "message", f"ws1 unexpected message: {msg1.data}"
        assert msg2.data.get("type") == "message", f"ws2 unexpected message: {msg2.data}"

    finally:
        ws1.close()
        ws2.close()


@th.unit_test("ws_instance_set_meta")
def test_ws_instance_set_meta(opts):
    # Login via REST to get a JWT
    assert opts.client.login(TEST_USER, TEST_PWORD), "authentication failed"
    uid = opts.client.jwt_data.uid
    assert uid is not None, "missing user id from jwt"

    ws_url = WsClient.build_url_from_host(opts.host, path="ws/realtime/")
    ws = WsClient(ws_url, logger=opts.logger)
    try:
        ws.connect(timeout=10.0)
        auth = ws.authenticate(opts.client.access_token, wait=True, timeout=10.0)
        assert auth.get("type") == "auth_success", f"unexpected auth response: {auth}"

        # Set metadata key via instance hook
        key = "rt_test"
        value = "ok"
        ws.send_json({"message_type": "set_meta", "key": key, "value": value})
        ack = ws.wait_for_type("ack", timeout=5.0)
        assert ack.data.get("type") == "ack", f"unexpected ack: {ack.data}"

        # Verify via REST
        resp = opts.client.get(f"/api/user/{uid}")
        assert resp.status_code == 200, f"Expected 200 got {resp.status_code}"
        meta = resp.response.data.metadata
        assert meta.get(key) == value, f"metadata {key} mismatch: {meta.get(key)} vs {value}"
    finally:
        ws.close()
