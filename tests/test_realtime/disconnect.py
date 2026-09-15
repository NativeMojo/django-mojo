from testit import helpers as th
from testit.ws_client import WsClient

TESTIT_TIER = "bug"
TEST_PWORD = "testit##mojo"


@th.django_unit_test("ws_manager_disconnect_user")
def test_ws_manager_disconnect_user(opts):
    import time

    from mojo.apps import realtime
    from mojo.apps.account.models import User

    usernames = ("ws_disconnect_4519_target", "ws_disconnect_4519_other")
    User.objects.filter(username__in=usernames).delete()
    ws_url = WsClient.build_url_from_host(opts.host, path="ws/realtime/")
    targets = [WsClient(ws_url, logger=opts.logger) for _ in range(2)]
    other = WsClient(ws_url, logger=opts.logger)
    sockets = targets + [other]
    try:
        users = []
        for username in usernames:
            user = User(username=username, display_name=username,
                        email=f"{username}@example.com", is_email_verified=True)
            user.save_password(TEST_PWORD)
            users.append(user)
        target_user, other_user = users
        target_topic = f"user:{target_user.id}"
        other_topic = f"user:{other_user.id}"

        assert opts.client.login(target_user.username, TEST_PWORD), "target login failed"
        for ws in targets:
            ws.connect(timeout=10.0)
            auth = ws.authenticate(opts.client.access_token, timeout=10.0)
            assert auth.get("user_id") == target_user.id, "wrong target socket identity"
            ws.send_json({"type": "subscribe", "topic": target_topic})
            subscribed = ws.wait_for_type("subscribed", timeout=5.0)
            assert subscribed.data.get("topic") == target_topic, "target subscription failed"

        assert opts.client.login(other_user.username, TEST_PWORD), "unrelated login failed"
        other.connect(timeout=10.0)
        auth = other.authenticate(opts.client.access_token, timeout=10.0)
        assert auth.get("user_id") == other_user.id, "wrong unrelated socket identity"
        other.send_json({"type": "subscribe", "topic": other_topic})
        subscribed = other.wait_for_type("subscribed", timeout=5.0)
        assert subscribed.data.get("topic") == other_topic, "unrelated subscription failed"

        connection_ids = set(realtime.get_user_connections("user", target_user.id))
        other_ids = set(realtime.get_user_connections("user", other_user.id))
        assert len(connection_ids) == 2, f"expected two target connections: {connection_ids}"
        assert len(other_ids) == 1, f"expected one unrelated connection: {other_ids}"
        assert connection_ids <= set(realtime.get_topic_subscribers(target_topic)), \
            "both target connections must be registered on their topic"
        for connection_id in connection_ids | other_ids:
            assert realtime.get_redis_info(connection_id) is not None, \
                f"missing live connection record: {connection_id}"

        # Application data resembling a control command must remain ordinary data.
        payload = {"type": "disconnect", "reason": "forced_disconnect"}
        for connection_id in connection_ids:
            realtime.send_to_connection(connection_id, payload)
        for ws in targets:
            message = ws.wait_for_type("message", timeout=5.0)
            assert message.data.get("data") == payload, "disconnect payload must stay wrapped"
            ws.send_json({"type": "ping"})
            pong = ws.wait_for_type("pong", timeout=5.0)
            assert pong.data.get("user_type") == "user", "ordinary payload broke socket ping"
            assert not ws._closed_event.is_set(), "ordinary data must not close the socket"

        realtime.disconnect_user("user", target_user.id)
        for ws in targets:
            message = ws.wait_for_type("disconnect", timeout=5.0)
            assert message.data.get("reason") == "forced_disconnect", \
                f"expected top-level forced disconnect reason: {message.data}"
            # This must precede finally/close(): the server must close each socket.
            assert ws._closed_event.wait(timeout=5.0), "server did not close target socket"

        deadline = time.monotonic() + 5.0
        while True:
            remaining_ids = realtime.get_user_connections("user", target_user.id)
            online = realtime.is_online("user", target_user.id)
            topic_ids = set(realtime.get_topic_subscribers(target_topic))
            records = [connection_id for connection_id in connection_ids
                       if realtime.get_redis_info(connection_id) is not None]
            if not remaining_ids and not online and not (connection_ids & topic_ids) and not records:
                break
            assert time.monotonic() < deadline, (
                f"forced disconnect cleanup incomplete: connections={remaining_ids}, "
                f"online={online}, topic_members={topic_ids}, records={records}")
            time.sleep(0.05)

        # Repeating the command for an offline identity is harmless.
        assert realtime.disconnect_user("user", target_user.id) is None, \
            "offline disconnect should be a no-op"
        assert not realtime.is_online("user", target_user.id), "offline disconnect restored presence"
        assert realtime.get_user_connections("user", target_user.id) == [], \
            "offline disconnect created a connection"

        assert not other._closed_event.is_set(), "disconnect closed an unrelated user"
        other.send_json({"type": "ping"})
        pong = other.wait_for_type("pong", timeout=5.0)
        assert pong.data.get("user_type") == "user", "unrelated socket must still answer ping"
        delivered = {"message": "unrelated user remains connected"}
        realtime.send_to_user("user", other_user.id, delivered)
        message = other.wait_for_type("message", timeout=5.0)
        assert message.data.get("data") == delivered, "unrelated direct delivery failed"
        assert realtime.is_online("user", other_user.id), "unrelated user lost online presence"
        assert set(realtime.get_user_connections("user", other_user.id)) == other_ids, \
            "unrelated online membership changed"
        assert other_ids <= set(realtime.get_topic_subscribers(other_topic)), \
            "unrelated topic membership was removed"
        for connection_id in other_ids:
            assert realtime.get_redis_info(connection_id) is not None, \
                "unrelated connection record was removed"
    finally:
        for ws in sockets:
            ws.close()
        opts.client.logout()
        User.objects.filter(username__in=usernames).delete()
