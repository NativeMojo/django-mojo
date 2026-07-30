"""Item 963 regression — a system-context save must not carry state between calls.

Two defects on the same path, fixed together:

1. `SYSTEM_REQUEST` was a module-level `objict` built once at import and shared
   by every request-less caller. `_evaluate_permission` rebinds `request.group`
   to a row's owning tenant on every FK attach (documented side effect, see its
   docstring), so one `create_from_dict` left that tenant on the singleton and
   the NEXT, unrelated create read it back at `on_rest_save` and stamped it onto
   the new row. Fixed by `system_request()`, built fresh per call.

2. The create/update owner auto-stamp assigned the pseudo-user *itself* to a
   `User` FK, raising `ValueError: Cannot assign "{'id': 1, ...}" ... must be a
   "User" instance`. That is why nothing in-repo ever reached defect 1 — the
   path crashed before it could leak. Fixed by `_resolve_stamp_actor`, which
   only yields a real model instance.

These run IN-PROCESS on purpose: the object under test is a module global in
*this* process, so `opts.client` (a separate server process) would not exercise
it. Same reasoning as tests/test_models/batch_row_permissions.py.
"""
import uuid as _uuid

import objict
from testit import helpers as th


PREFIX = "sysreq_"


def _cleanup():
    """Delete-before-create — these run against a long-lived database.

    Order matters: docit.Book pins `user` / `created_by` with on_delete=PROTECT,
    so books must go before the users they reference.
    """
    from mojo.apps.account.models import User, Group
    from mojo.apps.chat.models import ChatRoom, ChatMembership, ChatMessage

    ChatMessage.objects.filter(room__name__startswith=PREFIX).delete()
    ChatMembership.objects.filter(room__name__startswith=PREFIX).delete()
    ChatRoom.objects.filter(name__startswith=PREFIX).delete()
    if th.is_app_installed("mojo.apps.docit"):
        from mojo.apps.docit.models import Book
        Book.objects.filter(title__startswith=PREFIX).delete()
    User.objects.filter(email__startswith=PREFIX).delete()
    Group.objects.filter(name__startswith=PREFIX).delete()


def _build_request(user, group):
    """A caller-supplied request, same shape as batch_row_permissions.py."""
    req = objict.objict()
    req.user = user
    req.DATA = objict.objict()
    req.QUERY_PARAMS = objict.objict()
    req.method = "POST"
    req.group = group
    req.bearer = None
    req.ip = "127.0.0.1"
    req.path = "/api/chat/room"
    req.META = {}
    req.api_key = None
    return req


@th.django_unit_setup()
@th.requires_app("mojo.apps.chat")
def setup_system_request_isolation(opts):
    from mojo.apps.account.models import User, Group
    from mojo.apps.chat.models import ChatRoom
    from mojo.models import rest as mojo_rest

    _cleanup()

    tag = _uuid.uuid4().hex[:8]
    opts.tag = tag

    opts.group = Group.objects.create(name=f"{PREFIX}grp_a_{tag}", is_active=True)
    opts.other_group = Group.objects.create(name=f"{PREFIX}grp_b_{tag}", is_active=True)

    opts.user = User.objects.create_user(
        username=f"{PREFIX}{tag}@test.com",
        email=f"{PREFIX}{tag}@test.com",
        password="testit##mojo",
    )
    opts.user.is_active = True
    opts.user.save()

    # The poisoning room MUST be ownerless. ChatRoom.VIEW_PERMS includes
    # "owner" (chat/models/room.py), and the owner branch in
    # _evaluate_permission (mojo/models/rest.py, "owner" in perms) returns
    # BEFORE the request.group rebind below it. The system pseudo-user is id 1,
    # so a room owned by user id 1 would short-circuit the poisoning step and
    # test_tenant_does_not_leak_between_system_creates would pass vacuously
    # against broken code. Leave user=None so the owner check falls through.
    opts.room = ChatRoom.objects.create(
        name=f"{PREFIX}room_{tag}", group=opts.group, user=None)
    assert opts.room.user_id is None, (
        "the poisoning room must be ownerless or the owner branch short-circuits "
        f"the tenant rebind, got user_id={opts.room.user_id}"
    )

    # A sibling test module running in a parallel thread may have written to the
    # shared alias before this module started. Clear it so the assertions below
    # measure THIS module's calls.
    mojo_rest.SYSTEM_REQUEST.group = None

    if th.is_app_installed("mojo.apps.docit"):
        from mojo.apps.docit.models import Book
        opts.book = Book.objects.create(
            title=f"{PREFIX}book_{tag}",
            group=opts.group,
            user=opts.user,
            created_by=opts.user,
        )
    else:
        opts.book = None


