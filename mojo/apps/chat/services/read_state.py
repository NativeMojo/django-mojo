"""Durable arrival cursors, sparse maintenance exceptions and legacy receipts.

Normal snapshots read membership/room rows, never message history. Read locks
only the current seat and resolves the target AFTER that lock, so maintenance
cannot leave a stale room/sequence mapping behind a waiting acknowledgment.
"""
from .messages import join_bounded_messages
from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from mojo.helpers import logit, modules
from mojo.helpers.settings import settings
from objict import objict
from mojo.db import use_primary
from .backend import read_backend

_UNSET = object()
logger = logit.get_logger("chat", "chat.log")


class ReadStateUnavailable(RuntimeError):
    pass


def _position(member, room):
    if (member.last_read_seq > room.message_seq or member.unread_exception_count < 0
            or member.unread_exception_count > member.last_read_seq):
        raise ReadStateUnavailable("Chat cursor/cache invariant failed")
    return objict(
        room_id=room.pk, user_id=member.user_id,
        generation=room.read_state_generation, head_seq=room.message_seq,
        read_seq=member.last_read_seq,
        last_read_message_id=member.last_read_message_id,
        read_at=member.last_read_at.isoformat() if member.last_read_at else None,
        unread_exception_count=member.unread_exception_count,
        unread_count=room.message_seq - member.last_read_seq + member.unread_exception_count,
        read_frontier_message_id=(member.last_read_message_id
                                  if not member.unread_exception_count else None),
        room_name=room.name, room_kind=room.kind,
    )


@use_primary()
def positions_for_rooms(room_ids, user_ids=None, message_ids=None, *, backend=None):
    """(room_id, user_id)-keyed snapshots across rooms in one batched query.

    Supply message_ids when testing individual messages below a sparse prefix.
    No per-message hidden query is ever made by message_is_read.
    """
    if read_backend(backend) != "cursor":
        raise ValueError("positions_for_rooms requires cursor read state")
    from ..models import ChatMembership, ChatUnreadException
    qs = ChatMembership.objects.filter(
        room_id__in=room_ids, status__in=["active", "muted"]
    )
    if user_ids is not None:
        qs = qs.filter(user_id__in=user_ids)
    members = list(qs.select_related("room").order_by("pk"))
    positions = {(m.room_id, m.user_id): _position(m, m.room) for m in members}
    if message_ids is not None:
        checked = list(set(message_ids))
        sparse = {m.pk: (m.room_id, m.user_id) for m in members if m.unread_exception_count}
        for pos in positions.values():
            pos.checked_message_ids = checked
            pos.unread_exception_message_ids = []
        if sparse and checked:
            for member_id, message_id in ChatUnreadException.objects.filter(
                    membership_id__in=sparse, message_id__in=checked
            ).values_list("membership_id", "message_id"):
                positions[sparse[member_id]].unread_exception_message_ids.append(message_id)
    return positions


def recipient_positions(room, user_ids=None, message_ids=None, *, backend=None):
    """User-keyed one-room wrapper over the batched primary snapshot."""
    return {user_id: position for (_room_id, user_id), position in positions_for_rooms(
        [getattr(room, "pk", room)], user_ids, message_ids, backend=backend).items()}


@use_primary()
def unread_counts(user, room_ids=None, *, backend=None):
    if read_backend(backend) != "cursor":
        raise ValueError("unread_counts requires cursor read state")
    from ..models import ChatMembership
    qs = ChatMembership.objects.filter(
        user_id=getattr(user, "pk", user), status__in=["active", "muted"])
    if room_ids is not None:
        qs = qs.filter(room_id__in=room_ids)
    return {m.room_id: _position(m, m.room) for m in qs.select_related("room")}


def message_is_read(message, position):
    """Pure snapshot predicate; insufficient exception detail raises explicitly."""
    if message.room_id != position["room_id"]:
        raise ValueError("Message and position must belong to the same room")
    if message.room_seq is None:
        return True
    if message.room_seq > position["read_seq"]:
        return False
    if not position["unread_exception_count"]:
        return True
    if message.pk not in position.get("checked_message_ids", ()):
        raise ReadStateUnavailable("Supply message_ids to recipient_positions for sparse eligibility")
    return message.pk not in position.get("unread_exception_message_ids", ())


