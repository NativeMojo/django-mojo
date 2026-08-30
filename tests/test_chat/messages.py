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
# A THIRD user, used only by the join-cutoff tests. Deliberately not user1/user2:
# on_chat_dm's reuse lookup filters only on user + kind="direct", so a
# user1<->user2 direct room here would be silently handed to test_dm_flow.
TEST_EMAIL_DM = 'chat-msg-dmuser@example.com'
TEST_PASSWORD = 'TestPass1!'


@th.django_unit_setup()
@th.requires_app("mojo.apps.chat")
def setup_messages(opts):
    from mojo.apps.account.models import User
    from mojo.apps.chat.models import ChatRoom, ChatMembership, ChatMessage, ChatReaction, ChatReadReceipt

    # Clean up
    User.objects.filter(email__in=[
        TEST_EMAIL_1, TEST_EMAIL_2, TEST_EMAIL_ADMIN, TEST_EMAIL_DM,
    ]).delete()
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
    opts.dm_user = User.objects.create_user(
        username=TEST_EMAIL_DM, email=TEST_EMAIL_DM, password=TEST_PASSWORD,
    )
    opts.dm_user.is_email_verified = True
    opts.dm_user.save()

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


# ---------------------------------------------------------------------------
# Join-time history cutoff (item #3378)
#
# `ChatMembership.joined_at` bounds what a member may read in every room kind
# except `channel`. A hard-deleted-and-recreated membership row re-stamps
# joined_at, so a re-added member must not see what they missed. These tests
# build the membership churn at the model layer, which is where tenant
# products drive it -- no shipped endpoint creates a direct-room membership.
#
# `created` and `joined_at` are both auto_now_add and cannot be set at
# create(); every timestamp below is placed with .update(), never inferred
# from statement ordering.
# ---------------------------------------------------------------------------


def _cutoff_room(name, kind, owner):
    """A room of `kind` whose owner membership predates every message in it.

    Returns (room, t_old) -- `t_old` is the timestamp the caller should stamp
    on the pre-cutoff messages.
    """
    from datetime import timedelta
    from mojo.helpers import dates
    from mojo.apps.chat.models import ChatRoom, ChatMembership

    ChatRoom.objects.filter(name=name).delete()
    room = ChatRoom.objects.create(name=name, kind=kind, user=owner)
    ChatMembership.objects.create(room=room, user=owner, role="owner")
    # The founding membership is backdated ahead of the messages, so this test
    # asserts the cutoff and not merely the order two ORM writes happened in.
    ChatMembership.objects.filter(room=room, user=owner).update(
        joined_at=dates.utcnow() - timedelta(hours=2))
    return room, dates.utcnow() - timedelta(hours=1)


def _old_messages(room, author, at, count=2):
    """`count` messages in `room` stamped `at`, returned newest-id last."""
    from mojo.apps.chat.models import ChatMessage

    ids = []
    for i in range(count):
        ids.append(ChatMessage.objects.create(
            room=room, user=author, body=f"before the cutoff {i}").pk)
    ChatMessage.objects.filter(pk__in=ids).update(created=at)
    return ids


def _rejoin(room, user, at):
    """Hard-delete and recreate `user`'s membership, stamping joined_at `at`."""
    from mojo.apps.chat.models import ChatMembership

    ChatMembership.objects.filter(room=room, user=user).delete()
    ChatMembership.objects.create(room=room, user=user, role="member")
    ChatMembership.objects.filter(room=room, user=user).update(joined_at=at)


def _history_ids(opts, email, room_id):
    """The message ids `email` sees in `room_id` via the history endpoint."""
    opts.client.login(email, TEST_PASSWORD)
    resp = opts.client.get('/api/chat/room/messages', params={'room_id': room_id})
    assert_eq(resp.status_code, 200, f"expected 200 from history, got {resp.status_code}")
    return [m["id"] for m in resp.json.data]


