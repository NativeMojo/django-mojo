"""Chat delivery must observe membership loss even when disconnect fails."""
import asyncio

from testit import helpers as th

TESTIT_TIER = "core"


def _handler(user, topic):
    from mojo.apps.realtime.handler import WebSocketHandler

    handler = object.__new__(WebSocketHandler)
    handler.user = user
    handler.authenticated = True
    handler.subscribed_topics = {topic}
    handler.delivered = []
    handler.removed = []
    handler.errors = []

    async def send(message):
        handler.delivered.append(message)

    async def unsubscribe(value):
        handler.removed.append(value)
        handler.subscribed_topics.discard(value)

    handler.send_message = send
    handler.unsubscribe_from_topic = unsubscribe
    handler._log_exception = handler.errors.append
    return handler


def _deliver(handler, topic, kind="topic_message"):
    asyncio.run(handler.process_redis_message({
        "type": kind, "topic": topic, "timestamp": 123,
        "data": {"type": "chat_message", "body": "Private message"},
    }))


@th.django_unit_test()
@th.requires_app("mojo.apps.chat")
def test_existing_socket_stops_delivery_after_membership_removal(opts):
    from mojo.apps.account.models import User
    from mojo.apps.chat.models import ChatRoom, ChatMembership

    username = "test-realtime-chat-delivery"
    room_name = "test-realtime-chat-delivery-room"
    ChatRoom.objects.filter(name=room_name).delete()
    User.objects.filter(username=username).delete()
    user = User.objects.create(username=username)
    room = ChatRoom.objects.create(name=room_name, kind="group", group=None, user=None)
    try:
        member = ChatMembership.objects.create(room=room, user=user, role="member", status="active")
        topic = f"chat:{room.pk}"
        handler = _handler(user, topic)
        _deliver(handler, topic)
        assert len(handler.delivered) == 1, "An active member must receive chat"
        member.status = "muted"
        member.save(update_fields=["status"])
        _deliver(handler, topic)
        assert len(handler.delivered) == 2, "A muted member retains read access"

        member.delete()
        _deliver(handler, topic)
        assert len(handler.delivered) == 2, "Membership removal must suppress later chat without disconnect"
        assert handler.removed == [topic], "Revocation must unsubscribe the room"
        _deliver(handler, topic)
        assert len(handler.delivered) == 2, "A buffered frame after unsubscribe must remain suppressed"
    finally:
        room.delete()
        user.delete()


@th.django_unit_test()
def test_chat_authorization_error_fails_closed(opts):
    class FailingUser:
        def on_realtime_can_subscribe(self, topic):
            raise RuntimeError("Authorization storage unavailable")

    topic = "chat:123"
    handler = _handler(FailingUser(), topic)
    _deliver(handler, topic)
    assert not handler.delivered, "Authorization exceptions must never expose chat payloads"
    assert handler.removed == [topic], "Authorization exceptions must unsubscribe chat"
    assert handler.errors, "Authorization failure must be logged"


@th.django_unit_test()
def test_chat_requires_authenticated_identity_with_authorization_hook(opts):
    topic = "chat:123"
    handler = _handler(object(), topic)
    _deliver(handler, topic)
    assert not handler.delivered, "Missing authorization hook must fail closed for chat"
    assert handler.removed == [topic], "Missing authorization hook must unsubscribe chat"

    class PermittedUser:
        def on_realtime_can_subscribe(self, value):
            return True

    handler = _handler(PermittedUser(), topic)
    handler.authenticated = False
    _deliver(handler, topic)
    assert not handler.delivered, "Unauthenticated sockets must never receive chat"
    assert handler.removed == [topic], "Unauthenticated chat subscriptions must be removed"


@th.django_unit_test()
def test_non_chat_delivery_keeps_existing_behavior(opts):
    class UnexpectedAuthorization:
        def on_realtime_can_subscribe(self, topic):
            raise AssertionError("Non-chat delivery must not call the chat gate")

    handler = _handler(UnexpectedAuthorization(), "user:123")
    for kind in ("topic_message", "direct_message", "broadcast"):
        _deliver(handler, "user:123", kind)
    assert len(handler.delivered) == 3, "Existing non-chat delivery must remain unchanged"
    assert handler.delivered[0]["topic"] == "user:123", "Topic envelopes must be preserved"
    assert all(message["type"] == "message" for message in handler.delivered), "Messages retain their wrapper"
    assert not handler.removed and not handler.errors, "Non-chat delivery must not invoke chat authorization"


@th.django_unit_test()
@th.requires_app("mojo.apps.chat")
def test_open_socket_rechecks_current_account_state(opts):
    from mojo.apps.account.models import Group, User
    from mojo.apps.chat.models import ChatRoom, ChatMembership

    username = "test-realtime-chat-stale-user"
    room_name = "test-realtime-chat-stale-user-room"
    group_name = "test-realtime-chat-stale-user-group"
    ChatRoom.objects.filter(name=room_name).delete()
    Group.objects.filter(name=group_name).delete()
    User.objects.filter(username=username).delete()
    user = User.objects.create(username=username)
    group = Group.objects.create(name=group_name)
    room = ChatRoom.objects.create(name=room_name, kind="group", group=group, user=None)
    try:
        ChatMembership.objects.create(room=room, user=user, role="member", status="active")
        user.add_permission("manage_chat")
        topic = f"chat:{room.pk}"
        # The socket keeps the User loaded at connect; later changes land on
        # other copies of the row, exactly as an admin edit would.
        handler = _handler(User.objects.get(pk=user.pk), topic)
        _deliver(handler, topic)
        assert len(handler.delivered) == 1, "A permitted staff user must receive group-room chat"

        User.objects.get(pk=user.pk).remove_permission("manage_chat")
        _deliver(handler, topic)
        assert len(handler.delivered) == 1, "Removing the staff permission must stop delivery on an open socket"
        assert handler.removed == [topic], "Permission removal must unsubscribe the room"

        User.objects.get(pk=user.pk).add_permission("manage_chat")
        handler = _handler(User.objects.get(pk=user.pk), topic)
        _deliver(handler, topic)
        assert len(handler.delivered) == 1, "A re-granted staff user must receive chat"
        User.objects.filter(pk=user.pk).update(is_active=False)
        _deliver(handler, topic)
        assert len(handler.delivered) == 1, "A disabled account must stop receiving chat on an open socket"
        assert handler.removed == [topic], "Account deactivation must unsubscribe the room"
    finally:
        room.delete()
        group.delete()
        user.delete()
