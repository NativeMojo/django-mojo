"""
Deletion notifications for chat messages.

A consumer that keeps rows pointing at a chat message (a board comment, a
notification, an index entry) needs to know when those messages disappear.
Configure `CHAT_MESSAGE_DELETED_HANDLER` with a dotted path to a callable:

    handler(room_id, message_ids)

Coverage is honest and bounded: the hook fires for TTL cleanup
(`chat.cleanup.run_cleanup`) and for REST room deletion
(`ChatRoom.on_rest_pre_delete`). It does NOT fire for a raw ORM cascade --
`ChatRoom.group` is `on_delete=CASCADE` from `account.Group`, so deleting a
Group destroys rooms and their messages with no REST layer involved.

A broken consumer hook must never block a deletion, so every handler call is
wrapped and its exception logged rather than propagated.
"""
from mojo.helpers import logit, modules
from mojo.helpers.settings import settings

logger = logit.get_logger("chat", "chat.log")

# Sentinel for the keyword-only test seam.
_UNSET = object()

# Ids per handler call. A room can hold far more messages than a consumer
# wants in one payload.
_CHUNK = 1000


def _resolve_handler(handler):
    """Return the configured callable, or None when no handler is set.

    `handler` is a keyword-only test seam with a sentinel default: production
    callers pass nothing and the value comes from the
    `CHAT_MESSAGE_DELETED_HANDLER` setting. Passing None explicitly means
    "no handler".
    """
    if handler is not _UNSET:
        return handler if callable(handler) else None

    configured = settings.get("CHAT_MESSAGE_DELETED_HANDLER", None)
    if not configured:
        return None
    if callable(configured):
        return configured
    try:
        # Catch Exception, not ImportError: an exception raised inside the
        # target module at import time propagates unwrapped.
        func = modules.load_function(str(configured))
    except Exception as e:
        logger.exception(
            f"chat: could not load CHAT_MESSAGE_DELETED_HANDLER "
            f"'{configured}': {e}")
        return None
    if not callable(func):
        logger.error(
            f"chat: CHAT_MESSAGE_DELETED_HANDLER '{configured}' is not callable")
        return None
    return func


def notify_deleted(room_id, message_ids, *, handler=_UNSET):
    """Tell the configured handler that these message ids are gone.

    Returns the number of ids successfully handed to the handler.
    """
    func = _resolve_handler(handler)
    if func is None:
        return 0

    ids = list(message_ids or [])
    if not ids:
        return 0

    notified = 0
    for start in range(0, len(ids), _CHUNK):
        chunk = ids[start:start + _CHUNK]
        try:
            func(room_id, chunk)
        except Exception as e:
            logger.exception(
                f"chat: deletion handler failed for room {room_id}: {e}")
            continue
        notified += len(chunk)
    return notified


def notify_room_deleted(room, *, handler=_UNSET):
    """Notify for every message in a room that is about to be destroyed.

    Called from `ChatRoom.on_rest_pre_delete`, before the cascade runs, so the
    ids are still readable.
    """
    func = _resolve_handler(handler)
    if func is None:
        return 0

    from ..models import ChatMessage

    try:
        ids = list(
            ChatMessage.objects.filter(room=room).values_list("pk", flat=True))
    except Exception as e:
        logger.exception(
            f"chat: could not read message ids for room "
            f"{getattr(room, 'pk', None)}: {e}")
        return 0

    return notify_deleted(getattr(room, "pk", None), ids, handler=func)