@th.tier("bug")
@th.django_unit_test()
def test_direct_room_rejoin_history_cutoff(opts):
    """A re-added direct-room member reads only messages sent after they rejoined."""
    from datetime import timedelta
    from mojo.helpers import dates
    from mojo.apps.chat.models import ChatMessage, ChatMembership

    room, t_old = _cutoff_room("test-msg-dm-cutoff", "direct", opts.user1)
    ChatMembership.objects.create(room=room, user=opts.dm_user, role="member")
    old_ids = _old_messages(room, opts.user1, t_old)

    # What a tenant product does on a remove/re-add: hard delete, recreate.
    _rejoin(room, opts.dm_user, dates.utcnow() - timedelta(minutes=10))

    new_id = ChatMessage.objects.create(
        room=room, user=opts.user1, body="after the rejoin").pk

    opts.dm_cutoff_room_id = room.pk
    opts.dm_cutoff_new_message_id = new_id

    seen = _history_ids(opts, TEST_EMAIL_DM, room.pk)
    assert_eq(
        seen, [new_id],
        f"expected the re-added member to see only the post-rejoin message, got {seen}")

    # The founding participant is untouched by the bound.
    seen = set(_history_ids(opts, TEST_EMAIL_1, room.pk))
    assert_eq(
        seen, set(old_ids + [new_id]),
        f"expected the founding member to still see all three messages, got {sorted(seen)}")


@th.tier("bug")
@th.django_unit_test()
def test_arbitrary_kind_room_rejoin_cutoff(opts):
    """The cutoff covers unknown room kinds, not just direct/group.

    `ChatRoom.kind` is a caller-settable CharField -- `choices` is never
    enforced and CREATE_PERMS is ["authenticated"] -- so a room created with an
    arbitrary kind must be bound too. An allowlist of ("direct", "group")
    fails open here; excluding only "channel" fails closed.
    """
    from datetime import timedelta
    from mojo.helpers import dates
    from mojo.apps.chat.models import ChatMessage, ChatMembership

    room, t_old = _cutoff_room("test-msg-clubhouse-cutoff", "clubhouse", opts.user1)
    ChatMembership.objects.create(room=room, user=opts.dm_user, role="member")
    old_ids = _old_messages(room, opts.user1, t_old)

    _rejoin(room, opts.dm_user, dates.utcnow() - timedelta(minutes=10))

    new_id = ChatMessage.objects.create(
        room=room, user=opts.user1, body="after the rejoin").pk

    seen = _history_ids(opts, TEST_EMAIL_DM, room.pk)
    assert_eq(
        seen, [new_id],
        f"expected an arbitrary-kind room to bound the re-added member to the "
        f"post-rejoin message, got {seen}")

    seen = set(_history_ids(opts, TEST_EMAIL_1, room.pk))
    assert_eq(
        seen, set(old_ids + [new_id]),
        f"expected the founding member to still see all three messages, got {sorted(seen)}")


@th.django_unit_test()
def test_group_room_join_cutoff(opts):
    """A member added to a group room after messages exist sees only newer ones."""
    from datetime import timedelta
    from mojo.helpers import dates
    from mojo.apps.chat.models import ChatMessage, ChatMembership

    room, t_old = _cutoff_room("test-msg-group-cutoff", "group", opts.user1)
    old_ids = _old_messages(room, opts.user1, t_old)

    ChatMembership.objects.create(room=room, user=opts.dm_user, role="member")
    ChatMembership.objects.filter(room=room, user=opts.dm_user).update(
        joined_at=dates.utcnow() - timedelta(minutes=10))

    new_id = ChatMessage.objects.create(
        room=room, user=opts.user1, body="after the join").pk

    seen = _history_ids(opts, TEST_EMAIL_DM, room.pk)
    assert_eq(
        seen, [new_id],
        f"expected the late joiner to see only the post-join message, got {seen}")

    seen = set(_history_ids(opts, TEST_EMAIL_1, room.pk))
    assert_eq(
        seen, set(old_ids + [new_id]),
        f"expected the founding member to still see all three messages, got {sorted(seen)}")


