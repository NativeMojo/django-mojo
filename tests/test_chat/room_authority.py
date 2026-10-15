"""
Who may run a room-admin operation through the REST API (maestro #8347):
`room/rules`, `room/join`, `room/leave`, `room/member/remove|mute|ban` and
PUT/DELETE on `room/<pk>`.

Fixtures: O owns room R in group H, A is R's admin, M a member (with `chat`
in H), X an outsider. S holds the global manage_chat; G holds manage_chat in
H only. C is a channel with no group; CH a channel in H.

The hook (`CHAT_ROOM_AUTHORITY_HANDLER`) is exercised in-process through the
keyword-only `handler=` / `path=` seams on `room_authority.allows`: this
package is scanned strict, so no setting is patched here. The settings-file
cases live in `tests/test_chat_authority_extended_serial`.
"""
from types import SimpleNamespace

from testit import helpers as th
from testit.helpers import assert_eq, assert_true

TESTIT_TIER = "core"

PASSWORD = "TestPass1!"
PREFIX = "chat-authority-"
EMAILS = {
    name: f"{PREFIX}{name}@example.com"
    for name in ("o", "a", "m", "x", "s", "g")
}
GROUP = f"test-{PREFIX}h"
ROOM_PREFIX = f"test-{PREFIX}"
KEY_NAME = f"{PREFIX}override-key"


@th.django_unit_setup()
@th.requires_app("mojo.apps.chat")
def setup_room_authority(opts):
    from mojo.apps.account.models import ApiKey, Group, User
    from mojo.apps.chat.models import ChatRoom

    # Delete before create: this runs against a long-lived database.
    users = User.objects.filter(email__in=EMAILS.values())
    ChatRoom.objects.filter(name__startswith=ROOM_PREFIX).delete()
    ChatRoom.objects.filter(memberships__user__in=users).delete()
    ApiKey.objects.filter(name=KEY_NAME).delete()
    users.delete()
    Group.objects.filter(name=GROUP).delete()

    for name, email in EMAILS.items():
        user = User.objects.create_user(username=email, email=email, password=PASSWORD)
        user.is_email_verified = True
        user.save()
        setattr(opts, name, user)
    opts.s.add_permission("manage_chat")

    opts.h = Group.objects.create(name=GROUP)
    opts.h.add_member(opts.o).add_permission("chat")
    opts.h.add_member(opts.a).add_permission("chat")
    opts.h.add_member(opts.m).add_permission("chat")
    opts.h.add_member(opts.g).add_permission("manage_chat")
    # S is a plain member of H so the override key below is a working key
    # that assumes S; it carries no grant in H.
    opts.h.add_member(opts.s)

    opts.room = ChatRoom.objects.create(
        name=ROOM_PREFIX + "r", kind="group", user=opts.o, group=opts.h)
    opts.channel = ChatRoom.objects.create(
        name=ROOM_PREFIX + "c", kind="channel", group=None)
    opts.group_channel = ChatRoom.objects.create(
        name=ROOM_PREFIX + "ch", kind="channel", group=opts.h)

    _key, opts.override_token = ApiKey.create_for_group(
        opts.h, KEY_NAME, permissions={}, user=opts.s, override_user=True)


def _login(opts, name):
    assert_true(opts.client.login(EMAILS[name], PASSWORD), f"login as {name} failed")


def _reset(opts, rules=None, kind="group"):
    """Put R back to its starting state: O owner, A admin, M member; default
    rules unless `rules` is given."""
    from mojo.apps.chat.models import ChatMembership, ChatRoom
    from mojo.apps.chat.models.room import DEFAULT_RULES
    ChatRoom.objects.filter(pk=opts.room.pk).update(
        rules=dict(rules if rules is not None else DEFAULT_RULES),
        kind=kind, group=opts.h, user=opts.o)
    ChatMembership.objects.filter(room=opts.room).delete()
    for user, role in ((opts.o, "owner"), (opts.a, "admin"), (opts.m, "member")):
        ChatMembership.objects.create(room=opts.room, user=user, role=role, status="active")
    opts.room.refresh_from_db()


def _rules(opts):
    from mojo.apps.chat.models import ChatRoom
    return ChatRoom.objects.get(pk=opts.room.pk).rules


def _post_rules(opts, **values):
    return opts.client.post("/api/chat/room/rules", {"room_id": opts.room.pk, **values})


