"""
WebSocket message handler for chat.

Handles all chat-related message types routed from the User model's
on_realtime_message hook. Each message type is dispatched to a handler function.

Message types:
  - chat_message: Send a new message to a room
  - chat_edit: Edit an existing message
  - chat_flag: Flag a message for moderation
  - chat_react: Toggle an emoji reaction
  - chat_typing: Broadcast typing indicator (ephemeral)
  - chat_read: Mark messages as read
"""
from mojo.helpers import logit, dates

logger = logit.get_logger("chat", "chat.log")

CHAT_MESSAGE_TYPES = {
    "chat_message",
    "chat_edit",
    "chat_flag",
    "chat_react",
    "chat_typing",
    "chat_read",
}


def handle_chat_message(user, data):
    """
    Main entry point called from User.on_realtime_message.

    Routes to the appropriate handler based on message type.
    Returns a dict response to send back to the client, or None.
    """
    message_type = data.get("type") or data.get("action")

    handlers = {
        "chat_message": _handle_send,
        "chat_edit": _handle_edit,
        "chat_flag": _handle_flag,
        "chat_react": _handle_react,
        "chat_typing": _handle_typing,
        "chat_read": _handle_read,
    }

    handler = handlers.get(message_type)
    if not handler:
        return {"type": "error", "error": f"Unknown chat message type: {message_type}"}

    try:
        return handler(user, data)
    except Exception as e:
        logger.error(f"Chat handler error: {e}", exc_info=True)
        return {"type": "error", "error": "Chat message processing error"}


def _get_membership(user, room_id):
    """Get active membership for user in room. Returns (membership, error_response)."""
    from .models import ChatMembership, ChatRoom

    room = ChatRoom.objects.filter(pk=room_id).first()
    if not room:
        return None, None, {"type": "error", "error": "Room not found"}

    membership = ChatMembership.objects.filter(room=room, user=user).first()
    if not membership:
        return None, room, {"type": "error", "error": "Not a member of this room"}

    return membership, room, None


def _handle_send(user, data):
    """Handle sending a new chat message."""
    from .models import ChatMessage
    from .rules import check_rules, check_moderation, check_rate_limit
    from mojo.apps.realtime import publish_topic

    room_id = data.get("room_id")
    body = (data.get("body") or "").strip()
    kind = data.get("kind", "text")

    if not room_id:
        return {"type": "error", "error": "room_id is required"}
    if not body and kind == "text":
        return {"type": "error", "error": "body is required"}

    membership, room, error = _get_membership(user, room_id)
    if error:
        return error

    if not membership.can_send:
        return {"type": "error", "error": f"Cannot send messages (status: {membership.status})"}

    # Rate limit
    if not check_rate_limit(room, user):
        return {"type": "error", "error": "Rate limit exceeded"}

    # Room rules
    rule_errors = check_rules(room, body, kind)
    if rule_errors:
        return {"type": "error", "error": rule_errors[0]}

    # Content moderation
    decision, reasons = check_moderation(body)
    if decision == "block":
        return {"type": "error", "error": "Message blocked by moderation", "reasons": reasons}

    # Persist
    msg = ChatMessage.objects.create(
        room=room,
        user=user,
        body=body,
        kind=kind,
        moderation_decision=decision,
    )

    # Publish to room topic
    msg_data = {
        "type": "chat_message",
        "message_id": msg.pk,
        "room_id": room.pk,
        "user_id": user.pk,
        "body": body,
        "kind": kind,
        "created": msg.created.isoformat(),
    }
    if decision == "warn":
        msg_data["moderation_decision"] = "warn"

    publish_topic(room.topic, msg_data)

    # Update room modified timestamp
    room.save(update_fields=["modified"])

    return {
        "type": "chat_message_ack",
        "message_id": msg.pk,
        "room_id": room.pk,
        "created": msg.created.isoformat(),
    }


def _handle_edit(user, data):
    """Handle editing an existing message."""
    from .models import ChatMessage, ChatMembership
    from .rules import check_rules, check_moderation
    from mojo.apps.realtime import publish_topic

    message_id = data.get("message_id")
    body = (data.get("body") or "").strip()

    if not message_id:
        return {"type": "error", "error": "message_id is required"}
    if not body:
        return {"type": "error", "error": "body is required"}

    msg = ChatMessage.objects.filter(pk=message_id).select_related("room").first()
    if not msg:
        return {"type": "error", "error": "Message not found"}

    # Check permission: author or room admin/moderator
    is_author = msg.user_id == user.pk
    if not is_author:
        membership = ChatMembership.objects.filter(room=msg.room, user=user).first()
        if not membership or not membership.is_admin:
            return {"type": "error", "error": "Cannot edit this message"}

    # Room rules
    rule_errors = check_rules(msg.room, body, msg.kind)
    if rule_errors:
        return {"type": "error", "error": rule_errors[0]}

    # Content moderation
    decision, reasons = check_moderation(body)
    if decision == "block":
        return {"type": "error", "error": "Edited message blocked by moderation", "reasons": reasons}

    # Update
    msg.body = body
    msg.edited_at = dates.utcnow()
    msg.moderation_decision = decision
    msg.save(update_fields=["body", "edited_at", "moderation_decision"])

    # Publish edit event
    publish_topic(msg.room.topic, {
        "type": "chat_message_edited",
        "message_id": msg.pk,
        "room_id": msg.room_id,
        "user_id": user.pk,
        "body": body,
        "edited_at": msg.edited_at.isoformat(),
    })

    return {
        "type": "chat_edit_ack",
        "message_id": msg.pk,
        "edited_at": msg.edited_at.isoformat(),
    }