@th.django_unit_test()
def test_channel_room_has_no_cutoff(opts):
    """Channels are public: a late joiner still reads the full history."""
    from datetime import timedelta
    from mojo.helpers import dates
    from mojo.apps.chat.models import ChatMessage, ChatMembership

    room, t_old = _cutoff_room("test-msg-channel-nocutoff", "channel", opts.user1)
    old_ids = _old_messages(room, opts.user1, t_old)

    ChatMembership.objects.create(room=room, user=opts.dm_user, role="member")
    ChatMembership.objects.filter(room=room, user=opts.dm_user).update(
        joined_at=dates.utcnow() - timedelta(minutes=10))

    new_id = ChatMessage.objects.create(
        room=room, user=opts.user1, body="after the join").pk

    seen = set(_history_ids(opts, TEST_EMAIL_DM, room.pk))
    assert_eq(
        seen, set(old_ids + [new_id]),
        f"expected a channel joiner to see the full history, got {sorted(seen)}")


@th.django_unit_test()
def test_direct_room_rejoin_unread_count(opts):
    """The unread badge counts only what the history endpoint would return."""
    room_id = opts.dm_cutoff_room_id

    opts.client.login(TEST_EMAIL_DM, TEST_PASSWORD)
    resp = opts.client.get('/api/chat/unread')
    assert_eq(resp.status_code, 200, f"expected 200, got {resp.status_code}")

    entry = None
    for item in resp.json.data:
        if item["room_id"] == room_id:
            entry = item
            break
    assert_true(entry is not None, "expected the re-joined DM room in the unread counts")
    assert_eq(
        entry["unread_count"], 1,
        f"expected only the post-rejoin message to be unread, got {entry['unread_count']}")


# ---------------------------------------------------------------------------
# Read-state resolution and explicit reactions (item #3379)
#
# `chat_read` / POST room/read used to accept any integer as
# `up_to_message_id`, write receipts for messages the caller was never
# entitled to see, stamp `last_read_at = utcnow()`, and republish the raw
# number to the room. `chat_react` was a blind toggle on a message looked up
# with no room scope at all. Both are now resolved through
# `services.read_state`, whose bound is the JOIN bound -- deliberately not the
# disappearing-message TTL; see `test_read_target_past_ttl_still_receipts`.
# ---------------------------------------------------------------------------


def _read_room(opts, name, kind="group", rules=None, members=None):
    """A fresh room of `kind` with user1 as owner and `members` joined.

    Deletes any prior run's copy first -- these tests run against a long-lived
    database, not a fresh one.
    """
    from mojo.apps.chat.models import ChatRoom, ChatMembership

    ChatRoom.objects.filter(name=name).delete()
    room = ChatRoom.objects.create(name=name, kind=kind, user=opts.user1)
    if rules is not None:
        room.rules = rules
        room.save()
    ChatMembership.objects.create(room=room, user=opts.user1, role="owner")
    for member in (members if members is not None else [opts.user2]):
        ChatMembership.objects.create(room=room, user=member, role="member")
    return room


def _say(room, user, body):
    """One message in `room`, returned as a saved ChatMessage."""
    from mojo.apps.chat.models import ChatMessage
    return ChatMessage.objects.create(room=room, user=user, body=body)


def _read_frame(room, up_to):
    return {"type": "chat_read", "room_id": room.pk, "up_to_message_id": up_to}


@th.tier("core")
@th.django_unit_test()
def test_banned_member_cannot_mark_read(opts):
    """A banned member gets an error and writes no read state over WebSocket."""
    from mojo.apps.chat.handler import handle_chat_message
    from mojo.apps.chat.models import ChatMembership, ChatReadReceipt

    room = _read_room(opts, "test-msg-read-banned")
    msg = _say(room, opts.user1, "you cannot ack this")

    ms = ChatMembership.objects.get(room=room, user=opts.user2)
    ms.status = "banned"
    ms.save(update_fields=["status"])
    try:
        result = handle_chat_message(opts.user2, _read_frame(room, msg.pk))
        assert_eq(result["type"], "error", f"expected an error for a banned member, got {result}")
    finally:
        ms.status = "active"
        ms.save(update_fields=["status"])

    assert_eq(
        ChatReadReceipt.objects.filter(message__room=room, user=opts.user2).count(), 0,
        "expected a banned member to write no read receipts")
    ms.refresh_from_db()
    assert_true(
        ms.last_read_at is None,
        f"expected last_read_at to stay unset for a banned member, got {ms.last_read_at}")


