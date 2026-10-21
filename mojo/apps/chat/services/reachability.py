"""
Who a caller may put in a chat with them (maestro #8345).

One rule, asked at every client door that brings another account into a room:
`POST dm`, `POST room/member/add`. Server code that creates rooms or
memberships through the ORM never passes through here.

Default rule (no hook configured): any active user, as in 1.35.3 (#8386). A
group room takes members from outside its group, and an adder outside it.

Opt-in shared-group rule: `require_shared_group`. A caller holding the GLOBAL
`manage_chat` (a superuser implies it) reaches any active user, but never
through a session that assumes a member (`is_override_user_session`), the same
discipline as `_check_room_admin`. Otherwise caller and target must share an
active group: both hold an active DIRECT `GroupMember` row in the same
effectively active group. Inherited (parent-chain) membership does not count.

Hook: `CHAT_REACHABILITY_HANDLER = "myapp.chat.can_reach"` in the Django
settings FILE, called as `handler(user, target, room)` (room is None for a DM)
and reaching only when it returns True. It is read with `settings.get_static`,
never from a database Setting row, so nobody holding `manage_settings` can
point it at importable code. It fails closed: a load error, a non-callable or
an exception refuses. `require_shared_group`, `allow_all` and `deny_all` ship
for composition; a project that wants group rooms to take only their group's
members checks `room.group` in its own hook.

Every target-side refusal answers with `refuse(request)`: one status and one
body for a missing, inactive, own, absent or refused id. With no hook any
active id is reachable, so a DM that opens tells the caller the id is an
active account.
"""
from mojo.helpers import logit, modules
from mojo.helpers.request import is_override_user_session, is_signed_in_user
from mojo.helpers.settings import settings

logger = logit.get_logger("chat", "chat.log")

HANDLER_KEY = "CHAT_REACHABILITY_HANDLER"

# Sentinel for the keyword-only test seams.
_UNSET = object()
# A configured hook that could not be loaded: refuse, never fall back.
_BROKEN = object()


def parse_user_id(value):
    """Return a positive int user id, or None for anything else.

    Absent, empty, boolean, non-numeric and < 1 all come back None, so the
    caller can fold them into the uniform refusal.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        user_id = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return user_id if user_id >= 1 else None


def _is_person(user):
    # A real account.User; an ApiKey or the system pseudo-user is not one.
    return is_signed_in_user(user)


def is_caller(request, target_id):
    """True when `target_id` is the calling person's own id. An ApiKey caller
    is no person, so its own pk never collides with a user id."""
    return _is_person(request.user) and request.user.pk == target_id


def _active_request():
    from mojo.models.rest import ACTIVE_REQUEST
    return ACTIVE_REQUEST.get()


def has_global_reach(user, request=None):
    """Global `manage_chat`, never through an assumed-member session."""
    if user is None:
        return False
    if request is None:
        request = _active_request()
    if request is not None and is_override_user_session(request):
        return False
    return bool(user.has_permission("manage_chat"))


def shares_group(user, target_id):
    """True when `user` and the user `target_id` hold an active direct
    membership in one effectively active group.

    One keyed query whether or not the target exists, so a missing and an
    unrelated id cost the same. Each hit is confirmed with
    `is_effectively_active` (DM-048: an inactive ancestor darkens the group).
    """
    if not _is_person(user):
        return False
    from mojo.apps.account.models import GroupMember
    mine = GroupMember.objects.filter(
        user_id=user.pk, is_active=True, group__is_active=True,
    ).values("group_id")
    shared = GroupMember.objects.filter(
        user_id=target_id, user__is_active=True, is_active=True,
        group__is_active=True, group_id__in=mine,
    ).select_related("group")
    return any(gm.group.is_effectively_active() for gm in shared)


def require_shared_group(user, target, room=None):
    """The opt-in shared-group rule: global `manage_chat`, else a shared
    active group. Point `CHAT_REACHABILITY_HANDLER` here to switch it on."""
    if target is None or not target.is_active:
        return False
    if has_global_reach(user):
        return True
    return shares_group(user, target.pk)


def allow_all(user, target, room=None):
    """Reach any active user: the same as having no hook (1.35.3 behaviour)."""
    return True


def deny_all(user, target, room=None):
    """Refuse every client DM and member add; server code still creates rooms."""
    return False


def _handler(*, handler=_UNSET, path=_UNSET):
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


def _load_target(target_id):
    from mojo.apps.account.models import User
    return User.objects.filter(pk=target_id, is_active=True).first()


def reachable_user(request, target_id, room=None, *, handler=_UNSET, path=_UNSET):
    """Return the active User `target_id` if the caller may put them in a chat
    with them (in `room`, or a DM when None), else None.

    The caller's own id is the view's to refuse first. With no hook any
    active user is reachable.
    """
    caller = request.user
    func = _handler(handler=handler, path=path)
    if func is _BROKEN:
        return None

    if func is None:
        target = _load_target(target_id)
        if target is None:
            return None
    else:
        target = _load_target(target_id)
        if target is None:
            return None
        try:
            allowed = func(caller, target, room)
        except Exception as e:
            logger.error(f"chat: {HANDLER_KEY} raised, refusing: {e}")
            return None
        if allowed is not True:
            return None
    return target


def refuse(request):
    """The one answer for every target-side refusal."""
    from ..models import ChatRoom
    return ChatRoom.rest_error_response(request, 404, error="User not found")
