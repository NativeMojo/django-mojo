"""
Cross-origin auth handoff token service.

A short-lived, single-use Redis token that lets an authenticated user on the
auth origin hand a JWT to a different-origin app, without putting the JWT in
the URL.

Token shape in Redis:
    key:   auth:handoff:<code>
    value: JSON { "uid": <user_id>, "ip": <issuing_ip> }
    TTL:   AUTH_HANDOFF_CODE_TTL seconds (default 60)
"""
import json
import uuid

from mojo.helpers.redis import get_connection
from mojo.helpers.settings import settings

_KEY_PREFIX = "auth:handoff:"


def get_ttl():
    """Return the configured handoff code TTL in seconds."""
    return settings.get("AUTH_HANDOFF_CODE_TTL", 60, kind="int")


def create_handoff_code(user, ip=None):
    """
    Issue a short-lived handoff code for a fully authenticated user.

    Args:
        user: User instance (must already have completed primary auth + any MFA).
        ip:   Optional issuing IP for audit only — not enforced on consume.

    Returns:
        code string (32 hex chars).
    """
    code = uuid.uuid4().hex
    data = json.dumps({"uid": user.id, "ip": ip or ""})
    get_connection().setex(f"{_KEY_PREFIX}{code}", get_ttl(), data)
    return code


def consume_handoff_code(code):
    """
    Validate and consume (delete) a handoff code.

    Returns the stored data dict on success, None if invalid/expired.
    Single-use — atomic GETDEL guarantees only one concurrent caller wins.
    """
    if not code or not isinstance(code, str) or len(code) != 32 or not code.isalnum():
        return None
    raw = get_connection().getdel(f"{_KEY_PREFIX}{code}")
    if not raw:
        return None
    return json.loads(raw)