@th.tier("core")
@th.django_unit_test()
def test_banned_member_cannot_mark_read_via_rest(opts):
    """The REST read path refuses a banned member too, not just the handler."""
    from mojo.apps.chat.models import ChatMembership, ChatReadReceipt

    room = _read_room(opts, "test-msg-read-banned-rest")
    msg = _say(room, opts.user1, "you cannot ack this either")

    ms = ChatMembership.objects.get(room=room, user=opts.user2)
    ms.status = "banned"
    ms.save(update_fields=["status"])
    try:
        opts.client.login(TEST_EMAIL_2, TEST_PASSWORD)
        resp = opts.client.post('/api/chat/room/read', {
            'room_id': room.pk,
            'up_to_message_id': msg.pk,
        })
        assert_eq(
            resp.status_code, 404,
            f"expected 404 for a banned member over REST, got {resp.status_code}")
    finally:
        ms.status = "active"
        ms.save(update_fields=["status"])

    assert_eq(
        ChatReadReceipt.objects.filter(message__room=room, user=opts.user2).count(), 0,
        "expected no read receipts from a banned member over REST")


@th.django_unit_test()
def test_muted_member_can_mark_read(opts):
    """A muted member still marks read -- mute stops sending, not reading."""
    from mojo.apps.chat.handler import handle_chat_message
    from mojo.apps.chat.models import ChatMembership, ChatReadReceipt

    room = _read_room(opts, "test-msg-read-muted")
    msg = _say(room, opts.user1, "a muted member may ack this")

    ms = ChatMembership.objects.get(room=room, user=opts.user2)
    ms.status = "muted"
    ms.save(update_fields=["status"])
    try:
        result = handle_chat_message(opts.user2, _read_frame(room, msg.pk))
        assert_eq(result["type"], "chat_read_ack", f"expected a read ack for a muted member, got {result}")
        assert_eq(
            result["up_to_message_id"], msg.pk,
            "expected the muted member's read to resolve to the message")
    finally:
        ms.status = "active"
        ms.save(update_fields=["status"])

    assert_true(
        ChatReadReceipt.objects.filter(message=msg, user=opts.user2).exists(),
        "expected a muted member's read receipt to be written")


@th.tier("core")
@th.django_unit_test()
def test_read_clamps_out_of_range_target(opts):
    """An id far above anything in the room resolves to the newest real message."""
    from mojo.apps.chat.handler import handle_chat_message
    from mojo.apps.chat.models import ChatMembership, ChatMessage

    room = _read_room(opts, "test-msg-read-clamp")
    _say(room, opts.user1, "one")
    newest = _say(room, opts.user1, "two")

    bogus = ChatMessage.objects.order_by("-pk").first().pk + 10000
    result = handle_chat_message(opts.user2, _read_frame(room, bogus))

    assert_eq(result["type"], "chat_read_ack", f"expected a read ack, got {result}")
    assert_eq(
        result["up_to_message_id"], newest.pk,
        f"expected the bogus id {bogus} to clamp to {newest.pk}, "
        f"got {result['up_to_message_id']}")

    ms = ChatMembership.objects.get(room=room, user=opts.user2)
    newest.refresh_from_db()
    assert_eq(
        ms.last_read_at, newest.created,
        "expected last_read_at to come from the resolved message, not utcnow()")


