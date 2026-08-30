"""
WebSocket message handler for chat.

Handles all chat-related message types routed from the User model's
on_realtime_message hook. Each message type is dispatched to a handler function.

Message types:
  - chat_message: Send a new message to a room
  - chat_edit: Edit an existing message
  - chat_flag: Flag a message for moderation
  - chat_react: Add, remove or toggle an emoji reaction
  - chat_typing: Broadcast typing indicator (ephemeral)
  - chat_read: Mark messages as read
"""
import re

from django.db import IntegrityError

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

CLIENT_KEY_MAX = 64
_CLIENT_KEY_RE = re.compile(r"[A-Za-z0-9._:-]{1,64}")
CLIENT_KEY_ERROR = (
    "client_key must be 1-64 characters using letters, digits, '.', '_', ':' or '-'")


def _client_key(value):
    """Return (key_or_None, error_or_None). Absent/blank means no key."""
    if value is None:
        return None, None
    if not isinstance(value, str):
        return None, CLIENT_KEY_ERROR
    if not value.strip():
        # Blank (or whitespace-only) is "no key", not a bad key.
        return None, None
    # Validate the value as sent -- do not normalize whitespace away, or
    # " abc" and "abc" would silently become the same idempotency key.
    # fullmatch, never re.match with $ -- "$" also matches just before a
    # trailing newline, which would let "abc\n" through to be stored and
    # broadcast verbatim.
    if not _CLIENT_KEY_RE.fullmatch(value):
        return None, CLIENT_KEY_ERROR
    return value, None


def _send_error(client_key, message, **extra):
    """Error frame for a send, echoing the client_key when we have a valid one."""
    out = {"type": "error", "error": message}
    if client_key:
        out["client_key"] = client_key
    out.update(extra)
    return out


def _send_ack(msg, room, client_key):
    """Ack frame for a send. The single builder for every ack path."""
    out = {
        "type": "chat_message_ack",
        "message_id": msg.pk,
        "room_id": room.pk,
        "kind": msg.kind,
        "metadata": msg.metadata,
        "created": msg.created.isoformat(),
    }
    if client_key:
        out["client_key"] = client_key
    return out


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
        error = {"type": "error", "error": "Chat message processing error"}
        if message_type == "chat_message":
            # Best-effort: let the client correlate the crash with its send.
            key, key_error = _client_key(data.get("client_key"))
            if key and not key_error:
                error["client_key"] = key
        return error


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


def _handle_send(user, data, *, publisher=None):
    """Handle sending a new chat message.

    An optional `client_key` makes the send idempotent for the
    (room, user) pair: a retry with the same key returns the ack for the
    message already stored instead of creating a second one.

    Everything from validation onward is delegated to
    `services.messages.send_message`, the one creation path; this function owns
    the WebSocket frame shape and the client_key idempotency contract.

    `publisher` is a test seam for the room broadcast; production callers
    leave it unset and the realtime publisher is used.
    """
    from .models import ChatMessage
    from .services.messages import send_message

    client_key, key_error = _client_key(data.get("client_key"))
    if key_error:
        # Never echo the offending value back.
        return {"type": "error", "error": key_error}

    room_id = data.get("room_id")
    body = (data.get("body") or "").strip()
    kind = data.get("kind", "text")
    metadata = data.get("metadata")

    if not room_id:
        return _send_error(client_key, "room_id is required")

    membership, room, error = _get_membership(user, room_id)
    if error:
        return _send_error(client_key, error["error"])

    # Dedupe before rate limiting and can_send: a retry of a message we
    # already stored must not be punished for the retry.
    if client_key:
        existing = ChatMessage.objects.filter(
            room=room, user=user, client_key=client_key).first()
        if existing:
            if existing.body != body or existing.kind != kind:
                return _send_error(
                    client_key, "client_key is already bound to a different message")
            return _send_ack(existing, room, client_key)

    if not membership.can_send:
        return _send_error(
            client_key, f"Cannot send messages (status: {membership.status})")

    # Validation, room policy, persistence and the broadcast all live in the
    # send service. `client_authored=True` is what keeps a WebSocket client off
    # the server-only kinds.
    try:
        msg, error = send_message(
            room, user, body, kind=kind, metadata=metadata,
            client_authored=True,
            client_key=client_key,
            publisher=publisher,
            broadcast_extra={"client_key": client_key} if client_key else None,
        )
    except IntegrityError:
        if not client_key:
            raise
        # Either a concurrent send won the unique constraint, or the row
        # could not be written at all (the room was deleted mid-send and
        # the FK failed). Re-read decides which; either way we return here
        # and never broadcast a second frame for the same message.
        existing = ChatMessage.objects.filter(
            room=room, user=user, client_key=client_key).first()
        if existing is None:
            return _send_error(client_key, "Could not persist the message")
        if existing.body != body or existing.kind != kind:
            return _send_error(
                client_key, "client_key is already bound to a different message")
        return _send_ack(existing, room, client_key)

    if error:
        if client_key:
            error["client_key"] = client_key
        return error

    return _send_ack(msg, room, client_key)


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


