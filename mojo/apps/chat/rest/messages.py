from mojo import decorators as md
from mojo import errors as merrors
from mojo.apps.realtime import policy
from mojo.helpers.request import is_override_user_session
from .rooms import _deny_cross_tenant_room, _refuse_by_policy
from ..models import ChatRoom, ChatMessage, ChatMembership
from ..services import reachability
from ..services.messages import visible_messages
from ..services.read_state import mark_read


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

    _deny_cross_tenant_room(request, room)

    # Must be a member
    membership = ChatMembership.objects.filter(
        room=room, user=request.user, status__in=["active", "muted"],
    ).first()
    if not membership:
        if not (room.group and room.group.user_has_permission(
                request.user, ["chat", "manage_chat"],
                not is_override_user_session(request))):
            raise merrors.PermissionDeniedException()
    _refuse_by_policy(request, room)

    # Unflagged, the join-time cutoff for every non-channel kind, and the
    # disappearing-message TTL. One shared bound so history and the unread
    # counter can never disagree.
    qs = visible_messages(room, membership)

    # Cursor pagination: messages before a given message id
    before = request.DATA.get("before")
    if before:
        qs = qs.filter(pk__lt=int(before))

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
            "moderation_reasons": msg.moderation_reasons,
            "moderation_score": msg.moderation_score,
            "created": msg.created.isoformat(),
            "metadata": msg.metadata,
            # Author-scoped: client keys are client-chosen and may encode
            # device identity or content equality, so only the sender of a
            # message ever sees its key.
            "client_key": msg.client_key if msg.user_id == request.user.pk else None,
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

    _deny_cross_tenant_room(request, room)

    # Check moderator permission
    from .rooms import _check_room_moderator
    _check_room_moderator(request, room)

    qs = ChatMessage.objects.filter(room=room, is_flagged=True).order_by("-flagged_at")
    return ChatMessage.on_rest_list(request, queryset=qs)


@md.POST('dm')
@md.requires_auth()
def on_chat_dm(request):
    """
    Get or create a direct message room with the given user.

    Reuse is groupless-first: a personal DM is groupless by construction, so
    this endpoint never hands out a tenant-managed room as if it were one. If
    the pair's only shared direct room IS group-scoped, that room is returned
    as-is, with its `group` field set, and the client decides what to do about
    it. Minting a second groupless room instead would strand the conversation:
    the history stays in the old room, `/api/chat/rooms` lists both, and
    neither party learns why the thread went blank.

    A NEW room needs the reachability rule (services/reachability.py); with no
    hook that is any active user. Every target-side refusal -- no id, a bad id,
    the caller's own, a missing, inactive or refused user -- is the one 404
    "User not found", with no row written. A DM that opens does tell the
    caller the id is an active account.
    """
    # A DM room is groupless by construction, so no confined credential
    # (ApiKey / GroupScopedToken) may open or reach one — the same
    # None-group deny every room-resolving endpoint applies, enforced up front
    # so the create and reuse paths agree.
    _deny_cross_tenant_room(request, None)

    target_id = reachability.parse_user_id(request.DATA.get("user_id"))
    if target_id is None or reachability.is_caller(request, target_id):
        return reachability.refuse(request)

    # Every direct room the caller is in. The group predicate is applied to
    # each lookup below, never here: the two cases stay separate and explicit
    # so neither can silently answer for the other.
    my_rooms = ChatMembership.objects.filter(
        user=request.user,
        room__kind="direct",
    ).values_list("room_id", flat=True)

    # Reuse runs before the rule: both people are already members of the room
    # it returns, so it lets nobody reach anyone new and answers nothing about
    # an id the caller does not already share a room with.

    # 1. A personal DM the pair already shares. Groupless, like the one the
    # create path below builds.
    existing_room = ChatMembership.objects.filter(
        user_id=target_id,
        room_id__in=my_rooms,
        room__kind="direct",
        room__group__isnull=True,
    ).select_related("room").first()

    if existing_room:
        return _reuse_dm(request, existing_room.room)

    # 2. No personal DM, but the pair may already share a tenant-managed one.
    # Return it rather than forking the conversation into a second room; the
    # response carries `group`, which is how a client tells the two apart.
    scoped_room = ChatMembership.objects.filter(
        user_id=target_id,
        room_id__in=my_rooms,
        room__kind="direct",
        room__group__isnull=False,
    ).select_related("room").first()

    if scoped_room:
        return _reuse_dm(request, scoped_room.room)

    target_user = reachability.reachable_user(request, target_id)
    if target_user is None:
        return reachability.refuse(request)

    # Create new DM room
    room = ChatRoom.objects.create(
        kind="direct",
        user=request.user,
    )
    ChatMembership.objects.create(room=room, user=request.user, role="owner")
    ChatMembership.objects.create(room=room, user=target_user, role="member")

    return room.on_rest_get(request)


def _reuse_dm(request, room):
    """Hand back a DM the pair already shares, unless the project's
    REALTIME_SUBSCRIBE_HANDLER refuses the caller that room (maestro #8874).
    The refusal is the uniform "User not found" and creates nothing; with no
    hook the room comes back as before (#8345)."""
    if not policy.allows(request.user, room.topic, True):
        return reachability.refuse(request)
    return room.on_rest_get(request)


@md.POST('room/read')
@md.requires_auth()
@md.requires_params('room_id', 'up_to_message_id')
def on_chat_room_read(request):
    """Mark messages as read up to a given message id.

    `up_to_message_id` is clamped to the newest message the caller is entitled
    to; the resolved id comes back in the response (additive). A value that
    resolves to nothing writes nothing and returns `up_to_message_id: null` --
    including a non-numeric one, which used to raise `ValueError` out of the
    view as a 500.
    """
    room = ChatRoom.objects.filter(pk=request.DATA.room_id).first()
    if not room:
        return ChatRoom.rest_error_response(request, 404, error="Room not found")

    _deny_cross_tenant_room(request, room)

    # Active + muted, the same predicate history and `/unread` use. A banned
    # member has no standing to write read state; a muted member still reads.
    membership = ChatMembership.objects.filter(
        room=room, user=request.user, status__in=["active", "muted"],
    ).first()
    if not membership:
        return ChatRoom.rest_error_response(request, 404, error="Not a member")
    _refuse_by_policy(request, room)

    target = mark_read(
        room, request.user, membership, request.DATA.up_to_message_id)

    return {"status": True, "up_to_message_id": target.pk if target else None}


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
        # Same bound as the history endpoint, so a badge can never count a
        # message GET room/messages will not return.
        qs = visible_messages(ms.room, ms).exclude(user=request.user)

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