def unread_queryset(membership, queryset=None):
    """Apply exact unread eligibility BEFORE caller ordering/content limits.

    No display filtering here: maintenance must retain hidden unread identities.
    Use visible_messages(room, membership) as the base for user-facing readers.
    """
    from ..models import ChatMessage, ChatUnreadException
    # This queryset is evaluated by its caller, after this function returns.
    # Bind it explicitly so an outer use_reader scope cannot move eligibility
    # onto a lagging replica after a freshness-critical read-state operation.
    qs = (queryset if queryset is not None else ChatMessage.objects.all()).using("default")
    qs = qs.filter(room_id=membership.room_id)
    condition = Q(room_seq__gt=membership.last_read_seq)
    if membership.unread_exception_count:
        exceptions = ChatUnreadException.objects.filter(
            membership_id=membership.pk, message_id=OuterRef("pk"), room_seq=OuterRef("room_seq"))
        qs = qs.alias(_chat_unread_exception=Exists(exceptions))
        condition |= Q(_chat_unread_exception=True)
    return qs.filter(condition)


@use_primary()
def unread_message_ids(membership, message_ids):
    return set(unread_queryset(membership).filter(pk__in=message_ids).values_list("pk", flat=True))


@use_primary()
def resolve_read_target(room, membership, up_to_message_id, *, backend=None):
    if read_backend(backend) == "legacy":
        return _legacy_resolve_read_target(room, membership, up_to_message_id)
    try:
        target_id = int(up_to_message_id)
    except (ValueError, TypeError, OverflowError):
        return None
    if target_id < 1 or target_id > 9223372036854775807 or not membership:
        return None
    return join_bounded_messages(room, membership).filter(pk=target_id).first()


def _handler(handler):
    if handler is not _UNSET:
        return handler
    configured = settings.get_static("CHAT_READ_HANDLER", None)
    return modules.load_function(configured) if configured else None


def schedule_read_event(room_id, user_id, facts, changed, *, handler=_UNSET, publisher=None,
                        include_inactive=False):
    """Publish fresh standing state; immutable actual ack facts never expand.

    The service owns the cursor chat_read frame for REST, WS and own sends.
    A maintenance generation change invalidates an earlier callback completely.
    """
    facts = dict(facts)
    def committed():
        publish_allowed = True
        try:
            if include_inactive:
                from ..models import ChatMembership
                member = ChatMembership.objects.using("default").filter(
                    room_id=room_id, user_id=user_id).select_related("room").first()
                positions = {user_id: _position(member, member.room)} if member else {}
                publish_allowed = member is not None and member.status in ("active", "muted")
            else:
                positions = recipient_positions(room_id, [user_id], backend="cursor")
        except Exception:
            logger.exception("chat: committed read state unavailable")
            return
        current = positions.get(user_id)
        if current is None or current.generation != facts["ack_generation"]:
            return
        current.update(facts)
        if publisher is not False and publish_allowed:
            from mojo.apps.realtime import publish_topic
            publish = publisher or publish_topic
            try:
                publish(f"chat:{room_id}", dict(
                    current, type="chat_read", up_to_message_id=current.read_frontier_message_id))
            except Exception:
                logger.exception("chat: committed read publication failed")
        try:
            callback = _handler(handler)
            if callback:
                callback(room_id, user_id, current, changed)
        except Exception:
            logger.exception("chat: committed read handler failed")
    transaction.on_commit(committed)


def advance_locked(member, room, target, *, head=None, reason="explicit", handler=_UNSET, publisher=None):
    """Caller holds the membership lock, and room lock when creating a message."""
    from ..models import ChatMembership, ChatUnreadException
    _position(member, room)
    target_seq = target.room_seq if target is not None else head
    changed = False
    if target_seq is not None:
        if target_seq > room.message_seq:
            raise ReadStateUnavailable("Read target exceeds committed room head")
        removed = 0
        if member.unread_exception_count:
            removed, _ = ChatUnreadException.objects.filter(
                membership=member, room_seq__lte=target_seq).delete()
            if removed > member.unread_exception_count:
                raise ReadStateUnavailable("Unread exception cache is inconsistent")
        changed = target_seq > member.last_read_seq or bool(removed)
        member.unread_exception_count -= removed
        member.last_read_seq = max(member.last_read_seq, target_seq)
        # A stale read is valid but cannot replace newer actual acknowledgment facts.
        if target is not None and (member.last_read_message_id is None
                                   or target.pk > member.last_read_message_id):
            member.last_read_message_id = target.pk
            member.last_read_at = target.created
            changed = True
        if changed:
            ChatMembership.objects.filter(pk=member.pk).update(
                last_read_seq=member.last_read_seq,
                last_read_message_id=member.last_read_message_id,
                last_read_at=member.last_read_at,
                unread_exception_count=member.unread_exception_count)
    facts = dict(
        ack_target_id=target.pk if target is not None else None,
        ack_target_seq=target.room_seq if target is not None else head,
        ack_target_created=target.created.isoformat() if target is not None else None,
        ack_generation=room.read_state_generation, reason=reason)
    position = _position(member, room)
    position.update(facts)
    schedule_read_event(room.pk, member.user_id, facts, changed, handler=handler, publisher=publisher)
    return objict(target=target, position=position, changed=changed)


