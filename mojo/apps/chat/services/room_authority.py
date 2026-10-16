"""
Who may run a room-admin operation through the REST API (maestro #8347).

One rule, asked by the view functions in `rest/rooms.py` and nowhere else:
`room/rules`, `room/join`, `room/leave`, `room/member/remove`,
`room/member/mute`, `room/member/ban`, and PUT/POST/PATCH/DELETE on
`room/<pk>`. Server code that writes rooms, rules or memberships through the
ORM never passes through here, and its values are never validated.

Actions: `rules`, `update`, `delete`, `join`, `leave`, `member_remove`,
`member_mute`, `member_ban`.

Fixed parts, which no hook can change (the views run them before `require`):

- tenant confinement (`_deny_cross_tenant_room`) and the per-kind answers;
- value validation of every rules write (`validate_rules`, 400);
- a banned caller is refused every action except `leave`.

Default rule (`default_verdict`, no hook configured). A "global grant" is
`request.user.has_permission(...)`, never through a session that assumes a
member (`is_override_user_session`), the same discipline as
`_check_room_admin`.

- `rules`: `_check_room_admin` passes. Tightening `disappearing_ttl` (turning
  it on or shortening it) needs global `manage_chat`.
- `update`: an active or muted owner/admin membership, or `manage_chat` /
  `comms` in the room's group or globally. Rules changes follow `rules`.
  RestMeta `SAVE_PERMS` still runs afterwards.
- `delete`: allowed here; RestMeta `DELETE_PERMS` decides afterwards.
- `join`: allowed here. A group channel keeps its group check in the view,
  which no hook loosens.
- `leave`: allowed.
- `member_remove`: `_check_room_admin` passes and role order holds.
- `member_mute`, `member_ban`: `_check_room_moderator` passes and role order
  holds.

Role order: owner > admin > member. A caller acting only through a room role
may act on themself or on a target of strictly lower role. A grant holder
(`manage_chat` or `moderate_chat`, in the room's group or globally) acts on
anyone.

Hook: `CHAT_ROOM_AUTHORITY_HANDLER = "myapp.chat.room_authority"` in the
Django settings FILE, called as `handler(user, room, action, context)` and
allowing only when it returns True. `context` carries `target` (the target's
ChatMembership, or None, for `member_*`), `changes` (`{key: (old, new)}`),
`assumed_member` and `default` (the built-in verdict), so a project narrows
with `ctx["default"] and mine` or widens with `ctx["default"] or mine`. It is
read with `settings.get_static`, never from a database Setting row. It fails
closed: a load error, a non-callable, an exception or any return other than
True refuses. On the seven POST views its verdict is final; on `room/<pk>`
RestMeta perms run afterwards, so there it can only narrow. `deny_all` and
`default_room_authority` ship for composition.
"""
import json

from mojo import errors as merrors
from mojo.helpers import logit, modules
from mojo.helpers.request import is_override_user_session
from mojo.helpers.settings import settings

logger = logit.get_logger("chat", "chat.log")

HANDLER_KEY = "CHAT_ROOM_AUTHORITY_HANDLER"

ACTIONS = (
    "rules", "update", "delete", "join", "leave",
    "member_remove", "member_mute", "member_ban",
)

RULE_KEYS = (
    "allow_urls", "allow_media", "allow_phone_numbers",
    "max_message_length", "disappearing_ttl", "rate_limit",
)

TTL_MIN = 3600            # 1 hour
TTL_MAX = 31536000        # 365 days
MAX_MESSAGE_LENGTH = 4000
MAX_RATE_LIMIT = 100

# Fields of room/<pk> whose change is reported to the rule.
UPDATE_FIELDS = ("name", "kind", "group", "metadata")

ROLE_RANK = {"owner": 3, "admin": 2, "member": 1}

# Sentinel for the keyword-only test seams.
_UNSET = object()
# A configured hook that could not be loaded: refuse, never fall back.
_BROKEN = object()


# --- values ----------------------------------------------------------------

# Longer digit strings are no rule value; int() of a very long one raises.
MAX_DIGITS = 12