def _put(opts, body):
    return opts.client.put(f"/api/chat/room/{opts.room.pk}", body)


def _status(opts, user):
    from mojo.apps.chat.models import ChatMembership
    ms = ChatMembership.objects.filter(room=opts.room, user=user).first()
    return ms.status if ms else None


def _request(user, **identity):
    """The smallest request the rule reads: the caller and, for a confined
    credential, its identity."""
    return SimpleNamespace(user=user, **identity)


@th.tier("core")
@th.django_unit_test()
def test_admin_changes_allowed_rules(opts):
    """1. A room admin changes allow_urls and max_message_length."""
    _reset(opts)
    _login(opts, "a")
    resp = _post_rules(opts, allow_urls=False, max_message_length=1000)
    assert_eq(resp.status_code, 200, f"an admin may change these rules, got {resp.status_code}: {resp.json}")
    rules = _rules(opts)
    assert_eq(rules.get("allow_urls"), False, f"allow_urls saved, got {rules}")
    assert_eq(rules.get("max_message_length"), 1000, f"max_message_length saved, got {rules}")


@th.tier("core")
@th.django_unit_test()
def test_ttl_tightening_needs_global_manage_chat(opts):
    """2. Turning disappearing messages on: A and G are refused, S is allowed,
    an override key assuming S is refused."""
    for name in ("a", "g"):
        _reset(opts)
        _login(opts, name)
        resp = _post_rules(opts, disappearing_ttl=3600)
        assert_eq(resp.status_code, 403, f"{name} may not turn the TTL on, got {resp.status_code}: {resp.json}")
        assert_eq(_rules(opts).get("disappearing_ttl"), 0, f"{name}'s refused write left the TTL off")

    _reset(opts)
    resp = opts.client.post(
        "/api/chat/room/rules", {"room_id": opts.room.pk, "disappearing_ttl": 3600},
        headers={"Authorization": f"apikey {opts.override_token}"})
    assert_eq(resp.status_code, 403, f"an override key assuming S has no global grant, got {resp.status_code}: {resp.json}")
    assert_eq(_rules(opts).get("disappearing_ttl"), 0, "the override key's write left the TTL off")

    _login(opts, "s")
    resp = _post_rules(opts, disappearing_ttl=3600)
    assert_eq(resp.status_code, 200, f"S may turn the TTL on, got {resp.status_code}: {resp.json}")
    assert_eq(_rules(opts).get("disappearing_ttl"), 3600, "S's TTL saved")


@th.tier("core")
@th.django_unit_test()
def test_ttl_loosening_stays_with_admins(opts):
    """3. With R at one day, A may lengthen the TTL or turn it off, but not
    shorten it."""
    from mojo.apps.chat.models.room import DEFAULT_RULES
    day = dict(DEFAULT_RULES, disappearing_ttl=86400)
    _reset(opts, rules=day)
    _login(opts, "a")
    resp = _post_rules(opts, disappearing_ttl=172800)
    assert_eq(resp.status_code, 200, f"A may lengthen the TTL, got {resp.status_code}: {resp.json}")
    resp = _post_rules(opts, disappearing_ttl=0)
    assert_eq(resp.status_code, 200, f"A may turn the TTL off, got {resp.status_code}: {resp.json}")
    assert_eq(_rules(opts).get("disappearing_ttl"), 0, "the TTL is off")

    _reset(opts, rules=day)
    _login(opts, "a")
    resp = _post_rules(opts, disappearing_ttl=3600)
    assert_eq(resp.status_code, 403, f"A may not shorten the TTL, got {resp.status_code}: {resp.json}")
    assert_eq(_rules(opts).get("disappearing_ttl"), 86400, "the refused write left one day")


