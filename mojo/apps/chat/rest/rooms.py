from mojo import decorators as md
from mojo import errors as merrors
from mojo.helpers import dates
from ..models import ChatRoom, ChatMembership, ChatMessage


@md.URL('room')
@md.URL('room/<int:pk>')
@md.uses_model_security(ChatRoom)
def on_chat_room(request, pk=None):
    return ChatRoom.on_rest_request(request, pk)


@md.GET('rooms')
@md.requires_auth()
def on_chat_rooms_list(request):
    """List rooms the authenticated user is a member of."""
    room_ids = ChatMembership.objects.filter(
        user=request.user,
        status__in=["active", "muted"],
    ).values_list("room_id", flat=True)
    qs = ChatRoom.objects.filter(pk__in=room_ids)
    return ChatRoom.on_rest_list(request, queryset=qs)


@md.POST('room/join')
@md.requires_auth()
@md.requires_params('room_id')
def on_chat_room_join(request):
    """Join a channel room. Only works for kind=channel."""
    room = ChatRoom.objects.filter(pk=request.DATA.room_id).first()
    if not room:
        return ChatRoom.rest_error_response(request, 404, error="Room not found")

    if room.kind != "channel":
        return ChatRoom.rest_error_response(
            request, 403, error="Can only join channel rooms",
        )

    # If group-linked, check group membership
    if room.group:
        if not room.group.user_has_permission(request.user, ["chat", "manage_chat"]):
            raise merrors.PermissionDeniedException()

    membership, created = ChatMembership.objects.get_or_create(
        room=room, user=request.user,
        defaults={"role": "member", "status": "active"},
    )

    if not created and membership.status == "banned":
        return ChatRoom.rest_error_response(
            request, 403, error="You are banned from this room",
        )

    if not created and membership.status != "active":
        membership.status = "active"
        membership.save(update_fields=["status"])

    # System message
    if created:
        ChatMessage.objects.create(
            room=room, user=request.user,
            body=f"{request.user.display_name or request.user.username} joined",
            kind="system",
        )
        from mojo.apps.realtime import publish_topic
        publish_topic(room.topic, {
            "type": "chat_member_joined",
            "room_id": room.pk,
            "user_id": request.user.pk,
        })

    return membership.on_rest_get(request)


@md.POST('room/leave')
@md.requires_auth()
@md.requires_params('room_id')
def on_chat_room_leave(request):
    """Leave a room."""
    room = ChatRoom.objects.filter(pk=request.DATA.room_id).first()
    if not room:
        return ChatRoom.rest_error_response(request, 404, error="Room not found")

    if room.kind == "direct":
        return ChatRoom.rest_error_response(
            request, 400, error="Cannot leave a direct message room",
        )

    membership = ChatMembership.objects.filter(
        room=room, user=request.user,
    ).first()
    if not membership:
        return ChatRoom.rest_error_response(request, 404, error="Not a member")

    membership.delete()

    # System message
    ChatMessage.objects.create(
        room=room, user=request.user,
        body=f"{request.user.display_name or request.user.username} left",
        kind="system",
    )
    from mojo.apps.realtime import publish_topic
    publish_topic(room.topic, {
        "type": "chat_member_left",
        "room_id": room.pk,
        "user_id": request.user.pk,
    })

    return {"status": True}


@md.POST('room/member/add')
@md.requires_auth()
@md.requires_params('room_id', 'user_id')
def on_chat_room_add_member(request):
    """Add a member to a group or channel room. Requires admin role or manage_chat permission."""
    room = ChatRoom.objects.filter(pk=request.DATA.room_id).first()
    if not room:
        return ChatRoom.rest_error_response(request, 404, error="Room not found")

    if room.kind == "direct":
        return ChatRoom.rest_error_response(
            request, 400, error="Cannot add members to a direct message room",
        )

    # Permission check
    _check_room_admin(request, room)

    from mojo.apps.account.models import User
    target_user = User.objects.filter(pk=request.DATA.user_id).first()
    if not target_user:
        return ChatRoom.rest_error_response(request, 404, error="User not found")

    membership, created = ChatMembership.objects.get_or_create(
        room=room, user=target_user,
        defaults={"role": "member", "status": "active"},
    )

    if not created and membership.status == "banned":
        membership.status = "active"
        membership.save(update_fields=["status"])

    if created:
        ChatMessage.objects.create(
            room=room, user=request.user,
            body=f"{target_user.display_name or target_user.username} was added",
            kind="system",
        )

    return membership.on_rest_get(request)


@md.POST('room/member/remove')
@md.requires_auth()
@md.requires_params('room_id', 'user_id')
def on_chat_room_remove_member(request):
    """Remove a member from a room. Requires admin role or manage_chat permission."""
    room = ChatRoom.objects.filter(pk=request.DATA.room_id).first()
    if not room:
        return ChatRoom.rest_error_response(request, 404, error="Room not found")

    _check_room_admin(request, room)

    membership = ChatMembership.objects.filter(
        room=room, user_id=request.DATA.user_id,
    ).first()
    if not membership:
        return ChatRoom.rest_error_response(request, 404, error="Member not found")

    membership.delete()
    return {"status": True}


