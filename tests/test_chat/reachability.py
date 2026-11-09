"""
Who a caller may put in a chat with them (maestro #8345, #8386): `POST dm`,
`POST room/member/add` and the room-create door.

Fixtures: A and B share group G; A and D share group H, D is in no other
group; C shares nothing with anyone; Z is inactive (shares G); S holds the
global manage_chat. With no hook any active user is reachable and a G room
takes outsiders, as in 1.35.3; `require_shared_group` is the opt-in limit.

The hook (`CHAT_REACHABILITY_HANDLER`) is exercised in-process through the
keyword-only `handler=` / `path=` seams on `reachable_user`: this package is
scanned strict, so no setting is patched here. The one settings-file
end-to-end case lives in `tests/test_chat_reach_extended_serial`.
"""
from types import SimpleNamespace

from testit import helpers as th
from testit.helpers import assert_eq, assert_true

TESTIT_TIER = "core"

PASSWORD = "TestPass1!"
PREFIX = "chat-reach-"
EMAILS = {
    name: f"{PREFIX}{name}@example.com"
    for name in ("a", "b", "c", "d", "z", "s")
}
GROUPS = {name: f"test-{PREFIX}{name}" for name in ("g", "h", "j")}
ROOM_PREFIX = f"test-{PREFIX}"


@th.django_unit_setup()
@th.requires_app("mojo.apps.chat")
def setup_reachability(opts):
    from mojo.apps.account.models import Group, User
    from mojo.apps.chat.models import ChatRoom

    # Delete before create: this runs against a long-lived database. Direct
    # rooms the REST door creates carry no name, so they go with their users.
    users = User.objects.filter(email__in=EMAILS.values())
    ChatRoom.objects.filter(memberships__user__in=users).delete()
    ChatRoom.objects.filter(name__startswith=ROOM_PREFIX).delete()
    users.delete()
    Group.objects.filter(name__in=GROUPS.values()).delete()

    for name, email in EMAILS.items():
        user = User.objects.create_user(username=email, email=email, password=PASSWORD)
        user.is_email_verified = True
        user.save()
        setattr(opts, name, user)
    opts.s.add_permission("manage_chat")
    opts.z.is_active = False
    opts.z.save()

    opts.g = Group.objects.create(name=GROUPS["g"])
    opts.h = Group.objects.create(name=GROUPS["h"])
    opts.j = Group.objects.create(name=GROUPS["j"])
    # A holds chat in G; B is a plain member.
    opts.g.add_member(opts.a).add_permission("chat")
    opts.g.add_member(opts.b)
    opts.g.add_member(opts.z)
    opts.h.add_member(opts.a)
    opts.h.add_member(opts.d)
    opts.missing_id = (User.objects.order_by("-pk").values_list("pk", flat=True).first() or 0) + 100000


def _login(opts, name):
    assert_true(opts.client.login(EMAILS[name], PASSWORD), f"login as {name} failed")


def _counts():
    """Track every row attributable to this module, including wrong-room writes.

    Other framework packages create chat fixtures concurrently. Room names
    alone miss unnamed REST DMs, while just the requested room misses a write
    routed to a different room. Include the module's users, groups and all
    rooms touched by those users before counting their related rows.
    """
    from django.db.models import Q
    from mojo.apps.account.models import Group, User
    from mojo.apps.chat.models import ChatMembership, ChatMessage, ChatRoom
    users = User.objects.filter(email__in=EMAILS.values()).values("pk")
    groups = Group.objects.filter(name__in=GROUPS.values()).values("pk")
    member_rooms = ChatMembership.objects.filter(user_id__in=users).values("room_id")
    authored_rooms = ChatMessage.objects.filter(user_id__in=users).values("room_id")
    rooms = ChatRoom.objects.filter(
        Q(name__startswith=ROOM_PREFIX) | Q(user_id__in=users) | Q(group_id__in=groups)
        | Q(pk__in=member_rooms) | Q(pk__in=authored_rooms)).values("pk")
    return (
        rooms.count(),
        ChatMembership.objects.filter(Q(room_id__in=rooms) | Q(user_id__in=users)).count(),
        ChatMessage.objects.filter(Q(room_id__in=rooms) | Q(user_id__in=users)).count(),
    )


