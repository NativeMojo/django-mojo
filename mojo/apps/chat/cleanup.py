"""
Disappearing messages cleanup.

Call run_cleanup() periodically (e.g. from a cron job) to delete
messages that have exceeded their room's disappearing_ttl.
Flagged messages are preserved (evidence).
"""
from mojo.helpers import logit, dates
from datetime import timedelta

from .services.deletion import _UNSET, _CHUNK, notify_deleted

logger = logit.get_logger("chat", "chat.log")


def run_cleanup(*, handler=_UNSET):
    """
    Delete expired messages from rooms with disappearing_ttl > 0.
    Flagged messages are exempt (preserved as evidence).

    Notifies the configured deletion handler with the ids it removed, so a
    consumer holding message references can clean up. `handler` is a
    keyword-only test seam with a sentinel default; production callers pass
    nothing and the `CHAT_MESSAGE_DELETED_HANDLER` setting is used.

    Returns the total number of rows deleted, unchanged.
    """
    from .models import ChatRoom, ChatMessage

    rooms = ChatRoom.objects.filter(rules__disappearing_ttl__gt=0)
    total_deleted = 0

    for room in rooms:
        ttl = room.get_rule("disappearing_ttl", 0)
        if not ttl:
            continue

        cutoff = dates.utcnow() - timedelta(seconds=ttl)
        expiring = ChatMessage.objects.filter(
            room=room,
            created__lt=cutoff,
            is_flagged=False,
        )

        # Read the ids in chunks before deleting -- after the delete they are
        # gone, and a room can hold far more than one payload's worth.
        while True:
            ids = list(expiring.values_list("pk", flat=True)[:_CHUNK])
            if not ids:
                break

            deleted, _ = ChatMessage.objects.filter(pk__in=ids).delete()
            if not deleted:
                break

            total_deleted += deleted
            logger.info(f"Cleaned up {deleted} messages from room {room.pk}")
            notify_deleted(room.pk, ids, handler=handler)

    return total_deleted
