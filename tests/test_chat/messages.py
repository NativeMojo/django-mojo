"""
Tests for chat messages: send via REST history endpoint, edit, flag, moderation,
read receipts, unread counts, and DM messages.

Note: WebSocket send is tested indirectly via the handler. HTTP endpoints are
tested via opts.client since they hit the live server.
"""
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