def _room(opts, label, owner, group=None, members=()):
    """A room built by ORM, owned by `owner`; `members` are (user, role, status)."""
    from mojo.apps.chat.models import ChatMembership, ChatRoom
    room = ChatRoom.objects.create(name=ROOM_PREFIX + label, kind="group", user=owner, group=group)
    ChatMembership.objects.create(room=room, user=owner, role="owner")
    for user, role, status in members:
        ChatMembership.objects.create(room=room, user=user, role=role, status=status)
    return room


def _drop_direct(*users):
    """Remove every direct room any of `users` is in, so a DM test starts clean."""
    from mojo.apps.chat.models import ChatRoom
    ChatRoom.objects.filter(kind="direct", memberships__user__in=users).delete()


def _request(user, **identity):
    """The smallest request reachable_user reads: the caller and, for a
    confined credential, its identity."""
    return SimpleNamespace(user=user, **identity)


def _answer(resp):
    return resp.status_code, dict(resp.json or {})


@th.tier("core")
@th.django_unit_test()
def test_dm_shared_group_opens_room(opts):
    """A and B share G: the DM opens with both as members."""
    from mojo.apps.chat.models import ChatMembership
    _drop_direct(opts.a, opts.b)
    _login(opts, "a")
    resp = opts.client.post("/api/chat/dm", {"user_id": opts.b.pk})
    assert_eq(resp.status_code, 200, f"A may DM B (shared group), got {resp.status_code}: {resp.json}")
    assert_eq(resp.json.data.kind, "direct", "a DM is a direct room")
    members = set(ChatMembership.objects.filter(
        room_id=resp.json.data.id).values_list("user_id", flat=True))
    assert_eq(members, {opts.a.pk, opts.b.pk}, f"the DM holds exactly A and B, got {members}")


@th.tier("core")
@th.django_unit_test()
def test_dm_refusals_are_uniform(opts):
    """Missing, own, absent, non-numeric, zero, inactive and boolean ids all
    get the one answer the missing id gets, and nothing is written."""
    _drop_direct(opts.a)
    _login(opts, "a")
    expected = _answer(opts.client.post("/api/chat/dm", {"user_id": opts.missing_id}))
    assert_eq(expected[0], 404, f"a missing id is refused with 404, got {expected}")
    assert_eq(expected[1].get("error"), "User not found", f"refusal body, got {expected}")

    before = _counts()
    cases = {
        "own id": {"user_id": opts.a.pk},
        "absent": {},
        "non-numeric": {"user_id": "abc"},
        "zero": {"user_id": 0},
        "inactive": {"user_id": opts.z.pk},
        "boolean": {"user_id": True},
    }
    for label, body in cases.items():
        got = _answer(opts.client.post("/api/chat/dm", body))
        assert_eq(got, expected, f"DM to {label} must answer exactly like a missing id: {got} vs {expected}")
    assert_eq(_counts(), before, "a refused DM writes no room, membership or message")


@th.tier("core")
@th.django_unit_test()
def test_dm_any_active_user(opts):
    """With no hook A may DM C, who shares no group with A (1.35.3); the
    inactive Z still gets the uniform refusal."""
    from mojo.apps.chat.models import ChatMembership
    _drop_direct(opts.a, opts.c)
    _login(opts, "a")
    resp = opts.client.post("/api/chat/dm", {"user_id": opts.c.pk})
    assert_eq(resp.status_code, 200, f"A may DM C with no shared group, got {resp.status_code}: {resp.json}")
    members = set(ChatMembership.objects.filter(
        room_id=resp.json.data.id).values_list("user_id", flat=True))
    assert_eq(members, {opts.a.pk, opts.c.pk}, f"the DM holds exactly A and C, got {members}")

    expected = _answer(opts.client.post("/api/chat/dm", {"user_id": opts.missing_id}))
    got = _answer(opts.client.post("/api/chat/dm", {"user_id": opts.z.pk}))
    assert_eq(got, expected, f"an inactive user still answers like a missing id: {got} vs {expected}")


@th.tier("core")
@th.django_unit_test()
def test_dm_reuses_existing_room_without_rule(opts):
    """A pair that already shares a direct room gets that room back, and
    nothing is written."""
    from mojo.apps.chat.models import ChatMembership, ChatRoom
    _drop_direct(opts.a, opts.c)
    room = ChatRoom.objects.create(kind="direct", user=opts.a)
    ChatMembership.objects.create(room=room, user=opts.a, role="owner")
    ChatMembership.objects.create(room=room, user=opts.c, role="member")
    before = _counts()
    _login(opts, "a")
    resp = opts.client.post("/api/chat/dm", {"user_id": opts.c.pk})
    assert_eq(resp.status_code, 200, f"an existing DM is returned, got {resp.status_code}: {resp.json}")
    assert_eq(resp.json.data.id, room.pk, "the existing room comes back")
    assert_eq(_counts(), before, "reuse writes nothing")


