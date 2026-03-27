"""
Room rules enforcement for chat messages.

Checks per-room content policies (URLs, phone numbers, media, length)
and runs content_guard moderation. Returns (decision, errors) tuple.
"""
import time
from mojo.helpers.redis.client import get_connection


def check_rules(room, body, kind="text"):
    """
    Enforce room rules on a message body.

    Returns list of error strings. Empty list means all rules pass.
    """
    errors = []

    max_len = room.get_rule("max_message_length", 4000)
    if len(body) > max_len:
        errors.append(f"Message exceeds max length of {max_len}")

    if not room.get_rule("allow_media", True) and kind == "image":
        errors.append("Media messages are not allowed in this room")

    if not room.get_rule("allow_urls", True) or not room.get_rule("allow_phone_numbers", True):
        from mojo.helpers import content_guard
        result = content_guard.check_text(body, surface="chat")
        if not room.get_rule("allow_urls", True):
            for match in result.matches:
                if match.type in ("spam_link", "url"):
                    errors.append("URLs are not allowed in this room")
                    break
        if not room.get_rule("allow_phone_numbers", True):
            for match in result.matches:
                if match.type in ("spam_phone", "phone"):
                    errors.append("Phone numbers are not allowed in this room")
                    break

    return errors


def check_moderation(body):
    """
    Run content_guard moderation on message body.

    Returns (decision, reasons) where decision is "allow", "warn", or "block".
    """
    from mojo.helpers import content_guard
    result = content_guard.check_text(body, surface="chat")
    return result.decision, result.reasons


def check_rate_limit(room, user):
    """
    Check if user has exceeded the room's rate limit.

    Uses Redis sliding window counter. Returns True if allowed, False if rate limited.
    """
    limit = room.get_rule("rate_limit", 10)
    if not limit:
        return True

    redis = get_connection()
    key = f"chat:rate:{room.pk}:{user.pk}"
    now = time.time()
    window_start = now - 1.0

    pipe = redis.pipeline()
    pipe.zremrangebyscore(key, 0, window_start)
    pipe.zadd(key, {str(now): now})
    pipe.zcard(key)
    pipe.expire(key, 5)
    results = pipe.execute()

    count = results[2]
    return count <= limit
