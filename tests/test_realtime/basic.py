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
        assert auth.get("instance_kind") == "user", f"unexpected instance_kind: {auth.get('instance_kind')}"
        assert auth.get("instance_id") == uid, f"unexpected instance_id: {auth.get('instance_id')} vs {uid}"

        # Subscribe to the user-specific topic using external naming ("user:{id}")
        topic = f"user:{uid}"
        sub = ws.subscribe(topic, wait=True, timeout=5.0)
        assert sub.get("type") == "subscribed", f"unexpected subscribe response: {sub}"
        assert sub.get("topic") == topic, f"unexpected topic in subscribed: {sub.get('topic')}"

        # Ping/pong
        pong = ws.ping(wait=True, timeout=5.0)
        assert pong.get("type") == "pong", f"unexpected pong: {pong}"
        assert pong.get("instance_kind") == "user", f"unexpected instance_kind in pong: {pong}"

    finally:
        ws.close()


@th.django_unit_test("ws_notify_receive")
def test_ws_notify_receive(opts):
    from mojo.apps.realtime.utils import publish_to_instance


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
        assert auth.get("instance_kind") == "user", f"unexpected instance_kind: {auth.get('instance_kind')}"
        assert auth.get("instance_id") == uid, f"unexpected instance_id: {auth.get('instance_id')} vs {uid}"

        # Subscribe to the user topic
        topic = f"user:{uid}"
        sub = ws.subscribe(topic, wait=True, timeout=5.0)
        assert sub.get("type") == "subscribed", f"unexpected subscribe response: {sub}"
        assert sub.get("topic") == topic, f"unexpected topic in subscribed: {sub.get('topic')}"

        # Publish a notification to this instance (server-side)
        title = "TestIt Notification"
        message = "Hello from publish_to_instance"
        publish_to_instance("user", uid, dict(title=title, message=message, priority="high"))

        # Expect the notification on the client
        msg = ws.wait_for_type("notification", timeout=10.0)
        data = msg.data
        assert data.get("type") == "notification", f"unexpected ws message: {data}"
        assert data.get("topic") == topic, f"unexpected topic in notification: {data.get('topic')}"
        assert data.get("title") == title, f"unexpected title: {data.get('title')}"
        assert data.get("message") == message, f"unexpected message: {data.get('message')}"

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