@th.tier("core")
@th.django_unit_test()
def test_write_guard_ignores_parallel_fixtures_but_catches_wrong_room(opts):
    """Unrelated traffic is harmless; this module's misplaced writes still fail."""
    from mojo.apps.account.models import Group, User
    from mojo.apps.chat.models import ChatMembership, ChatMessage, ChatRoom
    email = "reachability-unrelated-counts@example.com"
    name = "reachability-unrelated-counts"
    ChatRoom.objects.filter(name=name).delete()
    User.objects.filter(email=email).delete()
    Group.objects.filter(name=name).delete()
    before = _counts()
    outsider = User.objects.create(username=email, email=email)
    group = Group.objects.create(name=name)
    room = ChatRoom.objects.create(name=name, user=outsider, group=group)
    try:
        ChatMembership.objects.create(room=room, user=outsider)
        ChatMessage.objects.create(room=room, user=outsider, body="another package's fixture")
        assert_eq(_counts(), before, "unrelated parallel room, membership and message inserts must not trip the reachability write guard")
        misplaced_member = ChatMembership.objects.create(room=room, user=opts.a)
        assert_true(_counts() != before, "a fixture user's membership inserted in the wrong unrelated room must still trip the write guard")
        misplaced_member.delete()
        assert_eq(_counts(), before, "removing the attributable write must restore the guard baseline despite unrelated fixtures")
        misplaced_message = ChatMessage.objects.create(room=room, user=opts.a, body="misrouted fixture write")
        assert_true(_counts()[2] > before[2], "a fixture user's message inserted in the wrong unrelated room must still trip the message write guard")
        misplaced_message.delete()
        assert_eq(_counts(), before, "unrelated fixtures alone must remain outside the write guard after a misplaced message is removed")
    finally:
        room.delete()
        group.delete()
        outsider.delete()


@th.tier("core")
@th.django_unit_test()
def test_dm_global_manage_chat_reaches_anyone(opts):
    """S holds the global manage_chat: S may DM C, who shares nothing."""
    _drop_direct(opts.s, opts.c)
    _login(opts, "s")
    resp = opts.client.post("/api/chat/dm", {"user_id": opts.c.pk})
    assert_eq(resp.status_code, 200, f"global manage_chat reaches C, got {resp.status_code}: {resp.json}")


@th.tier("core")
@th.django_unit_test()
def test_override_session_gets_no_bypass(opts):
    """Under the opt-in `require_shared_group`, a credential that assumes S
    never inherits S's global reach. With no hook it reaches C like anyone."""
    from mojo.apps.chat.services import reachability
    from mojo.models.rest import ACTIVE_REQUEST
    rule = reachability.require_shared_group
    plain = _request(opts.s)
    assumed = _request(opts.s, api_key=SimpleNamespace(override_user=True))
    for request, reaches, label in ((plain, True, "S's own session"),
                                    (assumed, False, "an assumed-member session")):
        # The hook reads the request the framework is serving, as it would live.
        token = ACTIVE_REQUEST.set(request)
        try:
            got = reachability.reachable_user(request, opts.c.pk, handler=rule)
        finally:
            ACTIVE_REQUEST.reset(token)
        if reaches:
            assert_eq(getattr(got, "pk", None), opts.c.pk, f"{label} reaches C under the opt-in rule")
        else:
            assert_true(got is None, f"{label} must not reach C through S's global grant")
    got = reachability.reachable_user(assumed, opts.c.pk, handler=None)
    assert_eq(getattr(got, "pk", None), opts.c.pk, "with no hook an assumed-member session reaches C")


