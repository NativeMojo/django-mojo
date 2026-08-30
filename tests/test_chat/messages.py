"""
Tests for chat messages: send via REST history endpoint, edit, flag, moderation,
read receipts, unread counts, and DM messages.

Note: WebSocket send is tested indirectly via the handler. HTTP endpoints are
tested via opts.client since they hit the live server.
"""

TESTIT_TIER = "extended"
from testit import helpers as th
from testit.helpers import assert_true, assert_eq

TEST_EMAIL_1 = 'chat-msg-user1@example.com'
TEST_EMAIL_2 = 'chat-msg-user2@example.com'
TEST_EMAIL_ADMIN = 'chat-msg-admin@example.com'
TEST_PASSWORD = 'TestPass1!'


@th.django_unit_setup()
@th.requires_app("mojo.apps.chat")
def setup_messages(opts):
    from mojo.apps.account.models import User
    from mojo.apps.chat.models import ChatRoom, ChatMembership, ChatMessage, ChatReaction, ChatReadReceipt

    # Clean up
    User.objects.filter(email__in=[TEST_EMAIL_1, TEST_EMAIL_2, TEST_EMAIL_ADMIN]).delete()
    ChatRoom.objects.filter(name__startswith="test-msg-").delete()

    # Create users (mark verified so login works with REQUIRE_VERIFIED_EMAIL)
    opts.user1 = User.objects.create_user(
        username=TEST_EMAIL_1, email=TEST_EMAIL_1, password=TEST_PASSWORD,
    )
    opts.user1.is_email_verified = True
    opts.user1.save()
    opts.user2 = User.objects.create_user(
        username=TEST_EMAIL_2, email=TEST_EMAIL_2, password=TEST_PASSWORD,
    )
    opts.user2.is_email_verified = True
    opts.user2.save()
    opts.admin_user = User.objects.create_user(
        username=TEST_EMAIL_ADMIN, email=TEST_EMAIL_ADMIN, password=TEST_PASSWORD,
    )
    opts.admin_user.is_email_verified = True
    opts.admin_user.save()
    opts.admin_user.add_permission("manage_chat")

    # Create a group room with both users
    opts.room = ChatRoom.objects.create(
        name="test-msg-room", kind="group", user=opts.user1,
    )
    opts.room.rules = {
        "allow_urls": True,
        "allow_media": True,
        "allow_phone_numbers": True,
        "max_message_length": 4000,
        "disappearing_ttl": 0,
        "rate_limit": 10,
    }
    opts.room.save()

    ChatMembership.objects.create(room=opts.room, user=opts.user1, role="owner")
    ChatMembership.objects.create(room=opts.room, user=opts.user2, role="member")
    ChatMembership.objects.create(room=opts.room, user=opts.admin_user, role="admin")

    # Create some messages directly for history tests
    for i in range(5):
        ChatMessage.objects.create(
            room=opts.room, user=opts.user1,
            body=f"test message {i}",
        )


@th.django_unit_test()
def test_message_history(opts):
    """Fetch paginated message history."""
    opts.client.login(TEST_EMAIL_1, TEST_PASSWORD)
    resp = opts.client.get('/api/chat/room/messages', params={
        'room_id': opts.room.pk,
    })
    assert_eq(resp.status_code, 200, f"expected 200, got {resp.status_code}")
    assert_true(len(resp.json.data) >= 5, f"expected at least 5 messages, got {len(resp.json.data)}")

    # Verify newest first
    ids = [m["id"] for m in resp.json.data]
    assert_eq(ids, sorted(ids, reverse=True), "expected messages in newest-first order")