@th.django_unit_test()
def test_read_clamps_foreign_room_message(opts):
    """A message id from another room never earns a receipt in this one."""
    from mojo.apps.chat.handler import handle_chat_message
    from mojo.apps.chat.models import ChatReadReceipt

    room = _read_room(opts, "test-msg-read-clamp-mine")
    mine = _say(room, opts.user1, "in my room")

    # Created second, so its pk is above everything in `room`.
    other = _read_room(opts, "test-msg-read-clamp-other", members=[opts.dm_user])
    foreign = _say(other, opts.user1, "in a room user2 is not in")

    result = handle_chat_message(opts.user2, _read_frame(room, foreign.pk))

    assert_eq(result["type"], "chat_read_ack", f"expected a read ack, got {result}")
    assert_eq(
        result["up_to_message_id"], mine.pk,
        f"expected the foreign id {foreign.pk} to clamp into the caller's own "
        f"room, got {result['up_to_message_id']}")
    assert_true(
        not ChatReadReceipt.objects.filter(message=foreign, user=opts.user2).exists(),
        "expected no receipt on a message in a room the caller is not in")


@th.tier("core")
@th.django_unit_test()
def test_read_rejects_non_numeric_target(opts):
    """A non-numeric up_to_message_id is a clean answer, not a 500."""
    room = _read_room(opts, "test-msg-read-nonnumeric")
    _say(room, opts.user1, "unreachable by a bad id")

    opts.client.login(TEST_EMAIL_2, TEST_PASSWORD)
    resp = opts.client.post('/api/chat/room/read', {
        'room_id': room.pk,
        'up_to_message_id': 'not-a-number',
    })
    assert_eq(
        resp.status_code, 200,
        f"expected 200 for a non-numeric target, got {resp.status_code}: {resp.json}")
    assert_true(
        "up_to_message_id" in resp.json.data,
        f"expected the resolved id key on the response, got {resp.json.data}")
    assert_true(
        resp.json.data.get("up_to_message_id") is None,
        f"expected a null resolved id, got {resp.json.data.get('up_to_message_id')}")

    from mojo.apps.chat.models import ChatReadReceipt
    assert_eq(
        ChatReadReceipt.objects.filter(message__room=room, user=opts.user2).count(), 0,
        "expected an unresolvable target to write nothing")


@th.tier("core")
@th.django_unit_test()
def test_read_target_past_ttl_still_receipts(opts):
    """A read whose target has aged past the room TTL still lands.

    The resolution bound is the JOIN bound, never the disappearing-message TTL.
    TTL expiry is monotonic in age and age is monotonic in pk, so once a
    message has aged out every message below it has too -- a TTL-bounded
    `pk__lte` lookup is EMPTY, not clamped. Resolving through it would throw
    the read away in exactly the race the clamp exists to survive: no receipt,
    no last_read_at, no broadcast, and a sender whose read indicator never
    updates for a message the other party genuinely read.
    """
    from datetime import timedelta
    from mojo.helpers import dates
    from mojo.apps.chat.handler import handle_chat_message
    from mojo.apps.chat.models import ChatMembership, ChatMessage, ChatReadReceipt
    from mojo.apps.chat.services.messages import visible_messages

    room = _read_room(
        opts, "test-msg-read-ttl", rules={"disappearing_ttl": 60})
    ChatMembership.objects.filter(room=room).update(
        joined_at=dates.utcnow() - timedelta(hours=3))

    aged = _say(room, opts.user1, "aged out of the TTL window")
    ChatMessage.objects.filter(pk=aged.pk).update(
        created=dates.utcnow() - timedelta(hours=2))
    aged.refresh_from_db()

    ms = ChatMembership.objects.get(room=room, user=opts.user2)
    assert_eq(
        visible_messages(room, ms).count(), 0,
        "expected the message to be past the room TTL for this test to mean anything")

    result = handle_chat_message(opts.user2, _read_frame(room, aged.pk))

    assert_eq(result["type"], "chat_read_ack", f"expected a read ack, got {result}")
    assert_eq(
        result["up_to_message_id"], aged.pk,
        "expected a TTL-expired target to still resolve to itself")
    assert_true(
        ChatReadReceipt.objects.filter(message=aged, user=opts.user2).exists(),
        "expected a receipt for a message that aged out after it was read")

    ms.refresh_from_db()
    assert_eq(
        ms.last_read_at, aged.created,
        "expected last_read_at to advance to the aged-out target's created")