@md.POST('room/member/mute')
@md.requires_auth()
@md.requires_params('room_id', 'user_id')
def on_chat_room_mute_member(request):
    """Mute a member. Requires admin or moderate_chat permission."""
    room = ChatRoom.objects.filter(pk=request.DATA.room_id).first()
    if not room:
        return ChatRoom.rest_error_response(request, 404, error="Room not found")

    _check_room_moderator(request, room)

    membership = ChatMembership.objects.filter(
        room=room, user_id=request.DATA.user_id,
    ).first()
    if not membership:
        return ChatRoom.rest_error_response(request, 404, error="Member not found")

    membership.status = "muted"
    membership.save(update_fields=["status"])
    return membership.on_rest_get(request)


@md.POST('room/member/ban')
@md.requires_auth()
@md.requires_params('room_id', 'user_id')
def on_chat_room_ban_member(request):
    """Ban a member. Requires admin or moderate_chat permission."""
    room = ChatRoom.objects.filter(pk=request.DATA.room_id).first()
    if not room:
        return ChatRoom.rest_error_response(request, 404, error="Room not found")

    _check_room_moderator(request, room)

    membership = ChatMembership.objects.filter(
        room=room, user_id=request.DATA.user_id,
    ).first()
    if not membership:
        return ChatRoom.rest_error_response(request, 404, error="Member not found")

    membership.status = "banned"
    membership.save(update_fields=["status"])
    return membership.on_rest_get(request)


@md.POST('room/rules')
@md.requires_auth()
@md.requires_params('room_id')
def on_chat_room_update_rules(request):
    """Update room rules. Requires admin role or manage_chat permission."""
    room = ChatRoom.objects.filter(pk=request.DATA.room_id).first()
    if not room:
        return ChatRoom.rest_error_response(request, 404, error="Room not found")

    _check_room_admin(request, room)

    # Merge new rules into existing
    allowed_keys = {
        "allow_urls", "allow_media", "allow_phone_numbers",
        "max_message_length", "disappearing_ttl", "rate_limit",
    }
    rules = dict(room.rules or {})
    for key in allowed_keys:
        if key in request.DATA:
            rules[key] = request.DATA[key]

    room.rules = rules
    room.save(update_fields=["rules"])
    return room.on_rest_get(request)


@md.GET('room/members')
@md.requires_auth()
@md.requires_params('room_id')
def on_chat_room_members(request):
    """List members of a room."""
    room = ChatRoom.objects.filter(pk=request.DATA.room_id).first()
    if not room:
        return ChatRoom.rest_error_response(request, 404, error="Room not found")

    # Must be a member or have manage_chat
    membership = ChatMembership.objects.filter(room=room, user=request.user).first()
    if not membership:
        if not (room.group and room.group.user_has_permission(request.user, "manage_chat")):
            raise merrors.PermissionDeniedException()

    qs = ChatMembership.objects.filter(room=room).exclude(status="banned")
    return ChatMembership.on_rest_list(request, queryset=qs)


@md.GET('room/online')
@md.requires_auth()
@md.requires_params('room_id')
def on_chat_room_online(request):
    """Get online members of a room."""
    from mojo.apps.realtime import is_online

    room = ChatRoom.objects.filter(pk=request.DATA.room_id).first()
    if not room:
        return ChatRoom.rest_error_response(request, 404, error="Room not found")

    members = ChatMembership.objects.filter(
        room=room, status__in=["active", "muted"],
    ).select_related("user")

    online_users = []
    for ms in members:
        if is_online("user", ms.user_id):
            online_users.append({
                "user_id": ms.user_id,
                "username": ms.user.username,
                "display_name": getattr(ms.user, "display_name", ""),
                "role": ms.role,
            })

    return {"status": True, "data": online_users}


def _check_room_admin(request, room):
    """Check if request user is a room admin or has manage_chat permission. Raises on failure."""
    membership = ChatMembership.objects.filter(room=room, user=request.user).first()
    if membership and membership.is_admin:
        return
    if room.group and room.group.user_has_permission(request.user, "manage_chat"):
        return
    if request.user.has_permission("manage_chat"):
        return
    raise merrors.PermissionDeniedException()


def _check_room_moderator(request, room):
    """Check if request user can moderate (admin, moderate_chat, or manage_chat). Raises on failure."""
    membership = ChatMembership.objects.filter(room=room, user=request.user).first()
    if membership and membership.is_admin:
        return
    if room.group and room.group.user_has_permission(request.user, ["moderate_chat", "manage_chat"]):
        return
    if request.user.has_permission(["moderate_chat", "manage_chat"]):
        return
    raise merrors.PermissionDeniedException()
