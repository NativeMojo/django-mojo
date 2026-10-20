"""Rare, transactional history operations; never used by ordinary chat traffic.

All rooms lock in PK order, then all seats. Snapshot routing is durable JSON;
restoration always transports CURRENT unread identities, never old read state.
The host retains ownership of membership/status/visibility policy.
"""
from contextlib import contextmanager

from django.db import models, transaction
from mojo.helpers import logit

from .read_state import unread_queryset

SNAPSHOT_VERSION = 1
logger = logit.get_logger("chat", "chat.log")


@contextmanager
def _locked(room_ids):
    from ..models import ChatRoom, ChatMembership
    ids = sorted(set(int(pk) for pk in room_ids))
    with transaction.atomic():
        rooms = list(ChatRoom.objects.select_for_update().filter(pk__in=ids).order_by("pk"))
        if len(rooms) != len(ids):
            raise ValueError("Every maintenance room must exist")
        members = list(ChatMembership.objects.select_for_update().filter(
            room_id__in=ids).order_by("room_id", "pk"))
        yield rooms, members


def _snapshot(rooms, members):
    from ..models import ChatMessage
    return {
        "version": SNAPSHOT_VERSION,
        "rooms": [{"id": r.pk, "generation": r.read_state_generation} for r in rooms],
        "message_rooms": {str(pk): room_id for pk, room_id in ChatMessage.objects.filter(
            room_id__in=[r.pk for r in rooms]).order_by("pk").values_list("pk", "room_id")},
        "memberships": [{"id": m.pk, "room_id": m.room_id, "user_id": m.user_id,
                         "joined_at": m.joined_at.isoformat(), "role": m.role,
                         "status": m.status} for m in members],
    }


def snapshot_rooms(room_ids):
    """Versioned provenance/routing only; deliberately contains no stale unread set."""
    with _locked(room_ids) as (rooms, members):
        return _snapshot(rooms, members)


def import_snapshot(snapshot):
    """Validate a v1 snapshot, or explicitly adapt a legacy routing-only v0.

    Legacy input must supply room_ids, message_rooms and memberships. Missing
    read cursor fields mean nothing; no state is reset during this adapter.
    """
    if not isinstance(snapshot, dict):
        raise ValueError("Chat maintenance snapshot must be an object")
    version = snapshot.get("version")
    if version == 0 and all(k in snapshot for k in ("room_ids", "message_rooms", "memberships")):
        snapshot = dict(snapshot, version=1, rooms=[
            {"id": int(pk), "generation": None} for pk in snapshot["room_ids"]])
    if snapshot.get("version") != SNAPSHOT_VERSION:
        raise ValueError("Unsupported chat maintenance snapshot version")
    try:
        rooms = sorted({int(r["id"]) for r in snapshot["rooms"]})
        routing = {str(int(pk)): int(room) for pk, room in snapshot["message_rooms"].items()}
        members = snapshot["memberships"]
        if not isinstance(members, list) or any(
                int(m["room_id"]) not in rooms or int(m["user_id"]) < 1 for m in members):
            raise ValueError("Invalid membership context")
        if not rooms or any(pk < 1 for pk in rooms) or any(
                int(pk) < 1 or room not in rooms for pk, room in routing.items()):
            raise ValueError("Invalid message routing")
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("Invalid chat maintenance snapshot") from error
    return dict(snapshot, message_rooms=routing)


def _live_unread(rooms, members):
    room_map = {r.pk: r for r in rooms}
    unread = {}
    for member in members:
        qs = unread_queryset(member)
        if room_map[member.room_id].kind != "channel":
            qs = qs.filter(created__gte=member.joined_at)
        unread.setdefault(member.user_id, set()).update(qs.values_list("pk", flat=True))
    return unread


def _rebuild(rooms, members, unread):
    from ..models import ChatMessage, ChatMembership, ChatUnreadException, ChatRoom
    # Null out all identities first, avoiding unique collisions while renumbering.
    qs = ChatMessage.objects.filter(room_id__in=[r.pk for r in rooms])
    models.QuerySet.update(qs, room_seq=None)
    mapping = {}
    created = {}
    for room in rooms:
        messages = list(ChatMessage.objects.filter(room_id=room.pk).order_by("pk"))
        for seq, message in enumerate(messages, 1):
            message.room_seq = seq
            mapping[message.pk] = (room.pk, seq)
            created[message.pk] = message.created
        # Bypass the public identity guard ONLY inside this locked maintenance path.
        if messages:
            models.QuerySet(model=ChatMessage).bulk_update(messages, ["room_seq"], batch_size=1000)
        room.message_seq = len(messages)
        room.read_state_generation += 1
        ChatRoom.objects.filter(pk=room.pk).update(
            message_seq=room.message_seq, read_state_generation=room.read_state_generation)
    room_map = {r.pk: r for r in rooms}
    actual = {}
    for member in members:
        where = mapping.get(member.last_read_message_id)
        if where:
            key = (member.user_id, where[0])
            previous = actual.get(key)
            if previous is None or member.last_read_message_id > previous[0]:
                actual[key] = (member.last_read_message_id, member.last_read_at)
    ChatUnreadException.objects.filter(membership_id__in=[m.pk for m in members]).delete()
    exceptions = []
    for member in members:
        ids = [pk for pk in unread.get(member.user_id, ())
               if pk in mapping and mapping[pk][0] == member.room_id
               and (room_map[member.room_id].kind == "channel"
                    or created[pk] >= member.joined_at)]
        member.last_read_seq = room_map[member.room_id].message_seq
        member.unread_exception_count = len(ids)
        # Stored actual facts survive only while the actual message still belongs here.
        member.last_read_message_id, member.last_read_at = actual.get(
            (member.user_id, member.room_id), (None, None))
        ChatMembership.objects.filter(pk=member.pk).update(
            last_read_seq=member.last_read_seq,
            unread_exception_count=member.unread_exception_count,
            last_read_message_id=member.last_read_message_id, last_read_at=member.last_read_at)
        exceptions.extend(ChatUnreadException(
            membership_id=member.pk, room_seq=mapping[pk][1], message_id=pk) for pk in ids)
    ChatUnreadException.objects.bulk_create(exceptions, batch_size=1000)


