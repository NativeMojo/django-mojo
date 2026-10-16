"""
`CHAT_ROOM_AUTHORITY_HANDLER` read from the settings file by the live server
(maestro #8347). The hook's logic is covered in-process in
`tests/test_chat/room_authority.py`; these are the end-to-end cases, here
because `th.server_settings()` reloads the shared server and so cannot run in
a parallel-safe package.
"""
from testit import helpers as th
from testit.helpers import assert_eq, assert_true

TESTIT_TIER = "extended"

PASSWORD = "TestPass1!"
PREFIX = "chat-authority-hook-"
EMAILS = {name: f"{PREFIX}{name}@example.com" for name in ("o", "m", "s")}
GROUP = f"test-{PREFIX}h"
ROOM_PREFIX = f"test-{PREFIX}"
HOOKS = "mojo.apps.chat.services.room_authority"


@th.django_unit_setup()
@th.requires_app("mojo.apps.chat")
def setup_settings_hook(opts):
    from mojo.apps.account.models import Group, User
    from mojo.apps.chat.models import ChatMembership, ChatRoom

    users = User.objects.filter(email__in=EMAILS.values())
    ChatRoom.objects.filter(name__startswith=ROOM_PREFIX).delete()
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
    opts.h.add_member(opts.m).add_permission("chat")

    opts.room = ChatRoom.objects.create(
        name=ROOM_PREFIX + "r", kind="group", user=opts.o, group=opts.h)
    ChatMembership.objects.create(room=opts.room, user=opts.o, role="owner")
    ChatMembership.objects.create(room=opts.room, user=opts.m, role="member")
    opts.channel = ChatRoom.objects.create(
        name=ROOM_PREFIX + "ch", kind="channel", group=opts.h)


def _login(opts, name):
    assert_true(opts.client.login(EMAILS[name], PASSWORD), f"login as {name} failed")


@th.django_unit_test()
def test_deny_all_refuses_every_operation(opts):
    """With deny_all set: rules, ban, join and PUT are refused, and S's DELETE
    is refused with the room kept. With the setting gone the owner's rules
    change passes again."""
    from mojo.apps.chat.models import ChatMembership, ChatRoom
    with th.server_settings(CHAT_ROOM_AUTHORITY_HANDLER=f"{HOOKS}.deny_all"):
        _login(opts, "o")
        resp = opts.client.post("/api/chat/room/rules",
                                {"room_id": opts.room.pk, "allow_urls": False})
        assert_eq(resp.status_code, 403, f"deny_all refuses rules, got {resp.status_code}: {resp.json}")
        resp = opts.client.post("/api/chat/room/member/ban",
                                {"room_id": opts.room.pk, "user_id": opts.m.pk})
        assert_eq(resp.status_code, 403, f"deny_all refuses ban, got {resp.status_code}: {resp.json}")
        resp = opts.client.put(f"/api/chat/room/{opts.room.pk}", {"name": ROOM_PREFIX + "renamed"})
        assert_eq(resp.status_code, 403, f"deny_all refuses PUT, got {resp.status_code}: {resp.json}")
        _login(opts, "m")
        resp = opts.client.post("/api/chat/room/join", {"room_id": opts.channel.pk})
        assert_eq(resp.status_code, 403, f"deny_all refuses join, got {resp.status_code}: {resp.json}")
        assert_true(not ChatMembership.objects.filter(room=opts.channel, user=opts.m).exists(),
                    "the refused join wrote no membership")
        _login(opts, "s")
        resp = opts.client.delete(f"/api/chat/room/{opts.room.pk}")
        assert_eq(resp.status_code, 403, f"deny_all refuses S's DELETE, got {resp.status_code}: {resp.json}")
        assert_true(ChatRoom.objects.filter(pk=opts.room.pk).exists(), "the room is kept")
    room = ChatRoom.objects.get(pk=opts.room.pk)
    assert_eq(room.name, ROOM_PREFIX + "r", "no refused PUT renamed the room")
    assert_eq(ChatMembership.objects.get(room=room, user=opts.m).status, "active", "M was not banned")

    _login(opts, "o")
    resp = opts.client.post("/api/chat/room/rules",
                            {"room_id": opts.room.pk, "allow_urls": False})
    assert_eq(resp.status_code, 200, f"with no hook the owner changes rules again, got {resp.status_code}: {resp.json}")


@th.django_unit_test()
def test_unloadable_hook_fails_closed(opts):
    """An unloadable path refuses even S's rules change."""
    with th.server_settings(CHAT_ROOM_AUTHORITY_HANDLER=f"{HOOKS}.no_such_hook"):
        _login(opts, "s")
        resp = opts.client.post("/api/chat/room/rules",
                                {"room_id": opts.room.pk, "allow_media": False})
        assert_eq(resp.status_code, 403, f"an unloadable hook refuses, got {resp.status_code}: {resp.json}")