@th.tier("core")
@th.django_unit_test()
def test_rule_values_are_validated(opts):
    """4. Bad values answer 400 and change nothing, even for S; the range
    edges are accepted; rate_limit 0 is S's alone."""
    _reset(opts)
    _login(opts, "s")
    bad = {
        "disappearing_ttl": ["abc", True, -5, 1, 3599, 31536001, 10 ** 12],
        "max_message_length": [0, 4001, "long", False],
        "rate_limit": [-1, 101, "x", True],
        "allow_urls": ["maybe", 1, None],
    }
    for key, values in bad.items():
        for value in values:
            before = _rules(opts)
            resp = _post_rules(opts, **{key: value})
            assert_eq(resp.status_code, 400, f"{key}={value!r} is refused with 400, got {resp.status_code}: {resp.json}")
            assert_true(key in str((resp.json or {}).get("error", "")),
                        f"the 400 names {key}, got {resp.json}")
            assert_eq(_rules(opts), before, f"{key}={value!r} left the rules unchanged")
    for ttl in (0, 3600, 31536000):
        resp = _post_rules(opts, disappearing_ttl=ttl)
        assert_eq(resp.status_code, 200, f"disappearing_ttl={ttl} is accepted, got {resp.status_code}: {resp.json}")
        assert_eq(_rules(opts).get("disappearing_ttl"), ttl, f"disappearing_ttl={ttl} saved")

    _reset(opts)
    _login(opts, "a")
    resp = _post_rules(opts, rate_limit=0)
    assert_eq(resp.status_code, 403, f"A may not turn rate limiting off, got {resp.status_code}: {resp.json}")
    _login(opts, "s")
    resp = _post_rules(opts, rate_limit=0)
    assert_eq(resp.status_code, 200, f"S may turn rate limiting off, got {resp.status_code}: {resp.json}")


@th.tier("core")
@th.django_unit_test()
def test_legacy_bad_value_does_not_block_other_keys(opts):
    """5. A TTL stored as "abc" by server code does not stop A changing
    allow_urls."""
    from mojo.apps.chat.models.room import DEFAULT_RULES
    _reset(opts, rules=dict(DEFAULT_RULES, disappearing_ttl="abc"))
    _login(opts, "a")
    resp = _post_rules(opts, allow_urls=False)
    assert_eq(resp.status_code, 200, f"A may change another key, got {resp.status_code}: {resp.json}")
    rules = _rules(opts)
    assert_eq(rules.get("allow_urls"), False, f"allow_urls saved, got {rules}")
    assert_eq(rules.get("disappearing_ttl"), "abc", "the stored TTL is left as it was")


@th.tier("core")
@th.django_unit_test()
def test_put_room_follows_the_rule(opts):
    """6. PUT room/<pk>: safe rules pass; tightening, bad rules, kind and
    group changes, a banned or departed owner and an outsider are refused."""
    from mojo.apps.chat.models import ChatMembership
    _reset(opts)
    _login(opts, "o")
    resp = _put(opts, {"rules": {"allow_media": False}})
    assert_eq(resp.status_code, 200, f"O may change a safe rule, got {resp.status_code}: {resp.json}")
    assert_eq(_rules(opts).get("allow_media"), False, "allow_media saved")

    resp = _put(opts, {"rules": {"disappearing_ttl": 60}})
    assert_eq(resp.status_code, 400, f"a 60 s TTL is out of range, got {resp.status_code}: {resp.json}")
    resp = _put(opts, {"rules": {"disappearing_ttl": 3600}})
    assert_eq(resp.status_code, 403, f"O may not turn the TTL on, got {resp.status_code}: {resp.json}")
    resp = _put(opts, {"rules": {"__replace": True, "disappearing_ttl": "abc"}})
    assert_eq(resp.status_code, 400, f"a bad __replace is refused, got {resp.status_code}: {resp.json}")
    resp = _put(opts, {"rules": [1, 2]})
    assert_eq(resp.status_code, 400, f"a non-object rules value is refused, got {resp.status_code}: {resp.json}")
    assert_eq(_rules(opts).get("disappearing_ttl"), 0, "no refused PUT changed the TTL")
    assert_true(isinstance(_rules(opts), dict), "no refused PUT replaced the rules")

    resp = _put(opts, {"kind": "channel"})
    assert_eq(resp.status_code, 403, f"O may not change the kind, got {resp.status_code}: {resp.json}")
    resp = _put(opts, {"group": None})
    assert_eq(resp.status_code, 403, f"O may not detach the group, got {resp.status_code}: {resp.json}")
    opts.room.refresh_from_db()
    assert_eq((opts.room.kind, opts.room.group_id), ("group", opts.h.pk), "kind and group unchanged")

    ChatMembership.objects.filter(room=opts.room, user=opts.o).update(status="banned")
    resp = _put(opts, {"name": ROOM_PREFIX + "r-banned"})
    assert_eq(resp.status_code, 403, f"a banned owner loses PUT, got {resp.status_code}: {resp.json}")
    ChatMembership.objects.filter(room=opts.room, user=opts.o).delete()
    resp = _put(opts, {"name": ROOM_PREFIX + "r-gone"})
    assert_eq(resp.status_code, 403, f"an owner who left loses PUT, got {resp.status_code}: {resp.json}")

    _login(opts, "x")
    resp = _put(opts, {"name": ROOM_PREFIX + "r-x"})
    assert_eq(resp.status_code, 403, f"an outsider may not PUT, got {resp.status_code}: {resp.json}")
    opts.room.refresh_from_db()
    assert_eq(opts.room.name, ROOM_PREFIX + "r", "no refused PUT renamed the room")

    _reset(opts)
    _login(opts, "s")
    resp = _put(opts, {"kind": "channel"})
    assert_eq(resp.status_code, 200, f"S may change the kind, got {resp.status_code}: {resp.json}")
    opts.room.refresh_from_db()
    assert_eq(opts.room.kind, "channel", "S's kind change saved")
    _reset(opts)