@th.django_unit_test()
def test_message_history_pagination(opts):
    """Test cursor-based pagination."""
    opts.client.login(TEST_EMAIL_1, TEST_PASSWORD)
    resp = opts.client.get('/api/chat/room/messages', params={
        'room_id': opts.room.pk,
        'limit': 2,
    })
    assert_eq(resp.status_code, 200, f"expected 200, got {resp.status_code}")
    assert_eq(len(resp.json.data), 2, "expected 2 messages")
    assert_true(resp.json.has_more, "expected has_more=True")
    assert_true(resp.json.cursor, "expected cursor")

    # Fetch next page
    resp2 = opts.client.get('/api/chat/room/messages', params={
        'room_id': opts.room.pk,
        'limit': 2,
        'before': resp.json.cursor,
    })
    assert_eq(resp2.status_code, 200, f"expected 200, got {resp2.status_code}")
    assert_eq(len(resp2.json.data), 2, "expected 2 messages on second page")

    # Ensure no overlap
    ids1 = {m["id"] for m in resp.json.data}
    ids2 = {m["id"] for m in resp2.json.data}
    assert_eq(len(ids1 & ids2), 0, "expected no overlap between pages")


@th.tier("core")
@th.django_unit_test()
def test_message_history_requires_membership(opts):
    """Non-member cannot fetch history."""
    from mojo.apps.account.models import User
    email = 'chat-msg-outsider@example.com'
    User.objects.filter(email=email).delete()
    outsider = User.objects.create_user(username=email, email=email, password=TEST_PASSWORD)
    outsider.is_email_verified = True
    outsider.save()

    opts.client.login(email, TEST_PASSWORD)
    resp = opts.client.get('/api/chat/room/messages', params={
        'room_id': opts.room.pk,
    })
    assert_eq(resp.status_code, 403, f"expected 403, got {resp.status_code}")


@th.django_unit_test()
def test_handler_send_message(opts):
    """Test sending a message via the chat handler directly."""
    from mojo.apps.chat.handler import handle_chat_message
    from mojo.apps.chat.models import ChatMessage

    count_before = ChatMessage.objects.filter(room=opts.room).count()

    result = handle_chat_message(opts.user1, {
        "type": "chat_message",
        "room_id": opts.room.pk,
        "body": "hello from handler test",
    })

    assert_eq(result["type"], "chat_message_ack", "expected ack response")
    assert_true(result.get("message_id"), "expected message_id in ack")

    count_after = ChatMessage.objects.filter(room=opts.room).count()
    assert_eq(count_after, count_before + 1, "expected one new message")

    opts.test_message_id = result["message_id"]


@th.django_unit_test()
def test_handler_send_empty_body(opts):
    """Empty body should be rejected."""
    from mojo.apps.chat.handler import handle_chat_message

    result = handle_chat_message(opts.user1, {
        "type": "chat_message",
        "room_id": opts.room.pk,
        "body": "",
    })
    assert_eq(result["type"], "error", "expected error for empty body")


@th.django_unit_test()
def test_handler_send_muted_user(opts):
    """Muted user cannot send messages."""
    from mojo.apps.chat.handler import handle_chat_message
    from mojo.apps.chat.models import ChatMembership

    ms = ChatMembership.objects.get(room=opts.room, user=opts.user2)
    ms.status = "muted"
    ms.save()

    result = handle_chat_message(opts.user2, {
        "type": "chat_message",
        "room_id": opts.room.pk,
        "body": "i am muted",
    })
    assert_eq(result["type"], "error", "expected error for muted user")
    assert_true("muted" in result["error"].lower(), "expected muted in error message")

    # Restore
    ms.status = "active"
    ms.save()


@th.django_unit_test()
def test_handler_edit_message(opts):
    """Author can edit their own message."""
    from mojo.apps.chat.handler import handle_chat_message
    from mojo.apps.chat.models import ChatMessage

    result = handle_chat_message(opts.user1, {
        "type": "chat_edit",
        "message_id": opts.test_message_id,
        "body": "edited message body",
    })
    assert_eq(result["type"], "chat_edit_ack", "expected edit ack")

    msg = ChatMessage.objects.get(pk=opts.test_message_id)
    assert_eq(msg.body, "edited message body", "expected body to be updated")
    assert_true(msg.edited_at, "expected edited_at to be set")


