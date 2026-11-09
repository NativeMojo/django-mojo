"""Transactional arrival sequencing through the supported ORM/service doors."""
from testit import helpers as th


def _fixture(name):
    from mojo.apps.account.models import User
    from mojo.apps.chat.models import ChatRoom, ChatMembership
    email = f"seq-{name}@example.com"
    User.objects.filter(email=email).delete()
    ChatRoom.objects.filter(name=f"seq-{name}").delete()
    user = User.objects.create_user(username=email, email=email, password="TestPass1!")
    room = ChatRoom.objects.create(name=f"seq-{name}", user=user)
    member = ChatMembership.objects.create(room=room, user=user, read_backend="cursor")
    return room, user, member


def _create(room, user=None, **kwargs):
    from mojo.apps.chat.models import ChatMessage
    return ChatMessage.objects.create(room=room, user=user, body="hello",
        read_backend="cursor", read_handler=None, read_publisher=False, **kwargs)


@th.django_unit_test()
def test_direct_create_save_file_and_system_sequence(opts):
    from mojo.apps.chat.models import ChatMessage, ChatReadReceipt
    room, user, member = _fixture("doors")
    first = _create(room, user, kind="file", metadata={"file_id": 42})
    second = ChatMessage(room=room, user=None, body="joined", kind="system")
    second.save(read_backend="cursor", read_handler=None, read_publisher=False)
    room.refresh_from_db(); member.refresh_from_db()
    assert first.room_seq == 1 and second.room_seq == 2, "Direct file and instance system creates must share sequencing"
    assert room.message_seq == 2, "Room head must cover every supported create door"
    assert member.last_read_seq == 1, "Sender must acknowledge through their own sequence, not future system arrivals"
    assert not ChatReadReceipt.objects.filter(message__room=room).exists(), "Cursor traffic must never write receipts"


@th.django_unit_test()
def test_outer_rollback_discards_counter_cursor_and_publication(opts):
    from django.db import transaction
    from mojo.apps.chat.models import ChatMessage
    from mojo.apps.chat.services.messages import send_message
    room, user, member = _fixture("rollback")
    events = []
    try:
        with transaction.atomic():
            msg, error = send_message(room, user, "hello", enforce_room_policy=False,
                validators={}, read_backend="cursor", read_handler=lambda *args: events.append(args),
                publisher=lambda *args: events.append(args), read_publisher=False)
            assert msg.room_seq == 1 and error is None, "Uncommitted send must have its authoritative sequence"
            assert not events, "Neither hook nor message may publish before the outer transaction commits"
            raise ValueError("rollback fixture")
    except ValueError:
        pass
    room.refresh_from_db(); member.refresh_from_db()
    assert room.message_seq == 0 and member.last_read_seq == 0, "Outer rollback must undo head and sender cursor together"
    assert not ChatMessage.objects.filter(room=room).exists() and not events, "Rolled back send must leave no row or frame"


@th.django_unit_test()
def test_failed_dedupe_does_not_consume_sequence(opts):
    from django.db import IntegrityError
    room, user, member = _fixture("dedupe")
    original = _create(room, user, client_key="same")
    try:
        _create(room, user, client_key="same")
        assert False, "Duplicate client key must hit the durable unique constraint"
    except IntegrityError:
        pass
    following = _create(room, None)
    room.refresh_from_db(); member.refresh_from_db()
    assert original.room_seq == 1 and following.room_seq == 2 and room.message_seq == 2, "Failed dedupe must not leave a sequence gap"
    assert member.last_read_seq == 1, "Rejected duplicate must not advance sender state"


@th.django_unit_test()
def test_concurrent_room_sends_have_contiguous_sequences(opts):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from django.db import close_old_connections
    room, user, member = _fixture("concurrent")
    barrier = Barrier(8)
    def send(index):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return _create(room, None).room_seq
        finally:
            close_old_connections()
    with ThreadPoolExecutor(max_workers=8) as pool:
        seqs = list(pool.map(send, range(8)))
    room.refresh_from_db()
    assert sorted(seqs) == list(range(1, 9)), "Concurrent same-room sends must serialize into unique contiguous arrivals"
    assert room.message_seq == 8, "Room counter must equal committed concurrent inserts"


@th.django_unit_test()
def test_membership_bulk_initializes_current_head_and_rejoin(opts):
    from mojo.apps.chat.models import ChatMembership
    room, user, member = _fixture("join")
    _create(room); _create(room)
    member.delete()
    seats = ChatMembership.objects.bulk_create([ChatMembership(room=room, user=user)], read_backend="cursor")
    assert seats[0].last_read_seq == 2, "Supported bulk membership creation must initialize from the locked current head"
    seats[0].delete()
    fresh = ChatMembership.objects.create(room=room, user=user, read_backend="cursor")
    assert fresh.last_read_seq == 2 and fresh.unread_exception_count == 0, "Rejoin must start at the current head with no stale exceptions"


@th.django_unit_test()
def test_sender_hook_committed_once_with_exact_target(opts):
    from django.db import transaction
    from mojo.apps.chat.models import ChatMessage
    room, user, member = _fixture("sender-hook")
    calls = []
    with transaction.atomic():
        msg = ChatMessage(room=room, user=user, body="hello")
        msg.save(read_backend="cursor", read_handler=lambda *a: calls.append(a), read_publisher=False)
        assert not calls, "Own-send hook must wait for commit"
    assert len(calls) == 1, "Own-send must invoke one successful-read hook"
    position = calls[0][2]
    assert position.reason == "own_send" and position.ack_target_id == msg.pk, "Own-send hook must carry its actual message target"
    assert position.read_seq == msg.room_seq and calls[0][3], "Own-send must durably acknowledge through its sequence"