@th.django_unit_test()
def test_read_last_read_at_matches_target_created(opts):
    """last_read_at is the resolved message's created, not the clock."""
    from mojo.apps.chat.handler import handle_chat_message
    from mojo.apps.chat.models import ChatMembership

    room = _read_room(opts, "test-msg-read-lastread")
    msg = _say(room, opts.user1, "stamp me")

    result = handle_chat_message(opts.user2, _read_frame(room, msg.pk))
    assert_eq(result["type"], "chat_read_ack", f"expected a read ack, got {result}")

    msg.refresh_from_db()
    ms = ChatMembership.objects.get(room=room, user=opts.user2)
    assert_eq(
        ms.last_read_at, msg.created,
        f"expected last_read_at == the target's created, got {ms.last_read_at}")


@th.django_unit_test()
def test_read_last_read_at_never_goes_backwards(opts):
    """A later read of an older message does not rewind last_read_at."""
    from mojo.apps.chat.handler import handle_chat_message
    from mojo.apps.chat.models import ChatMembership

    room = _read_room(opts, "test-msg-read-monotonic")
    first = _say(room, opts.user1, "older")
    second = _say(room, opts.user1, "newer")

    handle_chat_message(opts.user2, _read_frame(room, second.pk))
    ms = ChatMembership.objects.get(room=room, user=opts.user2)
    second.refresh_from_db()
    assert_eq(ms.last_read_at, second.created, "expected the first read to stamp the newer message")

    result = handle_chat_message(opts.user2, _read_frame(room, first.pk))
    assert_eq(
        result["up_to_message_id"], first.pk,
        "expected the ack to echo the resolved older target")

    ms.refresh_from_db()
    assert_eq(
        ms.last_read_at, second.created,
        f"expected last_read_at to hold at the newer message, got {ms.last_read_at}")


@th.django_unit_test()
def test_channel_last_read_at_uses_target_created(opts):
    """Channels stamp the resolved message's created and write no receipts."""
    from mojo.apps.chat.handler import handle_chat_message
    from mojo.apps.chat.models import ChatMembership, ChatReadReceipt

    room = _read_room(opts, "test-msg-read-channel", kind="channel")
    msg = _say(room, opts.user1, "channel message")

    result = handle_chat_message(opts.user2, _read_frame(room, msg.pk))
    assert_eq(result["type"], "chat_read_ack", f"expected a read ack, got {result}")
    assert_eq(result["up_to_message_id"], msg.pk, "expected the resolved id on a channel ack")

    msg.refresh_from_db()
    ms = ChatMembership.objects.get(room=room, user=opts.user2)
    assert_eq(
        ms.last_read_at, msg.created,
        f"expected a channel read to stamp the target's created, got {ms.last_read_at}")
    assert_eq(
        ChatReadReceipt.objects.filter(message__room=room, user=opts.user2).count(), 0,
        "expected no read receipts in a channel room")


@th.django_unit_test()
def test_read_via_rest_returns_resolved_id(opts):
    """The REST read response carries the resolved id additively.

    A view returning a plain dict with no `data` key is wrapped by the response
    decorator, so the endpoint's own body lands under `data` -- `data.status`
    is unchanged and `data.up_to_message_id` is the new key.
    """
    from mojo.apps.chat.models import ChatMessage

    room = _read_room(opts, "test-msg-read-rest-resolved")
    _say(room, opts.user1, "one")
    newest = _say(room, opts.user1, "two")

    bogus = ChatMessage.objects.order_by("-pk").first().pk + 10000
    opts.client.login(TEST_EMAIL_2, TEST_PASSWORD)
    resp = opts.client.post('/api/chat/room/read', {
        'room_id': room.pk,
        'up_to_message_id': bogus,
    })
    assert_eq(resp.status_code, 200, f"expected 200, got {resp.status_code}: {resp.json}")
    assert_eq(
        resp.json.data.status, True,
        "expected the existing data.status flag to be untouched")
    assert_eq(
        resp.json.data.get("up_to_message_id"), newest.pk,
        f"expected the REST response to carry the resolved id {newest.pk}, "
        f"got {resp.json.data.get('up_to_message_id')}")


