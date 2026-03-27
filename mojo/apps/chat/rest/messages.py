from mojo import decorators as md
from mojo import errors as merrors
from mojo.helpers import dates
from ..models import (
    ChatRoom, ChatMessage, ChatMembership,
    ChatReadReceipt,
)


@md.GET('room/messages')
@md.requires_auth()
@md.requires_params('room_id')
def on_chat_room_messages(request):
    """
    Get paginated message history for a room.
    Excludes flagged messages. Supports cursor-based pagination via `before` (message id).
    """
    room = ChatRoom.objects.filter(pk=request.DATA.room_id).first()
    if not room:
        return ChatRoom.rest_error_response(request, 404, error="Room not found")

    # Must be a member
    membership = ChatMembership.objects.filter(
        room=room, user=request.user, status__in=["active", "muted"],
    ).first()
    if not membership:
        if not (room.group and room.group.user_has_permission(request.user, ["chat", "manage_chat"])):
            raise merrors.PermissionDeniedException()

    qs = ChatMessage.objects.filter(room=room, is_flagged=False)

    # For group rooms, only show messages from after the user joined
    if room.kind == "group" and membership:
        qs = qs.filter(created__gte=membership.joined_at)

    # Cursor pagination: messages before a given message id
    before = request.DATA.get("before")
    if before:
        qs = qs.filter(pk__lt=int(before))

    # Filter out expired disappearing messages
    ttl = room.get_rule("disappearing_ttl", 0)
    if ttl:
        from datetime import timedelta
        cutoff = dates.utcnow() - timedelta(seconds=ttl)
        qs = qs.filter(created__gte=cutoff)

    qs = qs.order_by("-created")

    limit = int(request.DATA.get("limit", 50))
    limit = min(limit, 200)
    messages = list(qs[:limit])

    data = []
    for msg in messages:
        data.append({
            "id": msg.pk,
            "user_id": msg.user_id,
            "body": msg.body,
            "kind": msg.kind,
            "edited_at": msg.edited_at.isoformat() if msg.edited_at else None,
            "moderation_decision": msg.moderation_decision,
            "created": msg.created.isoformat(),
            "metadata": msg.metadata,
        })

    has_more = len(messages) == limit
    return {
        "status": True,
        "data": data,
        "has_more": has_more,
        "cursor": messages[-1].pk if messages and has_more else None,
    }


@md.GET('room/flagged')
@md.requires_auth()
@md.requires_params('room_id')
def on_chat_room_flagged(request):
    """Get flagged messages for moderator review."""
    room = ChatRoom.objects.filter(pk=request.DATA.room_id).first()
    if not room:
        return ChatRoom.rest_error_response(request, 404, error="Room not found")

    # Check moderator permission
    from .rooms import _check_room_moderator
    _check_room_moderator(request, room)

    qs = ChatMessage.objects.filter(room=room, is_flagged=True).order_by("-flagged_at")
    return ChatMessage.on_rest_list(request, queryset=qs)


@md.POST('dm')
@md.requires_auth()
@md.requires_params('user_id')
def on_chat_dm(request):
    """
    Get or create a direct message room with the given user.
    Returns the existing room if one already exists.
    """
    from mojo.apps.account.models import User

    target_user_id = int(request.DATA.user_id)
    if target_user_id == request.user.pk:
        return ChatRoom.rest_error_response(request, 400, error="Cannot DM yourself")

    target_user = User.objects.filter(pk=target_user_id).first()
    if not target_user:
        return ChatRoom.rest_error_response(request, 404, error="User not found")

    # Check if a DM room already exists between these two users
    my_rooms = ChatMembership.objects.filter(
        user=request.user,
        room__kind="direct",
    ).values_list("room_id", flat=True)

    existing_room = ChatMembership.objects.filter(
        user=target_user,
        room_id__in=my_rooms,
        room__kind="direct",
    ).select_related("room").first()

    if existing_room:
        return existing_room.room.on_rest_get(request)

    # Create new DM room
    room = ChatRoom.objects.create(
        kind="direct",
        user=request.user,
    )
    ChatMembership.objects.create(room=room, user=request.user, role="owner")
    ChatMembership.objects.create(room=room, user=target_user, role="member")

    return room.on_rest_get(request)


@md.POST('room/read')
@md.requires_auth()
@md.requires_params('room_id', 'up_to_message_id')
def on_chat_room_read(request):
    """Mark messages as read up to a given message id."""
    room = ChatRoom.objects.filter(pk=request.DATA.room_id).first()
    if not room:
        return ChatRoom.rest_error_response(request, 404, error="Room not found")

    membership = ChatMembership.objects.filter(room=room, user=request.user).first()
    if not membership:
        return ChatRoom.rest_error_response(request, 404, error="Not a member")

    up_to = int(request.DATA.up_to_message_id)

    if room.kind == "channel":
        membership.last_read_at = dates.utcnow()
        membership.save(update_fields=["last_read_at"])
    else:
        # Bulk create read receipts for unread messages
        unread_ids = ChatMessage.objects.filter(
            room=room,
            pk__lte=up_to,
            is_flagged=False,
        ).exclude(
            user=request.user,
        ).exclude(
            read_receipts__user=request.user,
        ).values_list("pk", flat=True)

        receipts = [
            ChatReadReceipt(message_id=msg_id, user=request.user)
            for msg_id in unread_ids
        ]
        if receipts:
            ChatReadReceipt.objects.bulk_create(receipts, ignore_conflicts=True)

        membership.last_read_at = dates.utcnow()
        membership.save(update_fields=["last_read_at"])

    return {"status": True}


@md.GET('unread')
@md.requires_auth()
def on_chat_unread(request):
    """Get unread message counts per room for the authenticated user."""
    memberships = ChatMembership.objects.filter(
        user=request.user,
        status__in=["active", "muted"],
    ).select_related("room")

    counts = []
    for ms in memberships:
        qs = ChatMessage.objects.filter(
            room=ms.room,
            is_flagged=False,
        ).exclude(user=request.user)

        if ms.room.kind == "channel":
            # Channel: count messages after last_read_at
            if ms.last_read_at:
                count = qs.filter(created__gt=ms.last_read_at).count()
            else:
                count = qs.count()
        else:
            # Direct/group: count messages without a read receipt
            count = qs.exclude(
                read_receipts__user=request.user,
            ).count()

        if count > 0:
            counts.append({
                "room_id": ms.room_id,
                "room_name": ms.room.name,
                "room_kind": ms.room.kind,
                "unread_count": count,
            })

    return {"status": True, "data": counts}
