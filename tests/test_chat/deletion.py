"""
Tests for the chat message deletion hook: TTL cleanup and REST room deletion.

The handler is injected through the keyword-only `handler=` sentinel seam.
Reaching `CHAT_MESSAGE_DELETED_HANDLER` through a setting or a patch is not
legal in this package -- see the note at the top of `kinds.py`.
"""

TESTIT_TIER = "extended"
from testit import helpers as th
from testit.helpers import assert_true, assert_eq

TEST_EMAIL = 'chat-deletion-user@example.com'
TEST_PASSWORD = 'TestPass1!'

ROOM_PREFIX = "test-deletion-"


@th.django_unit_setup()
@th.requires_app("mojo.apps.chat")
def setup_chat_deletion(opts):
    from mojo.apps.account.models import User
    from mojo.apps.chat.models import ChatRoom

    # Delete before create -- the database is long lived, not fresh.
    User.objects.filter(email=TEST_EMAIL).delete()
    ChatRoom.objects.filter(name__startswith=ROOM_PREFIX).delete()

    opts.user = User.objects.create_user(
        username=TEST_EMAIL, email=TEST_EMAIL, password=TEST_PASSWORD,
    )
    opts.user.is_email_verified = True
    opts.user.save()


def _make_room(name, user, rules=None):
    """A fresh room owned by `user`, replacing any prior run's copy."""
    from mojo.apps.chat.models import ChatRoom, ChatMembership

    full_name = f"{ROOM_PREFIX}{name}"
    ChatRoom.objects.filter(name=full_name).delete()
    room = ChatRoom.objects.create(name=full_name, kind="group", user=user)
    if rules is not None:
        merged = dict(room.rules or {})
        merged.update(rules)
        room.rules = merged
        room.save()
    ChatMembership.objects.get_or_create(
        room=room, user=user, defaults={"role": "owner"})
    return room


def _make_expired_messages(room, user, count):
    """Create `count` messages already older than the room's TTL."""
    from datetime import timedelta
    from mojo.helpers import dates
    from mojo.apps.chat.models import ChatMessage

    ids = []
    for i in range(count):
        msg = ChatMessage.objects.create(
            room=room, user=user, body=f"expiring message {i}")
        ids.append(msg.pk)

    # `created` is auto_now_add, so backdate it after the fact.
    old = dates.utcnow() - timedelta(days=1)
    ChatMessage.objects.filter(pk__in=ids).update(created=old)
    return ids


def _disarm(room):
    """Stop a TTL room being swept by any later global run_cleanup()."""
    room.rules = dict(room.rules or {}, disappearing_ttl=0)
    room.save(update_fields=["rules"])


@th.django_unit_test()
def test_cleanup_notifies_deletion_handler(opts):
    """TTL cleanup tells the handler which ids it removed."""
    from mojo.apps.chat.cleanup import run_cleanup
    from mojo.apps.chat.models import ChatMessage

    room = _make_room("ttl-notify", opts.user, rules={"disappearing_ttl": 60})
    expired = _make_expired_messages(room, opts.user, 3)
    fresh = ChatMessage.objects.create(
        room=room, user=opts.user, body="still within the ttl")

    calls = []

    def capture(room_id, message_ids):
        calls.append((room_id, list(message_ids)))

    try:
        run_cleanup(handler=capture)
    finally:
        _disarm(room)

    mine = [call for call in calls if call[0] == room.pk]
    assert_eq(len(mine), 1, f"expected one handler call for this room, got {len(mine)}")

    notified = set(mine[0][1])
    assert_eq(
        notified, set(expired),
        f"expected the expired ids to be notified, got {sorted(notified)}")
    assert_true(
        fresh.pk not in notified,
        "expected a message inside the ttl not to be notified")

    assert_eq(
        ChatMessage.objects.filter(pk__in=expired).count(), 0,
        "expected the expired messages to be deleted")
    assert_true(
        ChatMessage.objects.filter(pk=fresh.pk).exists(),
        "expected the message inside the ttl to survive")


