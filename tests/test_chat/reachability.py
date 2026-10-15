"""
Who a caller may put in a chat with them (maestro #8345): `POST dm`,
`POST room/member/add` and the room-create door.

Fixtures: A and B share group G; A and D share group H, D is in no other
group; C shares nothing with anyone; Z is inactive (shares G); S holds the
global manage_chat. Group-room integrity: a G room only takes members of G.

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
    # A may chat in G (room create); B is a plain member.
    opts.g.add_member(opts.a).add_permission("chat")
    opts.g.add_member(opts.b)
    opts.g.add_member(opts.z)
    opts.h.add_member(opts.a)
    opts.h.add_member(opts.d)
    opts.missing_id = (User.objects.order_by("-pk").values_list("pk", flat=True).first() or 0) + 100000


def _login(opts, name):
    assert_true(opts.client.login(EMAILS[name], PASSWORD), f"login as {name} failed")


def _counts():
    from mojo.apps.chat.models import ChatMembership, ChatMessage, ChatRoom
    return (ChatRoom.objects.count(), ChatMembership.objects.count(), ChatMessage.objects.count())


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
    """Unrelated, missing, own, absent, non-numeric, zero and inactive ids all
    get the one answer the missing id gets, and nothing is written."""
    _drop_direct(opts.a)
    _login(opts, "a")
    expected = _answer(opts.client.post("/api/chat/dm", {"user_id": opts.missing_id}))
    assert_eq(expected[0], 404, f"a missing id is refused with 404, got {expected}")
    assert_eq(expected[1].get("error"), "User not found", f"refusal body, got {expected}")

    before = _counts()
    cases = {
        "unrelated": {"user_id": opts.c.pk},
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
def test_dm_reuses_existing_room_without_rule(opts):
    """A pair that already shares a direct room gets it back even when the
    rule would not let them open a new one."""
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
def test_dm_global_manage_chat_reaches_anyone(opts):
    """S holds the global manage_chat: S may DM C, who shares nothing."""
    _drop_direct(opts.s, opts.c)
    _login(opts, "s")
    resp = opts.client.post("/api/chat/dm", {"user_id": opts.c.pk})
    assert_eq(resp.status_code, 200, f"global manage_chat reaches C, got {resp.status_code}: {resp.json}")


@th.tier("core")
@th.django_unit_test()
def test_override_session_gets_no_bypass(opts):
    """A credential that assumes S never inherits S's global reach."""
    from mojo.apps.chat.services import reachability
    plain = _request(opts.s)
    assumed = _request(opts.s, api_key=SimpleNamespace(override_user=True))
    got = reachability.reachable_user(plain, opts.c.pk, handler=None)
    assert_eq(getattr(got, "pk", None), opts.c.pk, "S's own session reaches C")
    assert_true(reachability.reachable_user(assumed, opts.c.pk, handler=None) is None,
                "an assumed-member session must not reach C through S's global grant")


@th.tier("core")
@th.django_unit_test()
def test_hook_decides_and_fails_closed(opts):
    """The hook replaces the default rule; anything but True refuses."""
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
    got = reach(req, opts.b.pk, path="mojo.apps.chat.services.reachability.default_can_reach")
    assert_eq(getattr(got, "pk", None), opts.b.pk, "a hook path loads and answers (the default rule)")

    room = _room(opts, "hook-g", opts.a, group=opts.g)
    assert_true(reach(req, opts.c.pk, room=room, handler=reachability.allow_all) is None,
                "no hook can put an outsider in a group room")


