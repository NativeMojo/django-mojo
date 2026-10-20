from mojo import decorators as md
from mojo import errors as merrors
from mojo.apps.realtime import policy
from mojo.helpers import dates
from mojo.helpers.request import (
    identity_allows_group, is_override_user_session, restricted_identity)
from ..models import ChatRoom, ChatMembership
from ..services import reachability, room_authority
from ..services.access import publish_access_change
from ..services.messages import send_message


def _deny_cross_tenant_room(request, room):
    """Confine a restricted identity (ApiKey / GroupScopedToken) to rooms in
    its own group.

    These endpoints authorize against a caller-supplied `room_id` and then
    serve rosters, bodies and memberships through the GROUPLESS
    ChatMembership / ChatMessage models — model security never sees a tenant to
    re-bind to, so the bound has to be applied here. A room with no group
    (direct messages, personal rooms) denies every restricted identity:
    fail-closed, since a confined credential has no tenant claim on it.
    """
    if not identity_allows_group(request, getattr(room, "group", None)):
        raise merrors.PermissionDeniedException()


def _refuse_banned(request, room):
    """A banned caller has no say in a room except leaving it (#8347). Fixed:
    no authority hook runs before it or can lift it."""
    if room_authority.is_banned(request, room):
        raise merrors.PermissionDeniedException()


def _refuse_by_policy(request, room):
    """403 when the project's REALTIME_SUBSCRIBE_HANDLER refuses the caller
    this room's topic (maestro #8874). Asked after the view's own checks, so
    it can only narrow them; with no hook it never refuses."""
    if not policy.allows(request.user, room.topic, True):
        raise merrors.PermissionDeniedException()


@md.URL('room')
@md.URL('room/<int:pk>')
@md.uses_model_security(ChatRoom)
def on_chat_room(request, pk=None):
    """RestMeta CRUD for rooms. A write or delete on one room first passes the
    room-authority rule (services/room_authority.py); RestMeta perms still run
    afterwards. Create (no pk) and reads are the framework's alone."""
    if pk and request.method in ("POST", "PUT", "PATCH", "DELETE"):
        room = ChatRoom.objects.filter(pk=pk).first()
        # A missing room keeps the framework's 404 below.
        if room is not None:
            _deny_cross_tenant_room(request, room)
            if request.method == "DELETE":
                _refuse_banned(request, room)
                room_authority.require(request, room, "delete")
            else:
                changes, rules = room_authority.update_changes(room, request.DATA)
                _refuse_banned(request, room)
                room_authority.require(request, room, "update", changes=changes)
                if rules is not None:
                    request.DATA["rules"] = rules
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
    # Same tenant bound as the room-resolving endpoints below: a confined
    # credential lists only rooms inside its own group. request.user is a real
    # User under a group token (and under an override ApiKey), so an unfiltered
    # membership read would return every tenant's rooms the visitor belongs to.
    # Groupless rooms (DMs) drop out — consistent with _deny_cross_tenant_room.
    identity = restricted_identity(request)
    if identity is not None:
        qs = qs.filter(group__in=identity.get_groups())
    return ChatRoom.on_rest_list(request, queryset=qs)


