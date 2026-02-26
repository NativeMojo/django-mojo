"""
MFA token service.

A short-lived Redis token that bridges the first factor (password/SMS send)
and the second factor (TOTP code / SMS code verify).

Token shape in Redis:
    key:   mfa:<token>
    value: JSON { "uid": <user_id>, "methods": ["totp", "sms"] }
    TTL:   MFA_TOKEN_TTL seconds (default 300 / 5 minutes)
"""
import json
import uuid

from mojo.helpers.redis import get_connection
from mojo.helpers.settings import settings

MFA_TOKEN_TTL = settings.get("MFA_TOKEN_TTL", 300)
_KEY_PREFIX = "mfa:"


def create_mfa_token(user, methods):
    """
    Issue a short-lived MFA token for user.

    Args:
        user:    User instance
        methods: list of available second factors, e.g. ["totp", "sms"]

    Returns:
        token string
    """
    token = uuid.uuid4().hex
    data = json.dumps({"uid": user.id, "methods": methods})
    get_connection().setex(f"{_KEY_PREFIX}{token}", MFA_TOKEN_TTL, data)
    return token


def consume_mfa_token(token):
    """
    Validate and consume (delete) an MFA token.

    Returns the stored data dict on success, None if invalid/expired.
    Single-use — deleted immediately on retrieval.
    """
    r = get_connection()
    key = f"{_KEY_PREFIX}{token}"
    raw = r.get(key)
    if not raw:
        return None
    r.delete(key)
    return json.loads(raw)