@th.django_unit_test()
def test_cleanup_without_handler_still_deletes(opts):
    """No handler configured is the normal case and must not change cleanup."""
    from mojo.apps.chat.cleanup import run_cleanup
    from mojo.apps.chat.models import ChatMessage

    room = _make_room("ttl-no-handler", opts.user, rules={"disappearing_ttl": 60})
    expired = _make_expired_messages(room, opts.user, 2)

    try:
        deleted = run_cleanup(handler=None)
    finally:
        _disarm(room)

    assert_true(
        deleted >= 2, f"expected run_cleanup to report deletions, got {deleted}")
    assert_eq(
        ChatMessage.objects.filter(pk__in=expired).count(), 0,
        "expected the expired messages to be deleted with no handler configured")


@th.django_unit_test()
def test_room_delete_notifies_deletion_handler(opts):
    """A room delete destroys every message at once, so the hook covers it."""
    from mojo.apps.chat.models import ChatMessage

    room = _make_room("room-notify", opts.user)
    ids = [
        ChatMessage.objects.create(room=room, user=opts.user, body=f"m{i}").pk
        for i in range(3)
    ]

    calls = []

    def capture(room_id, message_ids):
        calls.append((room_id, list(message_ids)))

    room.on_rest_pre_delete(handler=capture)

    assert_eq(len(calls), 1, f"expected exactly one handler call, got {len(calls)}")
    assert_eq(calls[0][0], room.pk, f"expected the room id, got {calls[0][0]}")
    assert_eq(
        set(calls[0][1]), set(ids),
        f"expected every message id in the room, got {sorted(calls[0][1])}")


@th.django_unit_test()
def test_room_delete_cascades_messages(opts):
    """The hook's reason for existing: the cascade really does destroy them."""
    from mojo.apps.chat.models import ChatMessage

    room = _make_room("room-cascade", opts.user)
    ids = [
        ChatMessage.objects.create(room=room, user=opts.user, body=f"m{i}").pk
        for i in range(3)
    ]
    assert_eq(
        ChatMessage.objects.filter(pk__in=ids).count(), 3,
        "expected the messages to exist before the delete")

    room.delete()

    assert_eq(
        ChatMessage.objects.filter(pk__in=ids).count(), 0,
        "expected the room delete to cascade to its messages")


@th.tier("core")
@th.django_unit_test()
def test_deletion_handler_exception_does_not_block_delete(opts):
    """A broken consumer hook must never prevent a deletion."""
    from mojo.apps.chat.cleanup import run_cleanup
    from mojo.apps.chat.models import ChatRoom, ChatMessage

    def boom(room_id, message_ids):
        raise RuntimeError("consumer hook is down")

    # Room deletion path.
    room = _make_room("handler-raises", opts.user)
    ids = [
        ChatMessage.objects.create(room=room, user=opts.user, body=f"m{i}").pk
        for i in range(2)
    ]
    room_pk = room.pk

    room.on_rest_pre_delete(handler=boom)
    room.delete()

    assert_true(
        not ChatRoom.objects.filter(pk=room_pk).exists(),
        "expected the room to be deleted despite the failing handler")
    assert_eq(
        ChatMessage.objects.filter(pk__in=ids).count(), 0,
        "expected the messages to be deleted despite the failing handler")

    # TTL cleanup path.
    ttl_room = _make_room("handler-raises-ttl", opts.user,
                          rules={"disappearing_ttl": 60})
    expired = _make_expired_messages(ttl_room, opts.user, 2)

    try:
        run_cleanup(handler=boom)
    finally:
        _disarm(ttl_room)

    assert_eq(
        ChatMessage.objects.filter(pk__in=expired).count(), 0,
        "expected cleanup to delete despite the failing handler")