@th.django_unit_test("963: a system-context create does not inherit an earlier call's tenant")
def test_tenant_does_not_leak_between_system_creates(opts):
    """The core regression. Pre-fix the second row was stamped with group A."""
    from mojo.apps.chat.models import ChatRoom, ChatMessage

    # Poison: the room FK attach runs a VIEW check on ChatRoom, which rebinds
    # request.group to the room's owning tenant. `user=` is a model kwarg, not a
    # body key, so the owner field is already set and the create-time owner
    # stamp is skipped — this test is about the tenant, not the owner stamp
    # (ChatMessage.NO_SAVE_FIELDS pins `user`, so the body cannot carry it).
    msg = ChatMessage.create_from_dict(
        {"room": opts.room.pk, "body": "hi"}, user=opts.user)
    assert msg.room_id == opts.room.pk, (
        f"setup precondition: the room FK must attach, got room_id={msg.room_id}"
    )

    # Victim: a completely unrelated create, with no group in the body.
    victim_name = f"{PREFIX}victim_{opts.tag}"
    victim = ChatRoom.create_from_dict({"name": victim_name}, user=opts.user)

    assert victim.group_id is None, (
        f"TENANT LEAK: a system-context create inherited group "
        f"{victim.group_id} from an earlier, unrelated call "
        f"(fixture group={opts.group.pk})"
    )


@th.django_unit_test("963: system_request() is a fresh object per call")
def test_system_request_is_per_call(opts):
    """AC 1 + AC 3 — isolation, and the pseudo-request contract is preserved."""
    from mojo.models.rest import system_request, SYSTEM_REQUEST

    first = system_request()
    second = system_request()
    assert first is not second, (
        "system_request() must build a new object per call, got the same instance"
    )

    first.group = "poisoned"
    first.DATA.marker = "poisoned"
    assert second.group is None, (
        f"a write to one system request must not be visible on the next, "
        f"got group={second.group!r}"
    )
    assert not second.DATA, (
        f"DATA must be a fresh objict per call, got {dict(second.DATA)!r}"
    )

    # The contract SYSTEM_REQUEST has always carried (AC 3).
    for req, label in ((first, "system_request()"), (SYSTEM_REQUEST, "SYSTEM_REQUEST")):
        assert req.user.id == 1, f"{label}: user.id must be 1, got {req.user.id}"
        assert req.user.username == "system", (
            f"{label}: user.username must be 'system', got {req.user.username!r}"
        )
        assert req.user.display_name == "System", (
            f"{label}: user.display_name must be 'System', got {req.user.display_name!r}"
        )
        assert req.user.email == "", (
            f"{label}: user.email must be '', got {req.user.email!r}"
        )
        assert req.user.is_authenticated is True, (
            f"{label}: user.is_authenticated must be True, got {req.user.is_authenticated!r}"
        )
        assert req.user.has_permission("anything") is True, (
            f"{label}: has_permission must be omnipotent for system context"
        )
    assert isinstance(SYSTEM_REQUEST.DATA, objict.objict), (
        f"SYSTEM_REQUEST.DATA must stay a usable objict, got {type(SYSTEM_REQUEST.DATA)}"
    )


@th.django_unit_test("963: an explicitly supplied request still wins over the system fallback")
def test_explicit_request_is_honored(opts):
    """Control — must pass BEFORE and AFTER. Pins that swapping the eager
    `kwargs.pop` default for a sentinel did not break the caller-supplied path
    that on_rest_handle_batch relies on."""
    from mojo.apps.chat.models import ChatRoom

    req = _build_request(opts.user, opts.other_group)
    name = f"{PREFIX}explicit_{opts.tag}"
    room = ChatRoom.create_from_dict({"name": name}, request=req, user=opts.user)

    assert room.group_id == opts.other_group.pk, (
        f"an explicitly supplied request.group must still be stamped, "
        f"expected {opts.other_group.pk}, got {room.group_id}"
    )


@th.django_unit_test("963: a system-context create leaves the owner field null instead of raising")
def test_system_create_does_not_stamp_pseudo_user(opts):
    """The natural call — no owner kwarg. Pre-fix this raised
    ValueError: Cannot assign "{'id': 1, ...}": "ChatMessage.user" must be a
    "User" instance, because is_request_user() is a false positive for the
    objict pseudo-user."""
    from mojo.apps.chat.models import ChatMessage

    msg = ChatMessage.create_from_dict({"room": opts.room.pk, "body": "no owner"})

    assert msg.pk is not None, "a system-context create must succeed"
    assert msg.user_id is None, (
        f"there is no real user to attribute a system-context create to, so the "
        f"owner field must be left null, got user_id={msg.user_id}"
    )


@th.django_unit_test("963: a system-context update leaves modified_by alone instead of raising")
@th.requires_app("mojo.apps.docit")
def test_system_update_does_not_stamp_pseudo_user(opts):
    """The update branch has no 'already set' guard — its setattr is
    unconditional — so pre-fix this raised on EVERY system-context update of a
    model declaring UPDATED_BY_OWNER_FIELD. docit.Book is such a model."""
    assert opts.book is not None, "setup must have created the docit fixture book"

    new_title = f"{PREFIX}retitled_{opts.tag}"
    opts.book.update_from_dict({"title": new_title})

    opts.book.refresh_from_db()
    assert opts.book.title == new_title, (
        f"a system-context update must still apply the body, got {opts.book.title!r}"
    )
    assert opts.book.modified_by_id is None, (
        f"there is no real user to attribute a system-context update to, so "
        f"modified_by must be left alone, got {opts.book.modified_by_id}"
    )