@th.django_unit_test()
def test_handler_edit_not_author(opts):
    """Non-author non-admin cannot edit someone else's message."""
    from mojo.apps.chat.handler import handle_chat_message

    result = handle_chat_message(opts.user2, {
        "type": "chat_edit",
        "message_id": opts.test_message_id,
        "body": "sneaky edit",
    })
    assert_eq(result["type"], "error", "expected error for non-author edit")


@th.django_unit_test()
def test_handler_flag_message(opts):
    """Admin can flag a message."""
    from mojo.apps.chat.handler import handle_chat_message
    from mojo.apps.chat.models import ChatMessage

    result = handle_chat_message(opts.admin_user, {
        "type": "chat_flag",
        "message_id": opts.test_message_id,
    })
    assert_eq(result["type"], "chat_flag_ack", "expected flag ack")

    msg = ChatMessage.objects.get(pk=opts.test_message_id)
    assert_true(msg.is_flagged, "expected message to be flagged")
    assert_eq(msg.flagged_by_id, opts.admin_user.pk, "expected flagged_by to be admin")


@th.django_unit_test()
def test_flagged_excluded_from_history(opts):
    """Flagged messages should not appear in normal history."""
    opts.client.login(TEST_EMAIL_1, TEST_PASSWORD)
    resp = opts.client.get('/api/chat/room/messages', params={
        'room_id': opts.room.pk,
    })
    assert_eq(resp.status_code, 200, f"expected 200, got {resp.status_code}")
    msg_ids = [m["id"] for m in resp.json.data]
    assert_true(
        opts.test_message_id not in msg_ids,
        "expected flagged message to be excluded from history",
    )


@th.django_unit_test()
def test_flagged_messages_endpoint(opts):
    """Moderator can view flagged messages."""
    opts.client.login(TEST_EMAIL_ADMIN, TEST_PASSWORD)
    resp = opts.client.get('/api/chat/room/flagged', params={
        'room_id': opts.room.pk,
    })
    assert_eq(resp.status_code, 200, f"expected 200, got {resp.status_code}")
    flagged_ids = [m["id"] for m in resp.json.data]
    assert_true(opts.test_message_id in flagged_ids, "expected flagged message in moderator view")


@th.tier("core")
@th.django_unit_test()
def test_handler_flag_requires_permission(opts):
    """Non-moderator cannot flag messages."""
    from mojo.apps.chat.handler import handle_chat_message
    from mojo.apps.chat.models import ChatMessage

    # Create a fresh message to flag
    msg = ChatMessage.objects.create(
        room=opts.room, user=opts.user1, body="flag me",
    )

    result = handle_chat_message(opts.user2, {
        "type": "chat_flag",
        "message_id": msg.pk,
    })
    assert_eq(result["type"], "error", "expected error for non-moderator flag")


@th.django_unit_test()
def test_handler_react_toggle(opts):
    """Test adding and removing a reaction (toggle)."""
    from mojo.apps.chat.handler import handle_chat_message
    from mojo.apps.chat.models import ChatMessage, ChatReaction

    msg = ChatMessage.objects.filter(room=opts.room, is_flagged=False).first()

    # Add reaction
    result = handle_chat_message(opts.user1, {
        "type": "chat_react",
        "message_id": msg.pk,
        "emoji": "\U0001f44d",
    })
    assert_eq(result["type"], "chat_react_ack", "expected react ack")
    assert_eq(result["action"], "added", "expected action=added")

    assert_true(
        ChatReaction.objects.filter(message=msg, user=opts.user1, emoji="\U0001f44d").exists(),
        "expected reaction to be persisted",
    )

    # Toggle off
    result = handle_chat_message(opts.user1, {
        "type": "chat_react",
        "message_id": msg.pk,
        "emoji": "\U0001f44d",
    })
    assert_eq(result["action"], "removed", "expected action=removed")

    assert_true(
        not ChatReaction.objects.filter(message=msg, user=opts.user1, emoji="\U0001f44d").exists(),
        "expected reaction to be removed",
    )