def _invalidate(rooms, publisher):
    facts = [(r.pk, r.read_state_generation) for r in rooms]
    def committed():
        if publisher is False:
            return
        from mojo.apps.realtime import publish_topic
        for room_id, generation in facts:
            try:
                (publisher or publish_topic)(f"chat:{room_id}", {
                    "type": "chat_read_state_invalidated", "room_id": room_id,
                    "generation": generation})
            except Exception:
                logger.exception("chat: maintenance invalidation publication failed")
    transaction.on_commit(committed)


def relocate_messages(room_ids, message_rooms, *, arrival_message_ids=None, publisher=None):
    """Move PK->destination-room mappings and preserve current unread identities.

    Caller must create/retain destination seats before calling, and remove old
    seats only after this returns. All supplied rooms are rebuilt in PK order.
    Hosts may designate live mapped arrival_message_ids when a straggler was
    written after source seats disappeared: each destination seat except the
    author gains that exact unread identity, subject to destination entitlement.
    Other absent-source history is never inferred unread.
    Deleted mappings are harmless; a live mapped message outside locked rooms
    or a destination outside those rooms fails before mutation. Returns the
    PRE-operation versioned snapshot for routing a later restore.
    """
    from ..models import ChatMessage
    routing = {int(pk): int(room) for pk, room in message_rooms.items()}
    arrivals = {int(pk) for pk in (arrival_message_ids or ())}
    if not arrivals.issubset(routing):
        raise ValueError("Maintenance arrivals must be explicitly mapped message IDs")
    with _locked(room_ids) as (rooms, members):
        allowed = {r.pk for r in rooms}
        if any(room not in allowed for room in routing.values()):
            raise ValueError("Maintenance destination is outside locked rooms")
        if ChatMessage.objects.filter(pk__in=routing).exclude(room_id__in=allowed).exists():
            raise ValueError("Maintenance source is outside locked rooms")
        arrival_authors = dict(ChatMessage.objects.filter(pk__in=arrivals).values_list("pk", "user_id"))
        if len(arrival_authors) != len(arrivals):
            raise ValueError("Maintenance arrival messages must exist")
        snapshot = _snapshot(rooms, members)
        unread = _live_unread(rooms, members)
        for member in members:
            unread.setdefault(member.user_id, set()).update(
                pk for pk, author_id in arrival_authors.items()
                if routing[pk] == member.room_id and member.user_id != author_id)
        # All rows first lose their old sequence; room moves cannot collide.
        models.QuerySet.update(ChatMessage.objects.filter(room_id__in=allowed), room_seq=None)
        destinations = {}
        for pk, room_id in routing.items():
            destinations.setdefault(room_id, []).append(pk)
        for room_id, ids in destinations.items():
            models.QuerySet.update(ChatMessage.objects.filter(pk__in=ids), room_id=room_id)
        _rebuild(rooms, members, unread)
        _invalidate(rooms, publisher)
        return snapshot


def restore_snapshot(snapshot, *, publisher=None):
    """Restore routing while preserving reads performed AFTER the snapshot.

    Recreate desired memberships under the host's current role/status policy
    before calling. This function never restores stale roles, bans or cursors.
    Messages not named in the snapshot remain where they currently belong;
    the host may supply their explicit routing in relocate_messages separately.
    """
    snapshot = import_snapshot(snapshot)
    return relocate_messages([r["id"] for r in snapshot["rooms"]],
                             snapshot["message_rooms"], publisher=publisher)


def activate_initial(room_ids=None):
    """Idempotent Option A: rooms/seats only, no message scan or history backfill.

    Drain old writers first. The durable generation=1 marker prevents a repeated
    activation from clearing unread positions created after the first run.
    """
    from ..models import ChatRoom, ChatMembership, ChatUnreadException
    ids = list(ChatRoom.objects.filter(read_state_generation=0).values_list("pk", flat=True))
    if room_ids is not None:
        ids = sorted(set(ids).intersection(room_ids))
    if not ids:
        return 0
    with _locked(ids) as (rooms, members):
        fresh = [r for r in rooms if r.read_state_generation == 0]
        fresh_ids = [r.pk for r in fresh]
        for room in fresh:
            ChatMembership.objects.filter(room_id=room.pk).update(
                last_read_seq=room.message_seq, last_read_message_id=None,
                last_read_at=None, unread_exception_count=0)
        ChatUnreadException.objects.filter(membership__room_id__in=fresh_ids).delete()
        ChatRoom.objects.filter(pk__in=fresh_ids).update(read_state_generation=1)
        return len(fresh)