@th.tier("core")
@th.django_unit_test()
def test_delete_room_unchanged(opts):
    """7. DELETE: the owner is refused as before; S deletes, messages go too."""
    from mojo.apps.chat.models import ChatMessage, ChatRoom
    victim = ChatRoom.objects.create(
        name=ROOM_PREFIX + "delete", kind="group", user=opts.o, group=opts.h)
    from mojo.apps.chat.models import ChatMembership
    ChatMembership.objects.create(room=victim, user=opts.o, role="owner")
    ChatMessage.objects.create(room=victim, user=opts.o, body="bye")

    _login(opts, "o")
    resp = opts.client.delete(f"/api/chat/room/{victim.pk}")
    assert_eq(resp.status_code, 403, f"an owner cannot delete, got {resp.status_code}: {resp.json}")
    assert_true(ChatRoom.objects.filter(pk=victim.pk).exists(), "the room is kept")

    _login(opts, "s")
    resp = opts.client.delete(f"/api/chat/room/{victim.pk}")
    assert_eq(resp.status_code, 200, f"S deletes, got {resp.status_code}: {resp.json}")
    assert_true(not ChatRoom.objects.filter(pk=victim.pk).exists(), "the room is gone")
    assert_eq(ChatMessage.objects.filter(room_id=victim.pk).count(), 0, "its messages went with it")


@th.tier("core")
@th.django_unit_test()
def test_join_groupless_channel_needs_global_manage_chat(opts):
    """8. A channel with no group: M and X are refused with no row written, S
    joins; an H member joins H's channel; an override session and a banned
    member are refused."""
    from mojo.apps.chat.models import ChatMembership
    ChatMembership.objects.filter(room__in=[opts.channel, opts.group_channel]).delete()
    for name, user in (("m", opts.m), ("x", opts.x)):
        _login(opts, name)
        resp = opts.client.post("/api/chat/room/join", {"room_id": opts.channel.pk})
        assert_eq(resp.status_code, 403, f"{name} may not join a groupless channel, got {resp.status_code}: {resp.json}")
        assert_true(not ChatMembership.objects.filter(room=opts.channel, user=user).exists(),
                    f"{name}'s refused join wrote no membership")

    resp = opts.client.post(
        "/api/chat/room/join", {"room_id": opts.channel.pk},
        headers={"Authorization": f"apikey {opts.override_token}"})
    assert_eq(resp.status_code, 403, f"an override session may not join, got {resp.status_code}: {resp.json}")

    _login(opts, "s")
    resp = opts.client.post("/api/chat/room/join", {"room_id": opts.channel.pk})
    assert_eq(resp.status_code, 200, f"S may join, got {resp.status_code}: {resp.json}")

    _login(opts, "m")
    resp = opts.client.post("/api/chat/room/join", {"room_id": opts.group_channel.pk})
    assert_eq(resp.status_code, 200, f"an H member joins H's channel, got {resp.status_code}: {resp.json}")
    ChatMembership.objects.filter(room=opts.group_channel, user=opts.m).update(status="banned")
    resp = opts.client.post("/api/chat/room/join", {"room_id": opts.group_channel.pk})
    assert_eq(resp.status_code, 403, f"a banned member cannot rejoin, got {resp.status_code}: {resp.json}")
    assert_eq(ChatMembership.objects.get(room=opts.group_channel, user=opts.m).status, "banned",
              "the ban stands")