def _handle_react(user, data, *, publisher=None):
    """Handle adding/removing an emoji reaction.

    `action` is optional. Absent, the call toggles exactly as it always did.
    `"add"` and `"remove"` are explicit and idempotent, so a retried frame
    cannot invert the reaction. Anything else is refused.

    The request verbs are `add`/`remove`; the ack and the `chat_reaction` event
    keep reporting `added`/`removed`. That mismatch is a shipped contract, not
    an oversight -- do not "fix" it.

    `publisher` is a test seam for the room broadcast; production callers leave
    it unset and the realtime publisher is used.
    """
    from .models import ChatMessage, ChatReaction, ChatMembership
    from .services.messages import join_bounded_messages
    from mojo.apps.realtime import publish_topic

    message_id = data.get("message_id")
    emoji = (data.get("emoji") or "").strip()
    action = data.get("action")

    if not message_id:
        return {"type": "error", "error": "message_id is required"}
    if not emoji:
        return {"type": "error", "error": "emoji is required"}
    if len(emoji) > 8:
        return {"type": "error", "error": "Invalid emoji"}
    if action is not None and action not in ("add", "remove"):
        return {"type": "error", "error": "Invalid action"}

    # ONE error for every unreachable target: nonexistent, flagged, before the
    # caller joined, or in a room they are not in. The frame carries no
    # room_id, so a distinguishable "Not a member of this room" would make this
    # a GLOBAL, cross-tenant message-existence oracle for any authenticated
    # user. Keep these three returns identical.
    not_found = {"type": "error", "error": "Message not found"}

    msg = ChatMessage.objects.filter(pk=message_id).select_related("room").first()
    if not msg:
        return not_found

    membership = ChatMembership.objects.filter(room=msg.room, user=user).first()
    if not membership or membership.status == "banned":
        return not_found

    # Bound the target exactly as history is bounded, minus the TTL: a member
    # may react to anything they were entitled to see.
    if not join_bounded_messages(msg.room, membership).filter(pk=msg.pk).exists():
        return not_found

    if action == "add":
        # unique_together on (message, user, emoji) backs this: a concurrent
        # duplicate re-gets instead of raising.
        _, changed = ChatReaction.objects.get_or_create(
            message=msg, user=user, emoji=emoji,
        )
        result = "added"
    elif action == "remove":
        deleted, _ = ChatReaction.objects.filter(
            message=msg, user=user, emoji=emoji,
        ).delete()
        changed = bool(deleted)
        result = "removed"
    else:
        # Legacy toggle -- unchanged for clients that send no action.
        existing = ChatReaction.objects.filter(
            message=msg, user=user, emoji=emoji,
        ).first()
        if existing:
            existing.delete()
            result = "removed"
        else:
            ChatReaction.objects.create(message=msg, user=user, emoji=emoji)
            result = "added"
        changed = True

    # Publish ONLY on a real state change. chat_react is not rate limited
    # (check_rate_limit lives inside _handle_send), so broadcasting on every
    # call would let one member loop `add` and fan 1 inbound frame out to every
    # subscriber in the room. The idempotent ack below is the useful half.
    if changed:
        publish = publisher or publish_topic
        publish(msg.room.topic, {
            "type": "chat_reaction",
            "message_id": msg.pk,
            "room_id": msg.room_id,
            "user_id": user.pk,
            "emoji": emoji,
            "action": result,
        })

    return {
        "type": "chat_react_ack",
        "message_id": msg.pk,
        "emoji": emoji,
        "action": result,
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


def _handle_read(user, data, *, publisher=None):
    """Handle marking messages as read.

    Everything from the target clamp onward lives in
    `services.read_state.mark_read`, which `POST /api/chat/room/read` shares --
    the two used to carry duplicate copies of this block and could drift.

    `publisher` is a test seam for the room broadcast; production callers leave
    it unset and the realtime publisher is used.
    """
    from .models import ChatRoom, ChatMembership
    from .services.read_state import mark_read

    room_id = data.get("room_id")
    up_to_message_id = data.get("up_to_message_id")

    if not room_id or not up_to_message_id:
        return {"type": "error", "error": "room_id and up_to_message_id are required"}

    room = ChatRoom.objects.filter(pk=room_id).first()
    if not room:
        return {"type": "error", "error": "Room not found"}

    # Active + muted, the same predicate history and `/unread` use. A muted
    # member still reads; only a banned one is shut out. Letting a banned
    # member stamp receipts would write state they have no standing to write,
    # and a muted member who could NOT mark read would show as permanently
    # unread to everyone else in the room.
    membership = ChatMembership.objects.filter(
        room=room, user=user, status__in=["active", "muted"],
    ).first()
    if not membership:
        return {"type": "error", "error": "Not a member of this room"}

    target = mark_read(room, user, membership, up_to_message_id)

    # Publish the RESOLVED id, and only when something resolved -- the sender's
    # read indicator must not be whatever integer the reader sent.
    if target is not None and room.kind in ("direct", "group"):
        from mojo.apps.realtime import publish_topic
        publish = publisher or publish_topic
        publish(room.topic, {
            "type": "chat_read",
            "room_id": room.pk,
            "user_id": user.pk,
            "up_to_message_id": target.pk,
        })

    return {
        "type": "chat_read_ack",
        "room_id": room.pk,
        "up_to_message_id": target.pk if target else None,
    }
