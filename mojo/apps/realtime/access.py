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
- an ``access_changed`` announcement arrives on the identity's access channel
  (``publish_access_changed`` in manager.py). Every remembered decision is
  forgotten, so the next frame on each chat topic is re-checked. The account
  models publish one when a save takes a permission away or deactivates the
  user or one of their group members.
- the socket unsubscribes from the topic.

Only allow decisions are remembered. A denial unsubscribes the topic, so
there is nothing left to deliver. An access-change frame can only force a
re-check, never grant access, so a forged one costs a query and nothing more.

The expiry is the only bound on changes that publish nothing: rows changed
with a bulk ``update()`` or raw SQL, membership rows changed outside the chat
endpoints, a group deactivated or deleted with its rooms. It also bounds an
announcement that Redis lost.
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

    ``clock`` and ``recheck`` are test seams; production uses
    ``time.monotonic`` and ``recheck_seconds``.
    """

    def __init__(self, clock=None, recheck=None):
        self._clock = clock or time.monotonic
        self._recheck = recheck or recheck_seconds
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
            ttl = self._recheck()
        if ttl <= 0:
            self._expires.pop(topic, None)
            return
        self._expires[topic] = self._clock() + ttl

    def forget(self, topic):
        self._expires.pop(topic, None)

    def forget_all(self):
        self._expires.clear()


ACCESS_FIELDS = ("permissions", "is_active", "is_superuser")


def access_snapshot(instance, before=None, fields=None):
    """What an account or member row grants, as far as this instance has
    loaded it. A deferred field is left out: nothing is known about it.

    ``fields`` limits the answer to the fields a partial save or refresh just
    brought in line with the database; the others keep what ``before``, the
    earlier snapshot, last saw stored.
    """
    state = instance.__dict__
    seen = {}
    if "permissions" in state:
        perms = state["permissions"]
        perms = perms if isinstance(perms, dict) else {}
        seen["permissions"] = frozenset(key for key, value in perms.items() if value)
    for name in ACCESS_FIELDS[1:]:
        if name in state:
            seen[name] = bool(state[name])
    if fields is None:
        return seen
    kept = dict(before or {})
    for name in ACCESS_FIELDS:
        if name in fields:
            kept.pop(name, None)
            if name in seen:
                kept[name] = seen[name]
    return kept


def save_removes_access(before, instance, update_fields=None):
    """Whether saving ``instance`` can take access away from its user.

    ``before`` is the ``access_snapshot`` taken when the row was read, or
    None. Granting is never an answer of True. A value this instance never
    read counts as removed when it is written without one: when unsure, the
    sockets re-check.
    """
    before = before or {}
    written = None if update_fields is None else set(update_fields)
    for name, value in access_snapshot(instance).items():
        if written is not None and name not in written:
            continue
        if name == "permissions":
            if name not in before or before[name] - value:
                return True
        elif not value and before.get(name, True):
            return True
    return False
