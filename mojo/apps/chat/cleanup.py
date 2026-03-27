"""
Disappearing messages cleanup.

Call run_cleanup() periodically (e.g. from a cron job) to delete
messages that have exceeded their room's disappearing_ttl.
Flagged messages are preserved (evidence).
"""
from mojo.helpers import logit, dates
from datetime import timedelta

logger = logit.get_logger("chat", "chat.log")


def run_cleanup():
    """
    Delete expired messages from rooms with disappearing_ttl > 0.
    Flagged messages are exempt (preserved as evidence).
    """
    from .models import ChatRoom, ChatMessage

    rooms = ChatRoom.objects.filter(rules__disappearing_ttl__gt=0)
    total_deleted = 0

    for room in rooms:
        ttl = room.get_rule("disappearing_ttl", 0)
        if not ttl:
            continue

        cutoff = dates.utcnow() - timedelta(seconds=ttl)
        deleted, _ = ChatMessage.objects.filter(
            room=room,
            created__lt=cutoff,
            is_flagged=False,
        ).delete()

        if deleted:
            total_deleted += deleted
            logger.info(f"Cleaned up {deleted} messages from room {room.pk}")

    return total_deleted