@th.django_unit_test()
def test_handler_read_receipts(opts):
    """Test marking messages as read in a group room."""
    from mojo.apps.chat.handler import handle_chat_message
    from mojo.apps.chat.models import ChatMessage, ChatReadReceipt

    # Get latest message from user1
    latest = ChatMessage.objects.filter(
        room=opts.room, user=opts.user1, is_flagged=False,
    ).order_by("-pk").first()

    result = handle_chat_message(opts.user2, {
        "type": "chat_read",
        "room_id": opts.room.pk,
        "up_to_message_id": latest.pk,
    })
    assert_eq(result["type"], "chat_read_ack", "expected read ack")

    # Verify read receipts were created (not for user2's own messages)
    receipts = ChatReadReceipt.objects.filter(
        message__room=opts.room, user=opts.user2,
    )
    assert_true(receipts.count() > 0, "expected read receipts to be created")


@th.django_unit_test()
def test_read_receipt_idempotent(opts):
    """Marking the same messages read twice should not create duplicates."""
    from mojo.apps.chat.handler import handle_chat_message
    from mojo.apps.chat.models import ChatMessage, ChatReadReceipt

    latest = ChatMessage.objects.filter(
        room=opts.room, user=opts.user1, is_flagged=False,
    ).order_by("-pk").first()

    count_before = ChatReadReceipt.objects.filter(
        message__room=opts.room, user=opts.user2,
    ).count()

    handle_chat_message(opts.user2, {
        "type": "chat_read",
        "room_id": opts.room.pk,
        "up_to_message_id": latest.pk,
    })

    count_after = ChatReadReceipt.objects.filter(
        message__room=opts.room, user=opts.user2,
    ).count()
    assert_eq(count_after, count_before, "expected no new receipts on duplicate read")


@th.django_unit_test()
def test_unread_counts(opts):
    """Test unread count endpoint."""
    from mojo.apps.chat.models import ChatMessage

    # Create a new message from user1
    ChatMessage.objects.create(
        room=opts.room, user=opts.user1, body="new unread message",
    )

    opts.client.login(TEST_EMAIL_2, TEST_PASSWORD)
    resp = opts.client.get('/api/chat/unread')
    assert_eq(resp.status_code, 200, f"expected 200, got {resp.status_code}")

    # Find our room in the counts
    room_count = None
    for item in resp.json.data:
        if item["room_id"] == opts.room.pk:
            room_count = item["unread_count"]
            break
    assert_true(room_count is not None, "expected room in unread counts")
    assert_true(room_count > 0, f"expected unread count > 0, got {room_count}")


@th.django_unit_test()
def test_read_via_rest(opts):
    """Mark messages as read via REST endpoint."""
    from mojo.apps.chat.models import ChatMessage

    latest = ChatMessage.objects.filter(
        room=opts.room, is_flagged=False,
    ).order_by("-pk").first()

    opts.client.login(TEST_EMAIL_2, TEST_PASSWORD)
    resp = opts.client.post('/api/chat/room/read', {
        'room_id': opts.room.pk,
        'up_to_message_id': latest.pk,
    })
    assert_eq(resp.status_code, 200, f"expected 200, got {resp.status_code}")


