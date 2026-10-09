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

A check that was already running when one of those arrived must not put its
older answer back: ``allow`` takes the ``epoch`` read before the check began
and remembers nothing when a decision has been forgotten since.

Only allow decisions are remembered. A denial unsubscribes the topic, so
there is nothing left to deliver. An access-change frame can only force a
re-check, never grant access, so a forged one costs a query and nothing more.

The expiry is the only bound on changes that publish nothing: rows changed
with a bulk ``update()`` or raw SQL, membership rows changed outside the chat
endpoints, a group deactivated or deleted with its rooms. It also bounds an
announcement that Redis lost.
"""

import time

from django.db import DEFAULT_DB_ALIAS, connections

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

    ``epoch`` counts the times a decision was forgotten. A caller reads it
    before it starts a check and hands it to ``allow``; the answer of a check
    that an access change overtook is then not remembered.

    ``clock`` and ``recheck`` are test seams; production uses
    ``time.monotonic`` and ``recheck_seconds``.
    """

    def __init__(self, clock=None, recheck=None):
        self._clock = clock or time.monotonic
        self._recheck = recheck or recheck_seconds
        self._expires = {}
        self.epoch = 0

    def allows(self, topic):
        expires = self._expires.get(topic)
        if expires is None:
            return False
        if self._clock() < expires:
            return True
        del self._expires[topic]
        return False

    def allow(self, topic, ttl=None, epoch=None):
        if epoch is not None and epoch != self.epoch:
            # Something was forgotten while this check ran: its answer may
            # predate the change, so the next frame checks again.
            return
        if ttl is None:
            ttl = self._recheck()
        if ttl <= 0:
            self._expires.pop(topic, None)
            return
        self._expires[topic] = self._clock() + ttl

    def forget(self, topic):
        self.epoch += 1
        self._expires.pop(topic, None)

    def forget_all(self):
        self.epoch += 1
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


class _Scope:
    """The open transaction a snapshot was taken in.

    A snapshot read or written inside a transaction describes rows that a
    rollback takes back, and Django does not undo Python state on rollback.
    ``hooks`` is the connection's on-commit list at that moment: Django
    replaces that list on every rollback, savepoint rollback and commit, so
    the scope is still open only while the connection holds the same list.
    """

    __slots__ = ("hooks", "committed")

    def __init__(self, hooks):
        self.hooks = hooks
        self.committed = False

    def commit(self):
        self.committed = True
        self.hooks = None


def _connection(using):
    return connections[using or DEFAULT_DB_ALIAS]


def _open_scope(using):
    """The scope of the transaction open on ``using``, None outside one."""
    conn = _connection(using)
    if not conn.in_atomic_block:
        return None
    hooks = getattr(conn, "run_on_commit", None)
    scope = getattr(conn, "_mojo_access_scope", None)
    if scope is None or hooks is None or scope.hooks is not hooks:
        scope = _Scope(hooks)
        if hooks is not None:
            conn.on_commit(scope.commit)
        conn._mojo_access_scope = scope
    return scope


def _scope_holds(scope, using):
    if scope is None or scope.committed:
        return True
    conn = _connection(using)
    return (
        scope.hooks is not None and conn.in_atomic_block
        and scope.hooks is getattr(conn, "run_on_commit", None))


def access_before(instance):
    """What ``instance`` last saw stored, or None when that is not known:
    it was never read, or it was read or saved inside a transaction that has
    since been rolled back (wholly or to a savepoint)."""
    state = instance.__dict__
    seen = state.get("_access_seen")
    if seen is None:
        return None
    if not _scope_holds(state.get("_access_scope"), instance._state.db):
        return None
    return seen


def remember_access(instance, fields=None):
    """Note what ``instance`` now knows the row to store. Called after a
    read, a refresh or a save; ``fields`` names the ones a partial refresh
    or save just synced."""
    state = instance.__dict__
    state["_access_seen"] = access_snapshot(instance, access_before(instance), fields)
    state["_access_scope"] = _open_scope(instance._state.db)


def save_is_insert(instance, force_insert=False):
    """Whether a save is certain to add a row. An instance built with the
    primary key of an existing row updates that row without having read it,
    so only a missing key or a forced insert is certain."""
    return instance._state.adding and bool(instance.pk is None or force_insert)


def save_removes_access(before, instance, update_fields=None):
    """Whether saving ``instance`` can take access away from its user.

    ``before`` is ``access_before(instance)``: what the row was last known
    to store, or None when that is not known. Granting is never an answer of True. A value this instance never
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