@th.tier("core")
@th.django_unit_test()
def test_hook_decides_and_fails_closed(opts):
    """The hook replaces the default rule; anything but True refuses; the
    opt-in `require_shared_group` restores the shared-group limit."""
    from mojo.apps.chat.services import reachability

    def boom(user, target, room):
        raise RuntimeError("hook failure")

    req = _request(opts.a)
    reach = reachability.reachable_user
    assert_true(reach(req, opts.b.pk, handler=reachability.deny_all) is None, "deny_all refuses A -> B")
    got = reach(req, opts.c.pk, handler=reachability.allow_all)
    assert_eq(getattr(got, "pk", None), opts.c.pk, "allow_all lets A reach C")
    assert_true(reach(req, opts.z.pk, handler=reachability.allow_all) is None,
                "allow_all still never reaches an inactive user")
    assert_true(reach(req, opts.b.pk, handler=boom) is None, "a raising hook refuses")
    assert_true(reach(req, opts.b.pk, handler=lambda u, t, r: 1) is None,
                "a hook must return True itself; a truthy 1 refuses")
    assert_true(reach(req, opts.b.pk, path="no_such_module.can_reach") is None,
                "an unimportable hook path refuses")
    assert_true(reach(req, opts.b.pk, path="mojo.apps.chat.services.reachability.HANDLER_KEY") is None,
                "a hook path naming a non-callable refuses")
    path = "mojo.apps.chat.services.reachability.require_shared_group"
    got = reach(req, opts.b.pk, path=path)
    assert_eq(getattr(got, "pk", None), opts.b.pk, "require_shared_group lets A reach B (shared G)")
    assert_true(reach(req, opts.c.pk, path=path) is None,
                "require_shared_group refuses A -> C (no shared group)")

    room = _room(opts, "hook-g", opts.a, group=opts.g)
    for label, kwargs in (("allow_all", {"handler": reachability.allow_all}),
                          ("no hook", {"handler": None})):
        got = reach(req, opts.c.pk, room=room, **kwargs)
        assert_eq(getattr(got, "pk", None), opts.c.pk, f"{label} lets A put C in a G room")


@th.tier("core")
@th.django_unit_test()
def test_member_add_groupless_room(opts):
    """A's groupless room: B and C (no shared group) are added; missing, own,
    absent, inactive and bad ids get the uniform refusal and nothing is
    written."""
    from mojo.apps.chat.models import ChatMembership
    room = _room(opts, "add-groupless", opts.a)
    _login(opts, "a")
    expected = _answer(opts.client.post("/api/chat/room/member/add",
                                        {"room_id": room.pk, "user_id": opts.missing_id}))
    assert_eq(expected[0], 404, f"a missing id is refused with 404, got {expected}")
    assert_eq(expected[1].get("error"), "User not found", f"refusal body, got {expected}")

    before = _counts()
    for label, extra in {"own id": {"user_id": opts.a.pk},
                         "absent": {}, "inactive": {"user_id": opts.z.pk},
                         "non-numeric": {"user_id": "abc"}}.items():
        got = _answer(opts.client.post("/api/chat/room/member/add", {"room_id": room.pk, **extra}))
        assert_eq(got, expected, f"adding {label} must answer exactly like a missing id: {got}")
    assert_eq(_counts(), before, "a refused add writes no membership and no system message")

    resp = opts.client.post("/api/chat/room/member/add", {"room_id": room.pk, "user_id": opts.b.pk})
    assert_eq(resp.status_code, 200, f"A may add B, got {resp.status_code}: {resp.json}")
    assert_true(ChatMembership.objects.filter(room=room, user=opts.b, status="active").exists(),
                "B is now an active member")
    resp = opts.client.post("/api/chat/room/member/add", {"room_id": room.pk, "user_id": opts.c.pk})
    assert_eq(resp.status_code, 200, f"A may add C with no shared group, got {resp.status_code}: {resp.json}")
    assert_true(ChatMembership.objects.filter(room=room, user=opts.c, status="active").exists(),
                "C is now an active member")


@th.tier("core")
@th.django_unit_test()
def test_member_add_group_room_takes_outsiders(opts):
    """A G room takes members from outside G (1.35.3): A adds D, who is not
    in G; A adds B; S adds C."""
    from mojo.apps.chat.models import ChatMembership
    room = _room(opts, "add-g", opts.a, group=opts.g)
    _login(opts, "a")
    resp = opts.client.post("/api/chat/room/member/add", {"room_id": room.pk, "user_id": opts.d.pk})
    assert_eq(resp.status_code, 200, f"A may add D from outside G, got {resp.status_code}: {resp.json}")
    assert_true(ChatMembership.objects.filter(room=room, user=opts.d, status="active").exists(), "D was added")

    resp = opts.client.post("/api/chat/room/member/add", {"room_id": room.pk, "user_id": opts.b.pk})
    assert_eq(resp.status_code, 200, f"B is in G: added, got {resp.status_code}: {resp.json}")

    _login(opts, "s")
    resp = opts.client.post("/api/chat/room/member/add", {"room_id": room.pk, "user_id": opts.c.pk})
    assert_eq(resp.status_code, 200, f"S may add C to a G room, got {resp.status_code}: {resp.json}")
    assert_true(ChatMembership.objects.filter(room=room, user=opts.c, status="active").exists(), "C was added")


