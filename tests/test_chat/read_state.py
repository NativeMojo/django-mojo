"""Cursor eligibility, stale acknowledgments, sparse state and bounded snapshots."""
from testit import helpers as th
from .sequences import _fixture, _create


def _read(room, user, member, target, **kwargs):
    from mojo.apps.chat.services.read_state import mark_through
    return mark_through(room, user, member, getattr(target, "pk", target),
        backend="cursor", handler=kwargs.pop("handler", None),
        publisher=kwargs.pop("publisher", False), **kwargs)


@th.django_unit_test()
def test_exact_targets_stale_reads_and_no_receipts(opts):
    from mojo.apps.chat.models import ChatReadReceipt
    room, user, member = _fixture("exact")
    first, second = _create(room), _create(room)
    newest = _read(room, user, member, second)
    stale = _read(room, user, member, first)
    missing = _read(room, user, member, second.pk + 999999)
    member.refresh_from_db()
    assert newest.changed and not stale.changed, "Older valid acknowledgments must never rewind the durable prefix"
    assert member.last_read_message_id == second.pk and member.last_read_at == second.created, "Actual acknowledged PK/time must remain paired and monotonic"
    assert missing.target is None and member.last_read_seq == 2, "Cursor mode must reject invented IDs instead of clamping"
    assert not ChatReadReceipt.objects.filter(message__room=room).exists(), "Cursor reads must not materialize normal receipts"


@th.django_unit_test()
def test_invalid_foreign_banned_and_join_targets_are_noops(opts):
    from mojo.apps.chat.models import ChatMembership, ChatMessage
    from mojo.helpers import dates
    from datetime import timedelta
    room, user, member = _fixture("entitlement")
    foreign_room, _, _ = _fixture("foreign")
    foreign = _create(foreign_room)
    old = _create(room)
    ChatMessage.objects.filter(pk=old.pk).update(created=member.joined_at - timedelta(seconds=1))
    assert _read(room, user, member, foreign).target is None, "Foreign room PK must never resolve"
    assert _read(room, user, member, old).target is None, "A prejoin target is not entitled even when it has a sequence"
    for value in [None, "bad", -1, 2 ** 80]:
        assert _read(room, user, member, value).target is None, f"Invalid target {value!r} must safely resolve to no target"
    fresh = _create(room)
    ChatMembership.objects.filter(pk=member.pk).update(status="banned")
    assert _read(room, user, member, fresh).target is None, "Membership status must be revalidated under the current row lock"


@th.django_unit_test()
def test_snapshot_query_cost_and_deleted_arrival_slots(opts):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext
    from mojo.apps.chat.services.read_state import recipient_positions, unread_counts
    room, user, member = _fixture("query-cost")
    messages = [_create(room) for _ in range(4)]
    messages[-1].delete()
    with CaptureQueriesContext(connection) as captured:
        snapshot = recipient_positions(room, backend="cursor")[user.pk]
    assert len(captured) == 1, "Recipient snapshot must use one membership-room SQL query"
    assert snapshot.unread_count == 4, "Physical deletion must retain its arrival slot in ordinary counts"
    assert "chat_chatmessage" not in captured[0]["sql"] and "readreceipt" not in captured[0]["sql"], "Normal snapshots must never scan history or receipts"
    with CaptureQueriesContext(connection) as captured:
        counts = unread_counts(user, backend="cursor")
    assert len(captured) == 1 and counts[room.pk].unread_count == 4, "User room counts must be one bounded snapshot query"


def _sparse(room, member, messages):
    from mojo.apps.chat.models import ChatMembership, ChatUnreadException
    room.refresh_from_db()
    ChatMembership.objects.filter(pk=member.pk).update(
        last_read_seq=room.message_seq, unread_exception_count=len(messages))
    ChatUnreadException.objects.bulk_create([ChatUnreadException(
        membership=member, room_seq=m.room_seq, message_id=m.pk) for m in messages])
    member.refresh_from_db()


@th.django_unit_test()
def test_sparse_below_prefix_cleanup_and_ack_metadata(opts):
    from mojo.apps.chat.services.read_state import unread_message_ids
    room, user, member = _fixture("sparse-clean")
    first, second, third = _create(room), _create(room), _create(room)
    _read(room, user, member, third)
    _sparse(room, member, [first, second])
    result = _read(room, user, member, first)
    member.refresh_from_db()
    assert result.changed and member.last_read_seq == 3 and member.unread_exception_count == 1, "Read below encoded prefix must atomically remove covered exceptions"
    assert member.last_read_message_id == third.pk, "Exception cleanup by older target must preserve the greatest actual acknowledgment"
    assert unread_message_ids(member, [first.pk, second.pk, third.pk]) == {second.pk}, "Sparse exact eligibility must preserve only the uncovered unread identity"