@th.tier("core")
@th.django_unit_test()
def test_member_add_groupless_room(opts):
    """A's groupless room: B (shares G) is added; every other target gets the
    uniform refusal and nothing is written."""
    from mojo.apps.chat.models import ChatMembership
    room = _room(opts, "add-groupless", opts.a)
    _login(opts, "a")
    expected = _answer(opts.client.post("/api/chat/room/member/add",
                                        {"room_id": room.pk, "user_id": opts.missing_id}))
    assert_eq(expected[0], 404, f"a missing id is refused with 404, got {expected}")
    assert_eq(expected[1].get("error"), "User not found", f"refusal body, got {expected}")

    before = _counts()
    for label, extra in {"unrelated": {"user_id": opts.c.pk}, "own id": {"user_id": opts.a.pk},
                         "absent": {}, "inactive": {"user_id": opts.z.pk},
                         "non-numeric": {"user_id": "abc"}}.items():
        got = _answer(opts.client.post("/api/chat/room/member/add", {"room_id": room.pk, **extra}))
        assert_eq(got, expected, f"adding {label} must answer exactly like a missing id: {got}")
    assert_eq(_counts(), before, "a refused add writes no membership and no system message")

    resp = opts.client.post("/api/chat/room/member/add", {"room_id": room.pk, "user_id": opts.b.pk})
    assert_eq(resp.status_code, 200, f"A may add B, got {resp.status_code}: {resp.json}")
    assert_true(ChatMembership.objects.filter(room=room, user=opts.b, status="active").exists(),
                "B is now an active member")


@th.tier("core")
@th.django_unit_test()
def test_member_add_group_room_integrity(opts):
    """A G room takes members of G only: A shares H with D, yet cannot add D
    to it; even S's global grant cannot add C."""
    from mojo.apps.chat.models import ChatMembership
    room = _room(opts, "add-g", opts.a, group=opts.g)
    _login(opts, "a")
    resp = opts.client.post("/api/chat/room/member/add", {"room_id": room.pk, "user_id": opts.d.pk})
    assert_eq(resp.status_code, 404, f"D is outside G: refused, got {resp.status_code}: {resp.json}")
    assert_eq(resp.json.get("error"), "User not found", "the uniform refusal")
    assert_true(not ChatMembership.objects.filter(room=room, user=opts.d).exists(), "D was not added")

    resp = opts.client.post("/api/chat/room/member/add", {"room_id": room.pk, "user_id": opts.b.pk})
    assert_eq(resp.status_code, 200, f"B is in G: added, got {resp.status_code}: {resp.json}")

    _login(opts, "s")
    resp = opts.client.post("/api/chat/room/member/add", {"room_id": room.pk, "user_id": opts.c.pk})
    assert_eq(resp.status_code, 404, f"global manage_chat cannot add an outsider to a G room, got {resp.status_code}")
    assert_true(not ChatMembership.objects.filter(room=room, user=opts.c).exists(), "C was not added")


@th.tier("core")
@th.django_unit_test()
def test_member_add_owner_outside_group_refused(opts):
    """A room already in a group its owner is not a member of (the old
    cross-group create) gives that owner no way to add the group's members."""
    room = _room(opts, "add-foreign", opts.c, group=opts.g)
    _login(opts, "c")
    resp = opts.client.post("/api/chat/room/member/add", {"room_id": room.pk, "user_id": opts.b.pk})
    assert_eq(resp.status_code, 404, f"an owner outside G cannot add B, got {resp.status_code}: {resp.json}")


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
def test_room_create_needs_the_group(opts):
    """REST create in a group the caller cannot chat in is refused with no
    row; in G it works; a body `user` never changes the owner."""
    from mojo.apps.chat.models import ChatRoom
    _login(opts, "a")
    name = ROOM_PREFIX + "create-j"
    resp = opts.client.post("/api/chat/room", {"name": name, "kind": "group", "group": opts.j.pk})
    assert_eq(resp.status_code, 403, f"A is not in J: refused, got {resp.status_code}: {resp.json}")
    assert_true(not ChatRoom.objects.filter(name=name).exists(), "no room was created in J")

    for label, owner_field in (("real", opts.c.pk), ("missing", opts.missing_id)):
        name = f"{ROOM_PREFIX}create-g-{label}"
        resp = opts.client.post("/api/chat/room", {
            "name": name, "kind": "group", "group": opts.g.pk, "user": owner_field})
        assert_eq(resp.status_code, 200, f"A may create in G ({label} user field), got {resp.status_code}: {resp.json}")
        room = ChatRoom.objects.get(name=name)
        assert_eq(room.group_id, opts.g.pk, "the room is in G")
        assert_eq(room.user_id, opts.a.pk, f"the body's {label} user is ignored: the owner is A")