@th.django_unit_test()
def test_dm_flow(opts):
    """Create DM and verify message flow works."""
    opts.client.login(TEST_EMAIL_1, TEST_PASSWORD)
    resp = opts.client.post('/api/chat/dm', {
        'user_id': opts.user2.pk,
    })
    assert_eq(resp.status_code, 200, f"expected 200, got {resp.status_code}")
    dm_room_id = resp.json.data.id

    # Send message via handler
    from mojo.apps.chat.handler import handle_chat_message
    result = handle_chat_message(opts.user1, {
        "type": "chat_message",
        "room_id": dm_room_id,
        "body": "hey, this is a DM",
    })
    assert_eq(result["type"], "chat_message_ack", "expected ack for DM message")

    # Fetch history
    resp = opts.client.get('/api/chat/room/messages', params={
        'room_id': dm_room_id,
    })
    assert_eq(resp.status_code, 200, f"expected 200, got {resp.status_code}")
    assert_true(len(resp.json.data) >= 1, "expected at least 1 message in DM history")


@th.django_unit_test()
def test_handler_max_message_length(opts):
    """Messages exceeding max_message_length are rejected."""
    from mojo.apps.chat.handler import handle_chat_message

    long_body = "x" * 5000  # default limit is 4000

    result = handle_chat_message(opts.user1, {
        "type": "chat_message",
        "room_id": opts.room.pk,
        "body": long_body,
    })
    assert_eq(result["type"], "error", "expected error for long message")
    assert_true("max length" in result["error"].lower(), "expected max length error")


@th.tier("core")
@th.django_unit_test()
def test_handler_non_member_cannot_send(opts):
    """Non-member cannot send messages."""
    from mojo.apps.account.models import User
    from mojo.apps.chat.handler import handle_chat_message

    email = 'chat-msg-outsider2@example.com'
    User.objects.filter(email=email).delete()
    outsider = User.objects.create_user(username=email, email=email, password=TEST_PASSWORD)

    result = handle_chat_message(outsider, {
        "type": "chat_message",
        "room_id": opts.room.pk,
        "body": "sneaky message",
    })
    assert_eq(result["type"], "error", "expected error for non-member send")


@th.tier("core")
@th.django_unit_test()
def test_subscription_auth(opts):
    """Test on_realtime_can_subscribe for chat topics."""
    # Active member can subscribe
    can = opts.user1.on_realtime_can_subscribe(f"chat:{opts.room.pk}")
    assert_true(can, "expected active member can subscribe")

    # Non-member cannot subscribe
    from mojo.apps.account.models import User
    email = 'chat-msg-outsider3@example.com'
    User.objects.filter(email=email).delete()
    outsider = User.objects.create_user(username=email, email=email, password=TEST_PASSWORD)

    can = outsider.on_realtime_can_subscribe(f"chat:{opts.room.pk}")
    assert_true(not can, "expected non-member cannot subscribe")

    # Banned member cannot subscribe
    from mojo.apps.chat.models import ChatMembership
    ChatMembership.objects.create(room=opts.room, user=outsider, status="banned")
    can = outsider.on_realtime_can_subscribe(f"chat:{opts.room.pk}")
    assert_true(not can, "expected banned member cannot subscribe")


# ---------------------------------------------------------------------------
# client_key — idempotent send identity
# ---------------------------------------------------------------------------

def _make_room(name, user, rules=None):
    """Create a fresh room owned by `user`, deleting any prior run's copy."""
    from mojo.apps.chat.models import ChatRoom, ChatMembership

    ChatRoom.objects.filter(name=name).delete()
    room = ChatRoom.objects.create(name=name, kind="group", user=user)
    if rules is not None:
        room.rules = rules
        room.save()
    ChatMembership.objects.get_or_create(room=room, user=user, defaults={"role": "owner"})
    return room