@th.tier("core")
@th.django_unit_test()
def test_leave_unchanged(opts):
    """9. Leaving works as before; a direct room still answers 400."""
    from mojo.apps.chat.models import ChatMembership, ChatRoom
    ChatMembership.objects.get_or_create(
        room=opts.group_channel, user=opts.a, defaults={"role": "member", "status": "active"})
    _login(opts, "a")
    resp = opts.client.post("/api/chat/room/leave", {"room_id": opts.group_channel.pk})
    assert_eq(resp.status_code, 200, f"A may leave, got {resp.status_code}: {resp.json}")
    assert_true(not ChatMembership.objects.filter(room=opts.group_channel, user=opts.a).exists(),
                "A's membership is gone")

    direct = ChatRoom.objects.create(name=ROOM_PREFIX + "direct", kind="direct")
    ChatMembership.objects.create(room=direct, user=opts.a, role="member")
    resp = opts.client.post("/api/chat/room/leave", {"room_id": direct.pk})
    assert_eq(resp.status_code, 400, f"a direct room cannot be left, got {resp.status_code}: {resp.json}")


@th.tier("core")
@th.django_unit_test()
def test_member_operations_follow_role_order(opts):
    """10. An admin cannot act on the owner; the owner bans the admin; an
    admin mutes a member; a banned admin is refused; a group grant mutes the
    owner; an admin removes themself."""
    _reset(opts)
    _login(opts, "a")
    for op in ("remove", "mute", "ban"):
        resp = opts.client.post(f"/api/chat/room/member/{op}",
                                {"room_id": opts.room.pk, "user_id": opts.o.pk})
        assert_eq(resp.status_code, 403, f"A may not {op} the owner, got {resp.status_code}: {resp.json}")
    assert_eq(_status(opts, opts.o), "active", "the owner is untouched")

    resp = opts.client.post("/api/chat/room/member/mute",
                            {"room_id": opts.room.pk, "user_id": opts.m.pk})
    assert_eq(resp.status_code, 200, f"A may mute M, got {resp.status_code}: {resp.json}")
    assert_eq(_status(opts, opts.m), "muted", "M is muted")

    _login(opts, "o")
    resp = opts.client.post("/api/chat/room/member/ban",
                            {"room_id": opts.room.pk, "user_id": opts.a.pk})
    assert_eq(resp.status_code, 200, f"O may ban A, got {resp.status_code}: {resp.json}")
    assert_eq(_status(opts, opts.a), "banned", "A is banned")

    _login(opts, "a")
    resp = opts.client.post("/api/chat/room/member/mute",
                            {"room_id": opts.room.pk, "user_id": opts.m.pk})
    assert_eq(resp.status_code, 403, f"a banned admin has no authority, got {resp.status_code}: {resp.json}")

    _login(opts, "g")
    resp = opts.client.post("/api/chat/room/member/mute",
                            {"room_id": opts.room.pk, "user_id": opts.o.pk})
    assert_eq(resp.status_code, 200, f"a group grant mutes the owner, got {resp.status_code}: {resp.json}")
    assert_eq(_status(opts, opts.o), "muted", "the owner is muted")

    _reset(opts)
    _login(opts, "a")
    resp = opts.client.post("/api/chat/room/member/remove",
                            {"room_id": opts.room.pk, "user_id": opts.a.pk})
    assert_eq(resp.status_code, 200, f"A may remove themself, got {resp.status_code}: {resp.json}")
    assert_eq(_status(opts, opts.a), None, "A's membership is gone")
    _reset(opts)