@th.django_unit_test()
def test_react_explicit_add_is_idempotent(opts):
    """Two `add` frames leave one reaction and both ack `added`."""
    from mojo.apps.chat.handler import handle_chat_message
    from mojo.apps.chat.models import ChatReaction

    room = _read_room(opts, "test-msg-react-add")
    msg = _say(room, opts.user1, "react to me")

    frame = {
        "type": "chat_react", "message_id": msg.pk,
        "emoji": "\U0001f44d", "action": "add",
    }

    first = handle_chat_message(opts.user2, dict(frame))
    second = handle_chat_message(opts.user2, dict(frame))

    assert_eq(first["type"], "chat_react_ack", f"expected a react ack, got {first}")
    assert_eq(first["action"], "added", f"expected action=added, got {first['action']}")
    assert_eq(
        second["action"], "added",
        f"expected the repeated add to still ack added, got {second['action']}")
    assert_eq(
        ChatReaction.objects.filter(message=msg, user=opts.user2, emoji="\U0001f44d").count(), 1,
        "expected exactly one reaction row after two adds")


@th.django_unit_test()
def test_react_explicit_remove_is_idempotent(opts):
    """Two `remove` frames leave none and both ack `removed`."""
    from mojo.apps.chat.handler import handle_chat_message
    from mojo.apps.chat.models import ChatReaction

    room = _read_room(opts, "test-msg-react-remove")
    msg = _say(room, opts.user1, "react to me")

    handle_chat_message(opts.user2, {
        "type": "chat_react", "message_id": msg.pk,
        "emoji": "\U0001f44d", "action": "add",
    })

    frame = {
        "type": "chat_react", "message_id": msg.pk,
        "emoji": "\U0001f44d", "action": "remove",
    }
    first = handle_chat_message(opts.user2, dict(frame))
    second = handle_chat_message(opts.user2, dict(frame))

    assert_eq(first["action"], "removed", f"expected action=removed, got {first['action']}")
    assert_eq(
        second["action"], "removed",
        f"expected the repeated remove to still ack removed, got {second['action']}")
    assert_eq(
        ChatReaction.objects.filter(message=msg, user=opts.user2, emoji="\U0001f44d").count(), 0,
        "expected no reaction rows after two removes")


@th.tier("core")
@th.django_unit_test()
def test_react_noop_does_not_broadcast(opts):
    """A no-op add/remove acks but publishes nothing.

    chat_react is not rate limited, so publishing on every call would let one
    member loop `add` and fan one inbound frame out to every subscriber in the
    room. The idempotent ack is the useful half of the contract; the broadcast
    is not.
    """
    from mojo.apps.chat.handler import _handle_react

    room = _read_room(opts, "test-msg-react-noop")
    msg = _say(room, opts.user1, "react to me")

    published = []

    def capture(topic, payload):
        published.append((topic, payload))

    add = {"type": "chat_react", "message_id": msg.pk, "emoji": "\U0001f44d", "action": "add"}
    remove = dict(add, action="remove")

    first = _handle_react(opts.user2, dict(add), publisher=capture)
    assert_eq(first["action"], "added", f"expected action=added, got {first}")
    assert_eq(len(published), 1, f"expected one broadcast for the real add, got {len(published)}")
    assert_eq(published[0][0], room.topic, f"expected the room topic, got {published[0][0]}")

    repeat = _handle_react(opts.user2, dict(add), publisher=capture)
    assert_eq(repeat["action"], "added", "expected the repeated add to still ack added")
    assert_eq(
        len(published), 1,
        f"expected the no-op add to publish nothing, got {len(published)} broadcasts")

    _handle_react(opts.user2, dict(remove), publisher=capture)
    assert_eq(len(published), 2, f"expected one broadcast for the real remove, got {len(published)}")

    _handle_react(opts.user2, dict(remove), publisher=capture)
    assert_eq(
        len(published), 2,
        f"expected the no-op remove to publish nothing, got {len(published)} broadcasts")