@th.tier("core")
@th.django_unit_test()
def test_client_key_retry_returns_same_message(opts):
    """A resend with the same client_key acks the original, creating no second row."""
    from mojo.apps.chat.handler import handle_chat_message
    from mojo.apps.chat.models import ChatMessage

    frame = {
        "type": "chat_message",
        "room_id": opts.room.pk,
        "body": "idempotent hello",
        "client_key": "ck-retry-1",
    }

    first = handle_chat_message(opts.user1, dict(frame))
    assert_eq(first["type"], "chat_message_ack", f"expected ack, got {first}")
    assert_eq(first.get("client_key"), "ck-retry-1", "expected client_key echoed on ack")

    second = handle_chat_message(opts.user1, dict(frame))
    assert_eq(second["type"], "chat_message_ack", f"expected ack on retry, got {second}")
    assert_eq(
        second["message_id"], first["message_id"],
        "expected the retry to ack the original message_id")

    count = ChatMessage.objects.filter(
        room=opts.room, user=opts.user1, client_key="ck-retry-1").count()
    assert_eq(count, 1, f"expected exactly one stored message, got {count}")


@th.tier("core")
@th.django_unit_test()
def test_client_key_rebind_refused(opts):
    """Reusing a key with different content errors instead of dropping the message."""
    from mojo.apps.chat.handler import handle_chat_message
    from mojo.apps.chat.models import ChatMessage

    frame = {
        "type": "chat_message",
        "room_id": opts.room.pk,
        "body": "original body",
        "client_key": "ck-rebind-1",
    }
    first = handle_chat_message(opts.user1, dict(frame))
    assert_eq(first["type"], "chat_message_ack", f"expected ack, got {first}")

    frame["body"] = "a completely different body"
    second = handle_chat_message(opts.user1, dict(frame))
    assert_eq(second["type"], "error", f"expected error on rebind, got {second}")
    assert_true(
        "already bound" in second["error"],
        f"expected a rebind error, got {second['error']}")
    assert_eq(second.get("client_key"), "ck-rebind-1", "expected client_key echoed on error")

    rows = ChatMessage.objects.filter(
        room=opts.room, user=opts.user1, client_key="ck-rebind-1")
    assert_eq(rows.count(), 1, "expected no second row for a refused rebind")
    assert_eq(
        rows.first().body, "original body",
        "expected the original body to survive the refused rebind")


@th.tier("core")
@th.django_unit_test()
def test_client_key_absent_behaves_as_before(opts):
    """Sends with no client_key still create one message each, with no key echo."""
    from mojo.apps.chat.handler import handle_chat_message
    from mojo.apps.chat.models import ChatMessage

    count_before = ChatMessage.objects.filter(room=opts.room).count()

    first = handle_chat_message(opts.user1, {
        "type": "chat_message", "room_id": opts.room.pk, "body": "keyless one",
    })
    second = handle_chat_message(opts.user1, {
        "type": "chat_message", "room_id": opts.room.pk, "body": "keyless one",
    })

    assert_eq(first["type"], "chat_message_ack", f"expected ack, got {first}")
    assert_eq(second["type"], "chat_message_ack", f"expected ack, got {second}")
    assert_true(
        first["message_id"] != second["message_id"],
        "expected two distinct messages when no client_key is supplied")
    assert_true("client_key" not in first, "expected no client_key on a keyless ack")

    count_after = ChatMessage.objects.filter(room=opts.room).count()
    assert_eq(count_after, count_before + 2, "expected both keyless messages to persist")

    stored = ChatMessage.objects.get(pk=first["message_id"])
    assert_eq(stored.client_key, None, "expected a keyless message to store client_key=None")


@th.tier("core")
@th.django_unit_test()
def test_client_key_echoed_in_broadcast(opts):
    """The room broadcast carries the key, and a retry publishes nothing new."""
    from mojo.apps.chat.handler import _handle_send

    captured = []

    def capture(topic, payload):
        captured.append((topic, payload))

    frame = {
        "type": "chat_message",
        "room_id": opts.room.pk,
        "body": "broadcast me",
        "client_key": "ck-broadcast-1",
    }

    result = _handle_send(opts.user1, dict(frame), publisher=capture)
    assert_eq(result["type"], "chat_message_ack", f"expected ack, got {result}")
    assert_eq(len(captured), 1, f"expected exactly one broadcast, got {len(captured)}")

    topic, payload = captured[0]
    assert_eq(topic, opts.room.topic, f"expected publish on the room topic, got {topic}")
    assert_eq(
        payload.get("client_key"), "ck-broadcast-1",
        "expected client_key on the broadcast payload")

    retry = _handle_send(opts.user1, dict(frame), publisher=capture)
    assert_eq(retry["type"], "chat_message_ack", f"expected ack on retry, got {retry}")
    assert_eq(
        len(captured), 1,
        f"expected the retry to publish nothing, got {len(captured)} broadcasts")