def mark_through(room, user, membership, up_to_message_id, *, handler=_UNSET, publisher=None, backend=None):
    if read_backend(backend) == "legacy":
        target = _legacy_mark_read(room, user, membership, up_to_message_id)
        return objict(target=target, position=None, changed=target is not None)
    from ..models import ChatRoom, ChatMembership
    if membership is None:
        return objict(target=None, position=None, changed=False)
    with transaction.atomic():
        member = ChatMembership.objects.select_for_update(of=("self",)).filter(
            pk=membership.pk, room_id=room.pk, user_id=user.pk,
            status__in=["active", "muted"]).first()
        if member is None:
            return objict(target=None, position=None, changed=False)
        current_room = ChatRoom.objects.get(pk=member.room_id)
        target = resolve_read_target(current_room, member, up_to_message_id, backend="cursor")
        if target is None:
            return objict(target=None, position=_position(member, current_room), changed=False)
        return advance_locked(member, current_room, target, handler=handler, publisher=publisher)


def mark_read(room, user, membership, up_to_message_id, *, backend=None, handler=_UNSET, publisher=None):
    return mark_through(room, user, membership, up_to_message_id,
                        backend=backend, handler=handler, publisher=publisher).target


def mark_current_head(room, user, membership, *, handler=_UNSET, publisher=None, backend=None):
    if read_backend(backend) != "cursor":
        raise ValueError("mark_current_head requires cursor read state")
    from ..models import ChatRoom, ChatMembership
    with transaction.atomic():
        member = ChatMembership.objects.select_for_update(of=("self",)).get(
            pk=membership.pk, room_id=room.pk, user_id=user.pk, status__in=["active", "muted"])
        current_room = ChatRoom.objects.get(pk=member.room_id)
        return advance_locked(member, current_room, None, head=current_room.message_seq,
                              reason="current_head", handler=handler, publisher=publisher)


def _legacy_resolve_read_target(room, membership, up_to_message_id):
    """Clamp a client-supplied read bound to the newest message the caller
    could legitimately have seen at or below it. Returns a ChatMessage or None.

    None means "nothing to acknowledge": the value was not a usable message id,
    or the caller is entitled to no message at or below it. Callers must write
    nothing and broadcast nothing in that case.
    """
    try:
        up_to = int(up_to_message_id)
    except (TypeError, ValueError):
        # A non-numeric id is a client bug, not a server error. Returning None
        # is what keeps the REST view from raising ValueError into a 500.
        return None
    if up_to < 1:
        return None

    return join_bounded_messages(room, membership).filter(
        pk__lte=up_to,
    ).order_by("-pk").first()


def _legacy_mark_read(room, user, membership, up_to_message_id):
    """Resolve the target, write the receipts and `last_read_at`.

    Returns the resolved ChatMessage, or None when nothing resolved -- in which
    case nothing at all was written.
    """
    from ..models import ChatReadReceipt

    target = _legacy_resolve_read_target(room, membership, up_to_message_id)
    if target is None:
        return None

    if room.kind != "channel":
        # Receipts are bounded by pk, `last_read_at` by `created`: `created` is
        # auto_now_add set in Python while pk comes from the database sequence,
        # so under concurrent inserts the two orderings can disagree. The
        # pairing is chosen to match how `GET /api/chat/unread` reads each --
        # it is not a claim that they agree.
        unread_ids = join_bounded_messages(room, membership).filter(
            pk__lte=target.pk,
        ).exclude(
            user=user,  # Don't create receipts for own messages
        ).exclude(
            read_receipts__user=user,  # Skip already-read messages
        ).values_list("pk", flat=True)

        receipts = [
            ChatReadReceipt(message_id=msg_id, user=user)
            for msg_id in unread_ids
        ]
        if receipts:
            ChatReadReceipt.objects.bulk_create(receipts, ignore_conflicts=True)

    # Monotonic on purpose. There is no mark-unread feature, and the channel
    # branch of `GET /api/chat/unread` counts `created__gt=last_read_at`, so
    # rewinding would resurrect already-read messages as unread.
    if membership is not None and (
            membership.last_read_at is None
            or target.created > membership.last_read_at):
        membership.last_read_at = target.created
        membership.save(update_fields=["last_read_at"])

    return target
