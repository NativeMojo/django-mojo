"""
Cross-origin auth handoff token service.

A short-lived, single-use Redis token that lets an authenticated user on the
auth origin hand a JWT to a different-origin app, without putting the JWT in
the URL.

Token shape in Redis:
    key:   auth:handoff:<code>
    value: JSON { "uid": <user_id>, "ip": <issuing_ip>, "dest": <destination>,
                  "gid": <group_id or absent> }
    TTL:   AUTH_HANDOFF_CODE_TTL seconds (default 60)

The destination is validated at ISSUANCE — see
`mojo.apps.account.services.redirect_allowlist`. What is stored here is an
audit record of where the code was minted for, never a second gate.
"""
import json
import uuid

from mojo.helpers.redis import get_connection
from mojo.helpers.settings import settings

_KEY_PREFIX = "auth:handoff:"


def get_ttl():
    """Return the configured handoff code TTL in seconds."""
    return settings.get("AUTH_HANDOFF_CODE_TTL", 60, kind="int")


def create_handoff_code(user, destination=None, ip=None, group_id=None):
    """
    Issue a short-lived handoff code for a fully authenticated user.

    Args:
        user:        User instance (must already have completed primary auth + any MFA).
        destination: The already-validated destination URL this code was minted
                     for. Recorded for audit only.
        ip:          Optional issuing IP for audit only — not enforced on consume.
        group_id:    Confine this code's delivery to one group — it exchanges
                     into a GroupScopedToken package instead of a JWT pair.
                     UNLIKE `destination` and `ip`, this IS enforced on consume.

    `gid` is the ONE encoding of the gating decision, and it is decided HERE,
    at issuance, from the server-validated destination — never re-derived at
    exchange. Re-resolving would let a resolver that breaks inside the code's
    TTL turn a gated code back into a platform JWT. A code minted before a mode
    flip is therefore honored under the decision that was taken when it was
    minted, in both directions, for at most AUTH_HANDOFF_CODE_TTL seconds. A
    code minted before this feature existed simply has no `gid` key.

    Returns:
        code string (32 hex chars).

    NEITHER `destination` NOR `ip` IS ENFORCED ON CONSUME, and deliberately so.
    `POST /api/auth/exchange` is called by the consuming app's own backend, which
    chooses its own source IP and its own headers; an attacker holding the code
    holds those too, so a consume-time comparison would reject honest callers
    behind a different egress while stopping nobody. The gate that matters runs
    before this function is ever reached — the caller must have checked the
    destination with `redirect_allowlist.is_allowed_destination()`. What is
    stored here answers "where was this code sent?" after the fact.
    """
    code = uuid.uuid4().hex
    payload = {"uid": user.id, "ip": ip or "", "dest": destination or ""}
    if group_id:
        payload["gid"] = int(group_id)
    data = json.dumps(payload)
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