@th.tier("core")
@th.django_unit_test()
def test_client_key_echoed_in_send_error(opts):
    """A rejected send echoes the client_key so the client can correlate it."""
    from mojo.apps.chat.handler import handle_chat_message
    from mojo.apps.chat.models import ChatMessage

    result = handle_chat_message(opts.user1, {
        "type": "chat_message",
        "room_id": opts.room.pk,
        "body": "x" * 5000,  # room max_message_length is 4000
        "client_key": "ck-error-1",
    })
    assert_eq(result["type"], "error", f"expected error for long message, got {result}")
    assert_eq(result.get("client_key"), "ck-error-1", "expected client_key echoed on error")

    assert_eq(
        ChatMessage.objects.filter(client_key="ck-error-1").count(), 0,
        "expected no row for a rejected send")


@th.tier("core")
@th.django_unit_test()
def test_client_key_invalid_is_rejected(opts):
    """Malformed client_keys are rejected, echo nothing, and store nothing."""
    from mojo.apps.chat.handler import handle_chat_message
    from mojo.apps.chat.models import ChatMessage

    count_before = ChatMessage.objects.filter(room=opts.room).count()

    bad_keys = [
        "a" * 65,            # too long
        12345,               # not a string
        "has a space",       # disallowed character
        "abc\n",             # trailing newline -- fullmatch, not $
    ]

    for bad in bad_keys:
        result = handle_chat_message(opts.user1, {
            "type": "chat_message",
            "room_id": opts.room.pk,
            "body": "should not persist",
            "client_key": bad,
        })
        assert_eq(result["type"], "error", f"expected error for client_key {bad!r}")
        assert_true(
            "client_key must be" in result["error"],
            f"expected a client_key validation error for {bad!r}, got {result['error']}")
        assert_true(
            "client_key" not in result,
            f"expected the offending client_key {bad!r} not to be echoed back")

    count_after = ChatMessage.objects.filter(room=opts.room).count()
    assert_eq(count_after, count_before, "expected no messages stored for invalid keys")
    assert_eq(
        ChatMessage.objects.filter(body="should not persist").count(), 0,
        "expected no message row for any invalid client_key")


@th.tier("core")
@th.django_unit_test()
def test_client_key_history_author_scoped(opts):
    """History returns client_key only on the requesting user's own messages."""
    from mojo.apps.chat.handler import handle_chat_message

    ack = handle_chat_message(opts.user1, {
        "type": "chat_message",
        "room_id": opts.room.pk,
        "body": "author scoped key",
        "client_key": "ck-history-1",
    })
    assert_eq(ack["type"], "chat_message_ack", f"expected ack, got {ack}")
    message_id = ack["message_id"]

    # The other member sees the message but not its key.
    opts.client.login(TEST_EMAIL_2, TEST_PASSWORD)
    resp = opts.client.get('/api/chat/room/messages', params={'room_id': opts.room.pk})
    assert_eq(resp.status_code, 200, f"expected 200, got {resp.status_code}")
    rows = [m for m in resp.json.data if m["id"] == message_id]
    assert_eq(len(rows), 1, "expected the message in the other member's history")
    assert_eq(
        rows[0]["client_key"], None,
        "expected another member's client_key to be withheld")

    # The author sees their own key.
    opts.client.login(TEST_EMAIL_1, TEST_PASSWORD)
    resp = opts.client.get('/api/chat/room/messages', params={'room_id': opts.room.pk})
    assert_eq(resp.status_code, 200, f"expected 200, got {resp.status_code}")
    rows = [m for m in resp.json.data if m["id"] == message_id]
    assert_eq(len(rows), 1, "expected the message in the author's own history")
    assert_eq(
        rows[0]["client_key"], "ck-history-1",
        "expected the author to see their own client_key")