@th.tier("core")
@th.django_unit_test()
def test_member_add_by_owner_outside_group(opts):
    """The owner of a G room who is not in G adds B, as in 1.35.3."""
    from mojo.apps.chat.models import ChatMembership
    room = _room(opts, "add-foreign", opts.c, group=opts.g)
    _login(opts, "c")
    resp = opts.client.post("/api/chat/room/member/add", {"room_id": room.pk, "user_id": opts.b.pk})
    assert_eq(resp.status_code, 200, f"an owner outside G may add B, got {resp.status_code}: {resp.json}")
    assert_true(ChatMembership.objects.filter(room=room, user=opts.b, status="active").exists(), "B was added")


@th.tier("core")
@th.django_unit_test()
def test_banned_member_readded_and_banned_admin_powerless(opts):
    """An active owner re-adding a banned B makes B active again; a banned
    admin has no room authority, a muted admin keeps it."""
    from mojo.apps.chat.models import ChatMembership
    room = _room(opts, "ban", opts.a, members=[(opts.b, "member", "banned")])
    _login(opts, "a")
    resp = opts.client.post("/api/chat/room/member/add", {"room_id": room.pk, "user_id": opts.b.pk})
    assert_eq(resp.status_code, 200, f"the owner re-adds B, got {resp.status_code}: {resp.json}")
    assert_eq(ChatMembership.objects.get(room=room, user=opts.b).status, "active", "B is active again")

    admin_room = _room(opts, "ban-admin", opts.a, members=[(opts.b, "admin", "banned")])
    _login(opts, "b")
    resp = opts.client.post("/api/chat/room/member/add", {"room_id": admin_room.pk, "user_id": opts.a.pk})
    assert_eq(resp.status_code, 403, f"a banned admin cannot add, got {resp.status_code}")
    resp = opts.client.post("/api/chat/room/rules", {"room_id": admin_room.pk, "allow_urls": False})
    assert_eq(resp.status_code, 403, f"a banned admin cannot change rules, got {resp.status_code}")

    ChatMembership.objects.filter(room=admin_room, user=opts.b).update(status="muted")
    resp = opts.client.post("/api/chat/room/rules", {"room_id": admin_room.pk, "allow_urls": False})
    assert_eq(resp.status_code, 200, f"a muted admin keeps room authority, got {resp.status_code}: {resp.json}")


@th.tier("core")
@th.django_unit_test()
def test_room_create_any_group_owner_is_creator(opts):
    """REST create in J, a group A is not in, works as in 1.35.3; in G it
    works; a body `user` never changes the owner."""
    from mojo.apps.chat.models import ChatRoom
    _login(opts, "a")
    name = ROOM_PREFIX + "create-j"
    resp = opts.client.post("/api/chat/room", {"name": name, "kind": "group", "group": opts.j.pk})
    assert_eq(resp.status_code, 200, f"A may create in J, got {resp.status_code}: {resp.json}")
    room = ChatRoom.objects.get(name=name)
    assert_eq(room.group_id, opts.j.pk, "the room is in J")
    assert_eq(room.user_id, opts.a.pk, "the owner is A")

    for label, owner_field in (("real", opts.c.pk), ("missing", opts.missing_id)):
        name = f"{ROOM_PREFIX}create-g-{label}"
        resp = opts.client.post("/api/chat/room", {
            "name": name, "kind": "group", "group": opts.g.pk, "user": owner_field})
        assert_eq(resp.status_code, 200, f"A may create in G ({label} user field), got {resp.status_code}: {resp.json}")
        room = ChatRoom.objects.get(name=name)
        assert_eq(room.group_id, opts.g.pk, "the room is in G")
        assert_eq(room.user_id, opts.a.pk, f"the body's {label} user is ignored: the owner is A")
