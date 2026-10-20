"""Rare reordering preserves CURRENT unread identities across merge and restore."""
from testit import helpers as th
from .sequences import _fixture, _create
from .read_state import _read


def _two_rooms(name):
    from mojo.apps.chat.models import ChatRoom, ChatMembership
    room, user, first_seat = _fixture(name)
    ChatRoom.objects.filter(name=f"seq-{name}-second").delete()
    second = ChatRoom.objects.create(name=f"seq-{name}-second", user=user)
    second_seat = ChatMembership.objects.create(room=second, user=user, read_backend="cursor")
    return room, second, user, first_seat, second_seat


@th.django_unit_test()
def test_merge_read_restore_does_not_resurrect_unread(opts):
    from mojo.apps.chat.services.maintenance import relocate_messages, restore_snapshot
    from mojo.apps.chat.services.read_state import unread_message_ids
    first, second, user, first_seat, second_seat = _two_rooms("merge-restore")
    a, b, c, d = _create(first), _create(second), _create(first), _create(second)
    _read(first, user, first_seat, a)
    _read(second, user, second_seat, b)
    snapshot = relocate_messages([first.pk, second.pk], {b.pk: first.pk, d.pk: first.pk}, publisher=False)
    first_seat.refresh_from_db()
    assert unread_message_ids(first_seat, [a.pk, b.pk, c.pk, d.pk]) == {c.pk, d.pk}, "Merge must transport arbitrary source unread identities exactly"
    _read(first, user, first_seat, c)
    restore_snapshot(snapshot, publisher=False)
    first_seat.refresh_from_db(); second_seat.refresh_from_db()
    assert unread_message_ids(first_seat, [a.pk, c.pk]) == set(), "Revert must not resurrect the source message read after merge"
    assert unread_message_ids(second_seat, [b.pk, d.pk]) == {d.pk}, "Revert must route the still-unread source message back to its origin"
    assert first_seat.unread_exception_count == 0 and second_seat.unread_exception_count == 1, "Revert caches must match live routed exceptions"


@th.django_unit_test()
def test_hidden_unread_and_stragglers_survive_rebuild(opts):
    from mojo.apps.chat.models import ChatMessage
    from mojo.apps.chat.services.maintenance import relocate_messages
    from mojo.apps.chat.services.read_state import unread_message_ids
    first, second, user, seat, other = _two_rooms("hidden-move")
    hidden, visible = _create(first), _create(first)
    ChatMessage.objects.filter(pk=hidden.pk).update(is_flagged=True)
    relocate_messages([first.pk, second.pk], {hidden.pk: second.pk, visible.pk: second.pk}, publisher=False)
    other.refresh_from_db()
    assert unread_message_ids(other, [hidden.pk, visible.pk]) == {hidden.pk, visible.pk}, "Maintenance must preserve hidden extant unread identities without display filters"
    straggler = _create(first)
    relocate_messages([first.pk, second.pk], {straggler.pk: second.pk}, publisher=False)
    other.refresh_from_db()
    assert unread_message_ids(other, [hidden.pk, visible.pk, straggler.pk]) == {hidden.pk, visible.pk, straggler.pk}, "Straggler repair must union current sparse unread identities with new arrivals"


@th.django_unit_test()
def test_snapshot_version_validation_and_maintenance_rollback(opts):
    from django.db import transaction
    from mojo.apps.chat.services.maintenance import relocate_messages, import_snapshot
    first, second, user, seat, other = _two_rooms("maintenance-rollback")
    msg = _create(first)
    try:
        import_snapshot({"version": 99})
        assert False, "Unknown snapshot versions must fail before any mutation"
    except ValueError:
        pass
    try:
        with transaction.atomic():
            snap = relocate_messages([first.pk, second.pk], {msg.pk: second.pk}, publisher=False)
            assert snap["version"] == 1, "Maintenance snapshot must have an explicit routing schema version"
            raise ValueError("rollback fixture")
    except ValueError:
        pass
    msg.refresh_from_db(); first.refresh_from_db()
    assert msg.room_id == first.pk and msg.room_seq == 1 and first.message_seq == 1, "Outer rollback must atomically restore message routing, numbering and room head"
    old = import_snapshot({"version": 0, "room_ids": [first.pk, second.pk],
        "message_rooms": {str(msg.pk): first.pk}, "memberships": []})
    assert old["version"] == 1 and old["message_rooms"][str(msg.pk)] == first.pk, "Explicit legacy adapter must preserve routing without inventing cursor state"


@th.django_unit_test()
def test_waiting_read_refetches_current_target_mapping(opts):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from django.db import transaction, close_old_connections
    from mojo.apps.chat.models import ChatMembership
    from mojo.apps.chat.services.maintenance import relocate_messages
    first, second, user, seat, other = _two_rooms("waiting-read")
    msg = _create(first)
    started = Event()
    def read_waiter():
        close_old_connections()
        try:
            started.set()
            return _read(first, user, seat, msg)
        finally:
            close_old_connections()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            # Room-first ordering matches maintenance and deliberately stalls the read.
            from mojo.apps.chat.models import ChatRoom
            list(ChatRoom.objects.select_for_update().filter(pk__in=[first.pk, second.pk]).order_by("pk"))
            list(ChatMembership.objects.select_for_update().filter(pk=seat.pk))
            future = pool.submit(read_waiter)
            assert started.wait(5), "Read waiter must start while the old membership state is locked"
            relocate_messages([first.pk, second.pk], {msg.pk: second.pk}, publisher=False)
        result = future.result(timeout=10)
    assert result.target is None and not result.changed, "A read waiting behind maintenance must revalidate the target's CURRENT room instead of acknowledging stale mapping"


@th.django_unit_test()
def test_generation_invalidates_delayed_read_callbacks(opts):
    from django.db import transaction
    from mojo.apps.chat.services.maintenance import relocate_messages
    first, second, user, seat, other = _two_rooms("generation-callback")
    msg = _create(first)
    events = []
    with transaction.atomic():
        _read(first, user, seat, msg, handler=lambda *a: events.append(a))
        relocate_messages([first.pk, second.pk], {}, publisher=False)
    assert not events, "Generation changes must invalidate callbacks captured against older numbering"


@th.django_unit_test()
def test_initial_activation_is_idempotent_after_new_cursor_send(opts):
    from mojo.apps.chat.services.maintenance import activate_initial
    from mojo.apps.chat.services.read_state import unread_counts
    first, second, user, seat, other = _two_rooms("activation")
    assert activate_initial([first.pk]) == 1, "Unactivated legacy room must be marked once"
    fresh = _create(second)
    assert activate_initial([first.pk, second.pk]) == 0, "A new numbered room must already be activated so reruns never clear its unread arrivals"
    assert unread_counts(user, backend="cursor")[second.pk].unread_count == 1, "Activation rerun must preserve legitimate postactivation unread state"