@th.tier("core")
@th.django_unit_test()
def test_orm_writes_are_not_caught(opts):
    """11. Server code writes through the ORM with no request, and during a
    request for another room, with values stored as given."""
    from mojo.apps.chat.models import ChatMembership, ChatRoom
    from mojo.models.rest import ACTIVE_REQUEST

    def writes(label):
        room = ChatRoom.objects.create(name=ROOM_PREFIX + label, kind="group", group=opts.h)
        room.rules = {"disappearing_ttl": 30}
        room.save()
        room.refresh_from_db()
        assert_eq(room.rules.get("disappearing_ttl"), 30, f"{label}: a server-set TTL of 30 stays 30")
        ms = ChatMembership.objects.create(room=room, user=opts.x, role="member")
        ms.delete()
        ChatRoom.objects.filter(pk=room.pk).update(kind="channel")
        assert_eq(ChatRoom.objects.get(pk=room.pk).kind, "channel", f"{label}: .update(kind=...) applies")
        room.delete()
        assert_true(not ChatRoom.objects.filter(pk=room.pk).exists(), f"{label}: room.delete() applies")

    writes("orm-no-request")
    token = ACTIVE_REQUEST.set(SimpleNamespace(
        user=opts.x, method="PUT", DATA={"room_id": opts.room.pk}))
    try:
        writes("orm-in-request")
    finally:
        ACTIVE_REQUEST.reset(token)


@th.tier("core")
@th.django_unit_test()
def test_hook_seams(opts):
    """12. deny_all refuses; a pass-through equals the default; raising,
    non-True-returning and unloadable hooks refuse; a widening hook allows A's
    TTL tightening on rules but the framework's SAVE_PERMS still refuses an
    outsider on update; `changes` and `assumed_member` reach the hook."""
    from mojo.apps.chat.models import ChatRoom
    from mojo.apps.chat.services import room_authority as ra
    _reset(opts)
    room = opts.room
    tighten = {"disappearing_ttl": (0, 3600)}
    admin = _request(opts.a)
    cases = [
        ("rules", admin, {}, None),
        ("rules", admin, tighten, None),
        ("update", _request(opts.o), {"name": ("r", "r2")}, None),
        ("delete", admin, {}, None),
        ("join", _request(opts.m), {}, None),
        ("leave", admin, {}, None),
    ]
    for action, request, changes, target in cases:
        default = ra.allows(request, room, action, changes=changes, handler=None)
        assert_eq(ra.allows(request, room, action, changes=changes, handler=ra.deny_all), False,
                  f"deny_all refuses {action}")
        assert_eq(ra.allows(request, room, action, changes=changes,
                            handler=ra.default_room_authority), default,
                  f"a pass-through equals the default on {action}")

    def boom(user, room, action, context):
        raise RuntimeError("hook failure")

    for label, kwargs in (
            ("raising", {"handler": boom}),
            ("'yes'-returning", {"handler": lambda u, r, a, c: "yes"}),
            ("non-callable", {"handler": "not callable"}),
            ("unloadable", {"path": "mojo.apps.chat.services.room_authority.no_such_hook"})):
        allowed = ra.allows(admin, room, "rules", **kwargs)
        assert_eq(allowed, False, f"a {label} hook refuses")

    def widen(user, room, action, context):
        return context["default"] or user.pk == opts.a.pk or user.pk == opts.x.pk

    assert_eq(ra.allows(admin, room, "rules", changes=tighten), False,
              "by default A may not tighten the TTL")
    assert_eq(ra.allows(admin, room, "rules", changes=tighten, handler=widen), True,
              "a widening hook lets A tighten the TTL on room/rules")
    outsider = _request(opts.x)
    assert_eq(ra.allows(outsider, room, "update", handler=widen), True,
              "the widening hook passes X's update at the rule")
    framework_request = SimpleNamespace(user=opts.x, group=None, DATA={})
    assert_eq(ChatRoom.rest_check_permission(
                  framework_request, ["SAVE_PERMS", "VIEW_PERMS"], room), False,
              "but SAVE_PERMS still refuses X on room/<pk>")
    owner_request = SimpleNamespace(user=opts.o, group=None, DATA={})
    assert_eq(ChatRoom.rest_check_permission(
                  owner_request, ["SAVE_PERMS", "VIEW_PERMS"], room), True,
              "control: the same check passes the owner")

    seen = {}

    def spy(user, room, action, context):
        seen.update(context)
        return True

    assumed = _request(opts.s, api_key=SimpleNamespace(override_user=True))
    ra.allows(assumed, room, "rules", changes=tighten, handler=spy)
    assert_eq(seen.get("changes"), tighten, f"changes reach the hook, got {seen}")
    assert_eq(seen.get("assumed_member"), True, f"assumed_member reaches the hook, got {seen}")
    assert_eq(seen.get("default"), False, f"the override session's default is a refusal, got {seen}")