@th.django_unit_test()
def test_sparse_snapshot_details_are_batched_and_unknown_is_explicit(opts):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext
    from mojo.apps.chat.services.read_state import recipient_positions, message_is_read, ReadStateUnavailable
    room, user, member = _fixture("sparse-details")
    first, second = _create(room), _create(room)
    _sparse(room, member, [first])
    pos = recipient_positions(room, backend="cursor")[user.pk]
    try:
        message_is_read(second, pos)
        assert False, "Missing sparse exception detail must not silently report a read status"
    except ReadStateUnavailable:
        pass
    with CaptureQueriesContext(connection) as captured:
        pos = recipient_positions(room, message_ids=[first.pk, second.pk], backend="cursor")[user.pk]
        assert not message_is_read(first, pos) and message_is_read(second, pos), "Bounded snapshot detail must distinguish exact sparse unread IDs"
    assert len(captured) == 2, "Sparse multi-message eligibility must use only one extra batched SQL query"
    assert pos.read_frontier_message_id is None, "A sparse prefix must not claim a fully read scalar frontier"


@th.django_unit_test()
def test_filter_before_content_cap_and_legacy_null_read(opts):
    from mojo.apps.chat.models import ChatMessage
    from mojo.apps.chat.services.read_state import unread_queryset
    room, user, member = _fixture("before-cap")
    legacy = ChatMessage.objects.create(room=room, user=None, body="legacy")
    first, second = _create(room), _create(room)
    _read(room, user, member, first)
    member.refresh_from_db()
    selected = list(unread_queryset(member).order_by("pk")[:1])
    assert [m.pk for m in selected] == [second.pk], "Unread eligibility must apply before a content/page cap"
    assert legacy.room_seq is None, "Initial Option A must leave legacy history unnumbered and read"


@th.django_unit_test()
def test_invisible_tail_clear_and_deleted_exception_slots(opts):
    from mojo.apps.chat.services.read_state import mark_current_head, unread_counts
    room, user, member = _fixture("invisible")
    first, second = _create(room), _create(room)
    _sparse(room, member, [first])
    first.delete(); second.delete()
    assert unread_counts(user, backend="cursor")[room.pk].unread_count == 1, "Deleting a sparse message must not cascade away its count slot"
    result = mark_current_head(room, user, member, backend="cursor", handler=None, publisher=False)
    assert result.position.unread_count == 0 and result.position.ack_target_id is None, "Trusted head clear must cover invisible/deleted tails without inventing an actual target"


@th.django_unit_test()
def test_read_frames_do_not_rewind_on_valid_duplicate(opts):
    room, user, member = _fixture("frame-frontier")
    first, second = _create(room), _create(room)
    _read(room, user, member, second)
    frames, hooks = [], []
    result = _read(room, user, member, first,
        publisher=lambda topic, frame: frames.append(frame), handler=lambda *a: hooks.append(a))
    assert not result.changed and len(frames) == 1 and len(hooks) == 1, "Valid duplicate read may reconcile consumer state once"
    assert frames[0]["up_to_message_id"] == second.pk, "Committed frame must publish fresh durable frontier, never stale requested target"
    assert frames[0]["ack_target_id"] == first.pk, "Actual ack facts must stay distinct from the effective standing position"


@th.django_unit_test()
def test_sender_clears_sparse_exceptions(opts):
    room, user, member = _fixture("sender-sparse")
    first = _create(room)
    _sparse(room, member, [first])
    own = _create(room, user)
    member.refresh_from_db()
    assert member.last_read_seq == own.room_seq and member.unread_exception_count == 0, "Own send deliberately acknowledges older sparse unread positions"


@th.django_unit_test()
def test_corrupt_cursor_is_unavailable_not_zero(opts):
    from mojo.apps.chat.models import ChatMembership
    from mojo.apps.chat.services.read_state import unread_counts, ReadStateUnavailable
    room, user, member = _fixture("corrupt")
    ChatMembership.objects.filter(pk=member.pk).update(last_read_seq=20)
    try:
        unread_counts(user, backend="cursor")
        assert False, "Impossible cursor must raise unavailable state instead of clamping to zero"
    except ReadStateUnavailable:
        pass
