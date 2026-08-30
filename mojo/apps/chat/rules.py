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


def _payload_strings(value, out):
    """Collect every string key and string value inside a payload."""
    if isinstance(value, str):
        out.append(value)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _payload_strings(item, out)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                out.append(key)
            _payload_strings(item, out)


def check_payload_rules(room, metadata):
    """
    Apply the room's URL/phone rules to string values inside a message payload.

    `allow_urls=False` is a binary policy a room owner set, and check_rules
    reads `body` only -- so without this a card could carry
    {"link": "https://evil.tld/lure"} and defeat it outright.

    The moderation classifier is deliberately NOT applied here: running a
    heuristic over ids and slugs produces false positives with no recourse,
    and `body` is the human-visible moderated surface.

    Expects an already-validated, already-capped payload. Returns a list of
    error strings; empty means the payload passes.
    """
    allow_urls = room.get_rule("allow_urls", True)
    allow_phones = room.get_rule("allow_phone_numbers", True)
    if allow_urls and allow_phones:
        return []

    if not metadata:
        return []

    values = []
    _payload_strings(metadata, values)
    if not values:
        return []

    from mojo.helpers import content_guard
    result = content_guard.check_text("\n".join(values), surface="chat")

    errors = []
    if not allow_urls:
        for match in result.matches:
            if match.type in ("spam_link", "url"):
                errors.append("URLs are not allowed in this room")
                break
    if not allow_phones:
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