@th.django_unit_test()
def test_react_invalid_action_rejected(opts):
    """An unrecognised action is refused and changes nothing."""
    from mojo.apps.chat.handler import handle_chat_message
    from mojo.apps.chat.models import ChatReaction

    room = _read_room(opts, "test-msg-react-badaction")
    msg = _say(room, opts.user1, "react to me")

    result = handle_chat_message(opts.user2, {
        "type": "chat_react", "message_id": msg.pk,
        "emoji": "\U0001f44d", "action": "toggle",
    })
    assert_eq(result["type"], "error", f"expected an error for an unknown action, got {result}")
    assert_eq(
        result["error"], "Invalid action",
        f"expected 'Invalid action', got {result['error']}")
    assert_eq(
        ChatReaction.objects.filter(message=msg, user=opts.user2).count(), 0,
        "expected an invalid action to create no reaction")


@th.tier("core")
@th.django_unit_test()
def test_react_on_flagged_message_rejected(opts):
    """A flagged message is not a reactable target."""
    from mojo.apps.chat.handler import handle_chat_message
    from mojo.apps.chat.models import ChatMessage, ChatReaction

    room = _read_room(opts, "test-msg-react-flagged")
    msg = _say(room, opts.user1, "flag me then react")
    ChatMessage.objects.filter(pk=msg.pk).update(is_flagged=True)

    result = handle_chat_message(opts.user2, {
        "type": "chat_react", "message_id": msg.pk,
        "emoji": "\U0001f44d", "action": "add",
    })
    assert_eq(result["type"], "error", f"expected an error on a flagged message, got {result}")
    assert_eq(
        result["error"], "Message not found",
        f"expected the generic not-found error, got {result['error']}")
    assert_eq(
        ChatReaction.objects.filter(message=msg, user=opts.user2).count(), 0,
        "expected no reaction on a flagged message")


@th.tier("core")
@th.django_unit_test()
def test_react_foreign_room_message_generic_error(opts):
    """A message in a room the caller is not in gets the SAME error as a
    nonexistent one.

    The chat_react frame carries no room_id, so a distinguishable "Not a member
    of this room" would let any authenticated user probe every tenant's message
    ids for existence -- a global oracle, not a room-scoped one.
    """
    from mojo.apps.chat.handler import handle_chat_message
    from mojo.apps.chat.models import ChatMessage, ChatReaction

    room = _read_room(opts, "test-msg-react-foreign", members=[opts.dm_user])
    foreign = _say(room, opts.user1, "user2 is not in this room")

    frame = {"emoji": "\U0001f44d", "action": "add"}
    in_other_room = handle_chat_message(
        opts.user2, dict(frame, type="chat_react", message_id=foreign.pk))
    nonexistent_id = ChatMessage.objects.order_by("-pk").first().pk + 10000
    nonexistent = handle_chat_message(
        opts.user2, dict(frame, type="chat_react", message_id=nonexistent_id))

    assert_eq(
        in_other_room["error"], "Message not found",
        f"expected the generic not-found error for a foreign room, got {in_other_room}")
    assert_eq(
        in_other_room["error"], nonexistent["error"],
        f"expected identical errors for a foreign and a nonexistent message: "
        f"{in_other_room['error']!r} vs {nonexistent['error']!r}")
    assert_eq(
        ChatReaction.objects.filter(message=foreign, user=opts.user2).count(), 0,
        "expected no reaction on a message in a room the caller is not in")
