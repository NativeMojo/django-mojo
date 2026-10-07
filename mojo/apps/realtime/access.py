"""Per-connection memory of topic delivery decisions.

Chat delivery used to re-run the room access check (a user row, the room, the
membership, and for group rooms the group permission) for every frame on every
socket. A socket now remembers an allow decision once it has been made with a
fresh check, and serves later frames from memory until one of these happens:

- the entry is older than ``WS_SUBSCRIPTION_RECHECK_SECONDS`` (default 300).
  The next frame re-runs the check. ``0`` or less turns the memory off, so
  every frame checks, which is how chat delivery behaved before.
- a frame in ``ACCESS_CHANGE_FRAMES`` names this socket's user, or deletes the
  room. That frame itself is re-checked before it is delivered.
- the socket unsubscribes from the topic.

Only allow decisions are remembered. A denial unsubscribes the topic, so
there is nothing left to deliver. An access-change frame can only force a
re-check, never grant access, so a forged one costs a query and nothing more.

The expiry is the only bound on changes that publish no frame: a staff or
group permission removed, an account deactivated outside the disable service
(which force-disconnects), a group deleted with its rooms.
"""

import time

from mojo.helpers.settings import settings

DEFAULT_RECHECK_SECONDS = 300

# Frames published on a chat topic after a write that can take a user's
# access to the room away. See mojo/apps/chat/services/access.py.
ACCESS_CHANGE_FRAMES = frozenset({
    "chat_member_left",
    "chat_member_removed",
    "chat_member_banned",
    "chat_room_deleted",
})


def is_chat_topic(topic):
    return isinstance(topic, str) and topic.startswith("chat:")


def recheck_seconds():
    """The lifetime of a remembered decision.

    Read from static settings only: this bounds how long a revoked user can
    keep reading, so a database Setting row must not be able to stretch it.
    """
    return settings.get_static(
        "WS_SUBSCRIPTION_RECHECK_SECONDS", DEFAULT_RECHECK_SECONDS, kind="int")


def changes_access(payload, user):
    """Whether a frame's payload announces an access change for ``user``."""
    if not isinstance(payload, dict):
        return False
    kind = payload.get("type")
    if kind not in ACCESS_CHANGE_FRAMES:
        return False
    if kind == "chat_room_deleted":
        return True
    target = payload.get("user_id")
    if target is None:
        # A member frame that names nobody: re-check rather than guess.
        return True
    return str(target) == str(getattr(user, "pk", None))


class TopicAccess:
    """Topic -> expiry of the last allow decision, for one connection.

    ``clock`` is a test seam; production uses ``time.monotonic``.
    """

    def __init__(self, clock=None):
        self._clock = clock or time.monotonic
        self._expires = {}

    def allows(self, topic):
        expires = self._expires.get(topic)
        if expires is None:
            return False
        if self._clock() < expires:
            return True
        del self._expires[topic]
        return False

    def allow(self, topic, ttl=None):
        if ttl is None:
            ttl = recheck_seconds()
        if ttl <= 0:
            self._expires.pop(topic, None)
            return
        self._expires[topic] = self._clock() + ttl

    def forget(self, topic):
        self._expires.pop(topic, None)