def _int_value(value):
    """An int from an int or a string of ASCII digits (at most MAX_DIGITS);
    None for anything else (booleans, '²' and other Unicode digits included)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.isascii() and text.isdigit() and len(text) <= MAX_DIGITS:
            return int(text)
    return None


def _bool_value(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    return None


def _bad(key, accepted):
    raise merrors.ValueException(f"Invalid rule '{key}': must be {accepted}")


def _clean_rule(key, value):
    if key == "disappearing_ttl":
        ttl = _int_value(value)
        if ttl is None or not (ttl == 0 or TTL_MIN <= ttl <= TTL_MAX):
            _bad(key, f"0 (off) or an integer from {TTL_MIN} to {TTL_MAX} seconds")
        return ttl
    if key == "max_message_length":
        length = _int_value(value)
        if length is None or not (1 <= length <= MAX_MESSAGE_LENGTH):
            _bad(key, f"an integer from 1 to {MAX_MESSAGE_LENGTH}")
        return length
    if key == "rate_limit":
        limit = _int_value(value)
        if limit is None or not (0 <= limit <= MAX_RATE_LIMIT):
            _bad(key, f"0 (off) or an integer from 1 to {MAX_RATE_LIMIT} per second")
        return limit
    if key in ("allow_urls", "allow_media", "allow_phone_numbers"):
        flag = _bool_value(value)
        if flag is None:
            _bad(key, "true or false")
        return flag
    # Unknown keys pass through unread.
    return value


def validate_rules(incoming, *, replace=False):
    """Return `incoming` with every known rule key checked and normalised
    (digit strings to int, "true"/"false" to bool). Raises 400 naming the key
    and its range.

    Only the keys present are checked, so a room already holding a bad stored
    value can still change other keys. `replace` is accepted for the caller's
    clarity: on a replace the whole new dict is what is present.
    """
    if not isinstance(incoming, dict):
        raise merrors.ValueException("Invalid rules: must be an object")
    return {key: _clean_rule(key, value) for key, value in incoming.items()}


def _stored_ttl(value):
    """An already-stored TTL for comparison: unparseable or negative is 0
    (unset), so writing 0 over a bad stored value always succeeds."""
    ttl = _int_value(value)
    return ttl if ttl is not None and ttl > 0 else 0


def is_tightening(old, new):
    """True when a `disappearing_ttl` write turns it on or shortens it."""
    old_ttl = _stored_ttl(old)
    new_ttl = _stored_ttl(new)
    if new_ttl == 0:
        return False
    return old_ttl == 0 or new_ttl < old_ttl


def rules_changes(room, new_rules):
    """`{key: (old, new)}` for each rule key whose value would change."""
    old_rules = room.rules if isinstance(room.rules, dict) else {}
    keys = set(old_rules) | set(new_rules)
    changes = {}
    for key in keys:
        if key == "__replace":
            continue
        old = old_rules.get(key)
        new = new_rules.get(key)
        if old != new:
            changes[key] = (old, new)
    return changes


def _related_pk(value):
    if value is None:
        return None
    if hasattr(value, "pk"):
        return value.pk
    as_int = _int_value(value)
    if as_int is not None:
        return as_int
    # A dict or anything else the framework might act on: never equal.
    return _UNSET


def update_changes(room, data):
    """Validate and diff a `room/<pk>` write.

    Returns `(changes, rules)`: `changes` is `{key: (old, new)}` over
    `UPDATE_FIELDS` and each changed rule key; `rules` is the normalised
    incoming rules value to save (None when the write carries no rules).
    Raises 400 for a rules value that is not an object (or a JSON string of
    one) or holds a bad value. Fields the save never takes (`user`) are not
    reported.
    """
    changes = {}
    for key in UPDATE_FIELDS:
        names = (key, f"{key}_id") if key == "group" else (key,)
        for name in names:
            if name not in data:
                continue
            new = data.get(name)
            if key == "group":
                old = room.group_id
                new = _related_pk(new)
            else:
                old = getattr(room, key, None)
            if old != new:
                changes[key] = (old, new)

    rules = None
    if "rules" in data:
        incoming = data.get("rules")
        if isinstance(incoming, str):
            try:
                incoming = json.loads(incoming)
            except (TypeError, ValueError):
                incoming = None
        if not isinstance(incoming, dict):
            raise merrors.ValueException("Invalid rules: must be an object")
        incoming = dict(incoming)
        replace = bool(incoming.pop("__replace", False))
        clean = validate_rules(incoming, replace=replace)
        if replace:
            resulting = clean
        else:
            resulting = dict(room.rules if isinstance(room.rules, dict) else {})
            resulting.update(clean)
        changes.update(rules_changes(room, resulting))
        rules = dict(clean)
        if replace:
            rules["__replace"] = True
    return changes, rules


# --- grants ----------------------------------------------------------------

def _global(request, perms):
    if is_override_user_session(request):
        return False
    return bool(request.user.has_permission(perms))


def _group_grant(request, room, perms):
    if not room.group:
        return False
    return room.group.user_has_permission(
        request.user, perms, not is_override_user_session(request))


def _membership(room, user):
    from ..models import ChatMembership
    if user is None or not hasattr(user, "is_request_user"):
        return None
    return ChatMembership.objects.filter(room=room, user=user).first()


def is_banned(request, room):
    membership = _membership(room, request.user)
    return membership is not None and membership.status == "banned"


def _passes(check, request, room):
    try:
        check(request, room)
    except merrors.PermissionDeniedException:
        return False
    return True


def _role_order(request, room, target):
    """True when the caller may act on `target` (a ChatMembership or None)."""
    grants = ["moderate_chat", "manage_chat"]
    if _global(request, grants) or _group_grant(request, room, grants):
        return True
    if target is None:
        return True
    if target.user_id == request.user.pk:
        return True
    mine = _membership(room, request.user)
    if mine is None or mine.status == "banned":
        return False
    return ROLE_RANK.get(mine.role, 0) > ROLE_RANK.get(target.role, 0)


def _rules_allowed(request, changes):
    """The rule-value part of the default: tightening `disappearing_ttl`
    needs global `manage_chat`."""
    if "disappearing_ttl" in changes:
        old, new = changes["disappearing_ttl"]
        if is_tightening(old, new) and not _global(request, "manage_chat"):
            return False
    return True


def default_verdict(request, room, action, *, target=None, changes=None):
    """The framework's rule for `action`, as a bool."""
    from ..rest.rooms import _check_room_admin, _check_room_moderator
    changes = changes or {}
    if action == "rules":
        return _passes(_check_room_admin, request, room) and _rules_allowed(request, changes)
    if action == "update":
        mine = _membership(room, request.user)
        allowed = (
            (mine is not None and mine.is_admin and mine.status in ("active", "muted"))
            or _group_grant(request, room, ["manage_chat", "comms"])
            or _global(request, ["manage_chat", "comms"])
        )
        if not allowed:
            return False
        return _rules_allowed(request, changes)
    if action == "delete":
        return True
    if action in ("join", "leave"):
        return True
    if action == "member_remove":
        return (_passes(_check_room_admin, request, room)
                and _role_order(request, room, target))
    if action in ("member_mute", "member_ban"):
        return (_passes(_check_room_moderator, request, room)
                and _role_order(request, room, target))
    return False


