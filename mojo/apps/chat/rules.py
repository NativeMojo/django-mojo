"""
Room rules enforcement for chat messages.

Checks per-room content policies (URLs, phone numbers, media, length)
and provides advisory content_guard moderation adapters.
"""
import re
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


HIDE_LEVEL_KEY = "CHAT_MODERATION_HIDE_LEVEL"
ALLOWED_DOMAINS_KEY = "CHAT_MODERATION_ALLOWED_DOMAINS"
DEFAULT_HIDE_LEVEL = 70
HIDE_LEVEL_NEVER = 101  # only slurs are hidden
MAX_ALLOWED_DOMAINS = 200
_DOMAIN_RE = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def validate_hide_level(key, parsed):
    """Setting validator: a JSON integer 1..101 (101 hides only slurs)."""
    if isinstance(parsed, bool) or not isinstance(parsed, int) or not 1 <= parsed <= HIDE_LEVEL_NEVER:
        raise ValueError(f"{key} must be a JSON integer from 1 to {HIDE_LEVEL_NEVER}")


def validate_allowed_domains(key, parsed):
    """Setting validator: a JSON list of lowercase hostnames (no scheme, path, port or wildcard)."""
    if not isinstance(parsed, list) or len(parsed) > MAX_ALLOWED_DOMAINS:
        raise ValueError(f"{key} must be a JSON list of at most {MAX_ALLOWED_DOMAINS} hostnames")
    for domain in parsed:
        if not isinstance(domain, str) or not _DOMAIN_RE.match(domain):
            raise ValueError(
                f"{key} entries must be lowercase hostnames such as example.com, got {domain!r}")


def check_moderation_scored(body, *, group=None):
    """Return advisory (decision, reasons, score) for a chat body.

    Preserve classifier scores/reasons, including high_severity. The decision
    is "masked" when the score reaches the hide level or a slur matched
    (reason high_severity), else "warn" at the classifier's warn threshold,
    else "allow". The hide level (CHAT_MODERATION_HIDE_LEVEL) and link
    allowlist (CHAT_MODERATION_ALLOWED_DOMAINS) are live settings resolved
    for `group` (its row, then its parents', then the global row); with no
    group the global values apply. Consumers decide what to hide, and may
    reveal the body.
    """
    from mojo.helpers.settings import settings
    if not settings.get("CHAT_MODERATION_ENABLED", True, kind="bool"):
        return "allow", [], None

    hide_level = settings.get(HIDE_LEVEL_KEY, DEFAULT_HIDE_LEVEL, group=group, kind="int")
    domains = settings.get(ALLOWED_DOMAINS_KEY, [], group=group, kind="list")

    from mojo.helpers import content_guard
    result = content_guard.check_text(
        body, surface="chat", policy={"link_allow_domains": domains})
    if "high_severity" in result.reasons or result.score >= hide_level:
        decision = "masked"
    elif result.score >= content_guard.DEFAULT_POLICY["text_warn_threshold"]:
        decision = "warn"
    else:
        decision = "allow"
    return decision, list(result.reasons), result.score


def check_moderation(body, *, group=None):
    """Compatibility two-tuple: advisory (decision, reasons)."""
    decision, reasons, score = check_moderation_scored(body, group=group)
    return decision, reasons


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