def _handle_flag(user, data):
    """Handle flagging a message (moderator action)."""
    from .models import ChatMessage, ChatMembership
    from mojo.apps.realtime import publish_topic

    message_id = data.get("message_id")
    if not message_id:
        return {"type": "error", "error": "message_id is required"}

    msg = ChatMessage.objects.filter(pk=message_id).select_related("room").first()
    if not msg:
        return {"type": "error", "error": "Message not found"}

    # Check permission: room admin or moderate_chat group permission
    membership = ChatMembership.objects.filter(room=msg.room, user=user).first()
    has_perm = False
    if membership and membership.is_admin:
        has_perm = True
    if msg.room.group and msg.room.group.user_has_permission(user, "moderate_chat"):
        has_perm = True
    if user.has_permission("manage_chat"):
        has_perm = True

    if not has_perm:
        return {"type": "error", "error": "Permission denied"}

    msg.is_flagged = True
    msg.flagged_by = user
    msg.flagged_at = dates.utcnow()
    msg.save(update_fields=["is_flagged", "flagged_by", "flagged_at"])

    # Publish flag event so frontends hide the message
    publish_topic(msg.room.topic, {
        "type": "chat_message_flagged",
        "message_id": msg.pk,
        "room_id": msg.room_id,
        "flagged_by": user.pk,
    })

    return {
        "type": "chat_flag_ack",
        "message_id": msg.pk,
    }


def _handle_react(user, data):
    """Handle adding/removing an emoji reaction (toggle)."""
    from .models import ChatMessage, ChatReaction, ChatMembership
    from mojo.apps.realtime import publish_topic

    message_id = data.get("message_id")
    emoji = (data.get("emoji") or "").strip()

    if not message_id:
        return {"type": "error", "error": "message_id is required"}
    if not emoji:
        return {"type": "error", "error": "emoji is required"}
    if len(emoji) > 8:
        return {"type": "error", "error": "Invalid emoji"}

    msg = ChatMessage.objects.filter(pk=message_id).select_related("room").first()
    if not msg:
        return {"type": "error", "error": "Message not found"}

    # Must be a member
    membership = ChatMembership.objects.filter(room=msg.room, user=user).first()
    if not membership or membership.status == "banned":
        return {"type": "error", "error": "Not a member of this room"}

    # Toggle reaction
    existing = ChatReaction.objects.filter(
        message=msg, user=user, emoji=emoji,
    ).first()

    if existing:
        existing.delete()
        action = "removed"
    else:
        ChatReaction.objects.create(message=msg, user=user, emoji=emoji)
        action = "added"

    # Publish reaction event
    publish_topic(msg.room.topic, {
        "type": "chat_reaction",
        "message_id": msg.pk,
        "room_id": msg.room_id,
        "user_id": user.pk,
        "emoji": emoji,
        "action": action,
    })

    return {
        "type": "chat_react_ack",
        "message_id": msg.pk,
        "emoji": emoji,
        "action": action,
    }


def _handle_typing(user, data):
    """Handle typing indicator (ephemeral, no persistence)."""
    from mojo.apps.realtime import publish_topic
    from .models import ChatMembership

    room_id = data.get("room_id")
    if not room_id:
        return {"type": "error", "error": "room_id is required"}

    # Quick membership check
    exists = ChatMembership.objects.filter(
        room_id=room_id, user=user, status="active",
    ).exists()
    if not exists:
        return None

    from .models import ChatRoom
    room = ChatRoom.objects.filter(pk=room_id).first()
    if not room:
        return None

    publish_topic(room.topic, {
        "type": "chat_typing",
        "room_id": room.pk,
        "user_id": user.pk,
    })

    return None  # No ack for typing


def _handle_read(user, data):
    """Handle marking messages as read."""
    from .models import ChatRoom, ChatMessage, ChatMembership, ChatReadReceipt

    room_id = data.get("room_id")
    up_to_message_id = data.get("up_to_message_id")

    if not room_id or not up_to_message_id:
        return {"type": "error", "error": "room_id and up_to_message_id are required"}

    room = ChatRoom.objects.filter(pk=room_id).first()
    if not room:
        return {"type": "error", "error": "Room not found"}

    membership = ChatMembership.objects.filter(room=room, user=user).first()
    if not membership:
        return {"type": "error", "error": "Not a member of this room"}

    if room.kind == "channel":
        # Channels: just update last_read_at on membership
        membership.last_read_at = dates.utcnow()
        membership.save(update_fields=["last_read_at"])
    else:
        # Direct/group: create read receipts for unread messages
        unread_messages = ChatMessage.objects.filter(
            room=room,
            pk__lte=up_to_message_id,
            is_flagged=False,
        ).exclude(
            user=user,  # Don't create receipts for own messages
        ).exclude(
            read_receipts__user=user,  # Skip already-read messages
        ).values_list("pk", flat=True)

        receipts = [
            ChatReadReceipt(message_id=msg_id, user=user)
            for msg_id in unread_messages
        ]
        if receipts:
            ChatReadReceipt.objects.bulk_create(receipts, ignore_conflicts=True)

        # Also update last_read_at for convenience
        membership.last_read_at = dates.utcnow()
        membership.save(update_fields=["last_read_at"])

    # Publish read event for direct/group so sender sees read indicator
    if room.kind in ("direct", "group"):
        from mojo.apps.realtime import publish_topic
        publish_topic(room.topic, {
            "type": "chat_read",
            "room_id": room.pk,
            "user_id": user.pk,
            "up_to_message_id": up_to_message_id,
        })

    return {
        "type": "chat_read_ack",
        "room_id": room.pk,
        "up_to_message_id": up_to_message_id,
    }