def default_room_authority(user, room, action, context):
    """The framework's verdict, for a hook to compose with."""
    return context.get("default") is True


def deny_all(user, room, action, context):
    """Refuse every room-admin REST operation; server code is unaffected."""
    return False


# --- hook ------------------------------------------------------------------

def _load_handler(*, handler=_UNSET, path=_UNSET):
    """Return the configured hook, None when none is set, or _BROKEN.

    `handler` and `path` are keyword-only test seams: `handler` is the callable
    itself, `path` a dotted path to load. Passing None for either means "no
    hook". Production passes neither and reads the settings file.
    """
    if handler is not _UNSET:
        if handler is None:
            return None
        return handler if callable(handler) else _BROKEN
    configured = path if path is not _UNSET else settings.get_static(HANDLER_KEY, None)
    if not configured:
        return None
    if callable(configured):
        return configured
    try:
        # Exception, not ImportError: the target module can raise anything
        # at import time.
        func = modules.load_function(str(configured))
    except Exception as e:
        logger.error(f"chat: could not load {HANDLER_KEY} '{configured}': {e}")
        return _BROKEN
    if not callable(func):
        logger.error(f"chat: {HANDLER_KEY} '{configured}' is not callable")
        return _BROKEN
    return func


def allows(request, room, action, *, target=None, changes=None,
           handler=_UNSET, path=_UNSET):
    """The verdict for `action` on `room`: the hook's when one is set, else
    the default."""
    verdict = default_verdict(request, room, action, target=target, changes=changes)
    func = _load_handler(handler=handler, path=path)
    if func is None:
        return verdict
    if func is _BROKEN:
        return False
    context = {
        "target": target,
        "changes": dict(changes or {}),
        "assumed_member": is_override_user_session(request),
        "default": verdict,
    }
    try:
        allowed = func(request.user, room, action, context)
    except Exception as e:
        logger.error(f"chat: {HANDLER_KEY} raised on '{action}', refusing: {e}")
        return False
    if allowed is not True:
        if allowed is not False:
            logger.error(
                f"chat: {HANDLER_KEY} returned {allowed!r} on '{action}', refusing")
        return False
    return True


def require(request, room, action, *, target=None, changes=None,
            handler=_UNSET, path=_UNSET):
    """Raise 403 unless `action` is allowed."""
    if not allows(request, room, action, target=target, changes=changes,
                  handler=handler, path=path):
        raise merrors.PermissionDeniedException()