@md.POST('room/join')
@md.requires_auth()
@md.requires_params('room_id')
def on_chat_room_join(request):
    """Join a channel room. Only works for kind=channel."""
    room = ChatRoom.objects.filter(pk=request.DATA.room_id).first()
    if not room:
        return ChatRoom.rest_error_response(request, 404, error="Room not found")

    _deny_cross_tenant_room(request, room)

    if room.kind != "channel":
        return ChatRoom.rest_error_response(
            request, 403, error="Can only join channel rooms",
        )

    # If group-linked, check group membership. Fixed: no hook loosens it.
    if room.group:
        if not room.group.user_has_permission(
                request.user, ["chat", "manage_chat"],
                not is_override_user_session(request)):
            raise merrors.PermissionDeniedException()

    _refuse_banned(request, room)
    # Allowed by default; a project hook may narrow it.
    room_authority.require(request, room, "join")

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
        # broadcast=False is load-bearing: this site publishes
        # chat_member_joined below, never a chat_message frame.
        send_message(
            room, request.user,
            f"{request.user.display_name or request.user.username} joined",
            kind="system", client_authored=False,
            enforce_room_policy=False, broadcast=False,
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

    _deny_cross_tenant_room(request, room)

    if room.kind == "direct":
        return ChatRoom.rest_error_response(
            request, 400, error="Cannot leave a direct message room",
        )

    room_authority.require(request, room, "leave")

    membership = ChatMembership.objects.filter(
        room=room, user=request.user,
    ).first()
    if not membership:
        return ChatRoom.rest_error_response(request, 404, error="Not a member")

    membership.delete()

    # System message
    # broadcast=False: chat_member_left is published below instead.
    send_message(
        room, request.user,
        f"{request.user.display_name or request.user.username} left",
        kind="system", client_authored=False,
        enforce_room_policy=False, broadcast=False,
    )
    publish_access_change(room.pk, "chat_member_left", request.user.pk)

    return {"status": True}


@md.POST('room/member/add')
@md.requires_auth()
@md.requires_params('room_id')
def on_chat_room_add_member(request):
    """Add a member to a group or channel room. Requires admin role or manage_chat permission.

    The target must pass the reachability rule (services/reachability.py):
    with no hook, any active user, in or outside the room's group. Room-side
    answers come first; every target-side refusal is the one 404 "User not
    found", with no row written and no system message.
    """
    room = ChatRoom.objects.filter(pk=request.DATA.room_id).first()
    if not room:
        return ChatRoom.rest_error_response(request, 404, error="Room not found")

    _deny_cross_tenant_room(request, room)

    if room.kind == "direct":
        return ChatRoom.rest_error_response(
            request, 400, error="Cannot add members to a direct message room",
        )

    # Permission check
    _check_room_admin(request, room)

    target_id = reachability.parse_user_id(request.DATA.get("user_id"))
    if target_id is None or reachability.is_caller(request, target_id):
        return reachability.refuse(request)
    target_user = reachability.reachable_user(request, target_id, room=room)
    if target_user is None:
        return reachability.refuse(request)

    # Only after the rule passes: a ban is lifted only by an active admin
    # adding someone still reachable. There is no other unban path.
    membership, created = ChatMembership.objects.get_or_create(
        room=room, user=target_user,
        defaults={"role": "member", "status": "active"},
    )

    if not created and membership.status == "banned":
        membership.status = "active"
        membership.save(update_fields=["status"])

    if created:
        # broadcast=False: this site publishes nothing on the room topic.
        send_message(
            room, request.user,
            f"{target_user.display_name or target_user.username} was added",
            kind="system", client_authored=False,
            enforce_room_policy=False, broadcast=False,
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

    _deny_cross_tenant_room(request, room)

    user_id = reachability.parse_user_id(request.DATA.get("user_id"))
    if user_id is None:
        return ChatRoom.rest_error_response(request, 400, error="Invalid user_id")

    _refuse_banned(request, room)
    # One lookup: the membership the rule judges is the one changed below.
    membership = _target_membership(room, user_id)
    room_authority.require(request, room, "member_remove", target=membership)
    if not membership:
        return ChatRoom.rest_error_response(request, 404, error="Member not found")

    membership.delete()
    # Open sockets remember room access; this frame makes theirs re-check.
    publish_access_change(room.pk, "chat_member_removed", membership.user_id)
    return {"status": True}


@md.POST('room/member/mute')
@md.requires_auth()
@md.requires_params('room_id', 'user_id')
def on_chat_room_mute_member(request):
    """Mute a member. Requires admin or moderate_chat permission."""
    room = ChatRoom.objects.filter(pk=request.DATA.room_id).first()
    if not room:
        return ChatRoom.rest_error_response(request, 404, error="Room not found")

    _deny_cross_tenant_room(request, room)

    user_id = reachability.parse_user_id(request.DATA.get("user_id"))
    if user_id is None:
        return ChatRoom.rest_error_response(request, 400, error="Invalid user_id")

    _refuse_banned(request, room)
    # One lookup: the membership the rule judges is the one changed below.
    membership = _target_membership(room, user_id)
    room_authority.require(request, room, "member_mute", target=membership)
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

    _deny_cross_tenant_room(request, room)

    user_id = reachability.parse_user_id(request.DATA.get("user_id"))
    if user_id is None:
        return ChatRoom.rest_error_response(request, 400, error="Invalid user_id")

    _refuse_banned(request, room)
    # One lookup: the membership the rule judges is the one changed below.
    membership = _target_membership(room, user_id)
    room_authority.require(request, room, "member_ban", target=membership)
    if not membership:
        return ChatRoom.rest_error_response(request, 404, error="Member not found")

    membership.status = "banned"
    membership.save(update_fields=["status"])
    # Open sockets remember room access; this frame makes theirs re-check.
    publish_access_change(room.pk, "chat_member_banned", membership.user_id)
    return membership.on_rest_get(request)


@md.POST('room/rules')
@md.requires_auth()
@md.requires_params('room_id')
def on_chat_room_update_rules(request):
    """Update room rules. Requires admin role or manage_chat permission.

    Every value is checked first (400 naming the key and its range). Turning
    disappearing messages on or shortening them needs global manage_chat by
    default (services/room_authority.py).
    """
    room = ChatRoom.objects.filter(pk=request.DATA.room_id).first()
    if not room:
        return ChatRoom.rest_error_response(request, 404, error="Room not found")

    _deny_cross_tenant_room(request, room)

    incoming = room_authority.validate_rules({
        key: request.DATA[key]
        for key in room_authority.RULE_KEYS if key in request.DATA
    })
    _refuse_banned(request, room)

    # Merge new rules into existing
    rules = dict(room.rules or {})
    rules.update(incoming)
    room_authority.require(request, room, "rules",
                           changes=room_authority.rules_changes(room, rules))

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

    _deny_cross_tenant_room(request, room)

    # Must be a member or have manage_chat
    membership = ChatMembership.objects.filter(room=room, user=request.user).first()
    if not membership:
        if not (room.group and room.group.user_has_permission(
                request.user, "manage_chat",
                not is_override_user_session(request))):
            raise merrors.PermissionDeniedException()
    _refuse_by_policy(request, room)

    qs = ChatMembership.objects.filter(room=room).exclude(status="banned")
    return ChatMembership.on_rest_list(request, queryset=qs)


@md.GET('room/online')
@md.requires_auth()
@md.requires_params('room_id')
def on_chat_room_online(request):
    """Get online members of a room.

    Gated like message history (rest/messages.py), not like the member list:
    an active/muted membership, else the group grant that already reads the
    room. Presence is weaker information than history, so whoever may read the
    one may read the other — and a banned row counts for neither. Without this
    gate any signed-in user could list who is online in a groupless DM by
    trying room ids (maestro #3859).
    """
    from mojo.apps.realtime import is_online

    room = ChatRoom.objects.filter(pk=request.DATA.room_id).first()
    if not room:
        return ChatRoom.rest_error_response(request, 404, error="Room not found")

    _deny_cross_tenant_room(request, room)

    membership = ChatMembership.objects.filter(
        room=room, user=request.user, status__in=["active", "muted"],
    ).first()
    if not membership:
        if not (room.group and room.group.user_has_permission(
                request.user, ["chat", "manage_chat"],
                not is_override_user_session(request))):
            raise merrors.PermissionDeniedException()
    _refuse_by_policy(request, room)

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


def _target_membership(room, user_id):
    """The member_* target's membership in `room`, or None. `user_id` is
    already parsed: the views never query with the raw request value."""
    return ChatMembership.objects.filter(room=room, user_id=user_id).first()


def _check_room_admin(request, room):
    """Check if request user is a room admin or has manage_chat permission. Raises on failure.

    `check_user` and the bare global fallback are BOTH skipped for a session
    that assumes a member (override ApiKey / group token): those reads are the
    member's untenanted platform-wide dict, which a confined credential must
    never inherit. Authority comes from the room membership or a member grant
    in the room's own group.

    A banned admin's membership carries no authority; a muted admin keeps it
    (a mute stops content, not room management).
    """
    membership = ChatMembership.objects.filter(room=room, user=request.user).first()
    if membership and membership.is_admin and membership.status != "banned":
        return
    check_user = not is_override_user_session(request)
    if room.group and room.group.user_has_permission(request.user, "manage_chat", check_user):
        return
    if check_user and request.user.has_permission("manage_chat"):
        return
    raise merrors.PermissionDeniedException()


def _check_room_moderator(request, room):
    """Check if request user can moderate (admin, moderate_chat, or manage_chat). Raises on failure.

    Same assumed-member discipline as _check_room_admin.
    """
    membership = ChatMembership.objects.filter(room=room, user=request.user).first()
    if membership and membership.is_admin and membership.status != "banned":
        return
    check_user = not is_override_user_session(request)
    if room.group and room.group.user_has_permission(
            request.user, ["moderate_chat", "manage_chat"], check_user):
        return
    if check_user and request.user.has_permission(["moderate_chat", "manage_chat"]):
        return
    raise merrors.PermissionDeniedException()