@th.tier("core")
@th.django_unit_test()
def test_client_key_scoped_per_room(opts):
    """The same key in a different room is a different message."""
    from mojo.apps.chat.handler import handle_chat_message
    from mojo.apps.chat.models import ChatMessage

    other_room = _make_room("test-msg-key-room2", opts.user1)

    key = "ck-per-room-1"
    first = handle_chat_message(opts.user1, {
        "type": "chat_message", "room_id": opts.room.pk,
        "body": "same key, room one", "client_key": key,
    })
    second = handle_chat_message(opts.user1, {
        "type": "chat_message", "room_id": other_room.pk,
        "body": "same key, room two", "client_key": key,
    })

    assert_eq(first["type"], "chat_message_ack", f"expected ack in room one, got {first}")
    assert_eq(second["type"], "chat_message_ack", f"expected ack in room two, got {second}")
    assert_true(
        first["message_id"] != second["message_id"],
        "expected the same client_key in another room to create its own message")

    assert_eq(
        ChatMessage.objects.filter(
            room=other_room, user=opts.user1, client_key=key).count(), 1,
        "expected exactly one message stored in the second room")


@th.tier("core")
@th.django_unit_test()
def test_client_key_retry_skips_rate_limit(opts):
    """A retry is deduped before the rate limiter, so it is never rate limited."""
    from mojo.apps.chat.handler import handle_chat_message

    room = _make_room("test-msg-key-ratelimit", opts.user1, rules={"rate_limit": 1})

    frame = {
        "type": "chat_message",
        "room_id": room.pk,
        "body": "first and only",
        "client_key": "ck-ratelimit-1",
    }

    first = handle_chat_message(opts.user1, dict(frame))
    assert_eq(first["type"], "chat_message_ack", f"expected ack, got {first}")

    # Consume the (limit of 1) budget so the limiter is exhausted.
    blocked = handle_chat_message(opts.user1, {
        "type": "chat_message", "room_id": room.pk, "body": "second in the window",
    })
    assert_eq(blocked["type"], "error", f"expected the limiter to be exhausted, got {blocked}")
    assert_true(
        "Rate limit" in blocked["error"],
        f"expected a rate limit error, got {blocked['error']}")

    retry = handle_chat_message(opts.user1, dict(frame))
    assert_eq(
        retry["type"], "chat_message_ack",
        f"expected the retry to be deduped ahead of the rate limiter, got {retry}")
    assert_eq(
        retry["message_id"], first["message_id"],
        "expected the retry to ack the original message")


@th.tier("core")
@th.django_unit_test()
def test_client_key_unique_constraint_enforced(opts):
    """The database refuses two messages with the same (room, user, client_key)."""
    from django.db import IntegrityError, transaction
    from mojo.apps.chat.models import ChatMessage

    key = "ck-constraint-1"
    ChatMessage.objects.filter(
        room=opts.room, user=opts.user1, client_key=key).delete()

    ChatMessage.objects.create(
        room=opts.room, user=opts.user1, body="constraint one", client_key=key)

    raised = False
    try:
        with transaction.atomic():
            ChatMessage.objects.create(
                room=opts.room, user=opts.user1, body="constraint two", client_key=key)
    except IntegrityError:
        raised = True

    assert_true(raised, "expected IntegrityError on a duplicate (room, user, client_key)")
    assert_eq(
        ChatMessage.objects.filter(
            room=opts.room, user=opts.user1, client_key=key).count(), 1,
        "expected only the first message to survive")
