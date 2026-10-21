"""
`REALTIME_SUBSCRIBE_HANDLER` read from the settings file by the live server
(maestro #8874). The hook's own matrix is covered in-process in
`tests/test_realtime/subscribe_policy.py`; these are the end-to-end cases,
here because `th.server_settings()` reloads the shared server.

The hook is `_hooks.support_only`: a player whose account row carries the
restricted flag may take part only in rooms named as support rooms. "plain"
is an ordinary groupless room, "support" a server-created groupless support
room; the player and the staff user are members of both.
"""
from testit import helpers as th
from testit.helpers import assert_eq, assert_true
from testit.ws_client import WsClient

from tests.test_chat_policy_extended_serial._hooks import RESTRICTED_KEY, SUPPORT_PREFIX

TESTIT_TIER = "extended"

PASSWORD = "TestPass1!"
PREFIX = "chat-policy-"
EMAILS = {name: f"{PREFIX}{name}@example.com" for name in ("player", "staff")}
PLAIN_PREFIX = "test-chat-policy-plain-"
ROOM_PREFIX = "test-chat-policy-"
HOOKS = "tests.test_chat_policy_extended_serial._hooks"
HOOK = f"{HOOKS}.support_only"
POLICY = "mojo.apps.realtime.policy"
QUIET = 2.0


@th.django_unit_setup()
@th.requires_app("mojo.apps.chat")
def setup_chat_policy(opts):
    from mojo.apps.account.models import User
    from mojo.apps.chat.models import ChatRoom

    users = User.objects.filter(email__in=EMAILS.values())
    ChatRoom.objects.filter(memberships__user__in=users).delete()
    ChatRoom.objects.filter(name__startswith=ROOM_PREFIX).delete()
    users.delete()
    for name, email in EMAILS.items():
        user = User.objects.create_user(username=email, email=email, password=PASSWORD)
        user.is_email_verified = True
        user.save()
        setattr(opts, name, user)


def _restrict(user, on):
    """Flip the player's restriction on the account row and commit it."""
    from mojo.apps.account.models import User
    row = User.objects.get(pk=user.pk)
    row.metadata = dict(row.metadata or {}, **{RESTRICTED_KEY: on})
    row.save(update_fields=["metadata"])


def _room(opts, label, support=False):
    """A groupless room built by server code, with the player and staff in it."""
    from mojo.apps.chat.models import ChatMembership, ChatRoom
    name = (SUPPORT_PREFIX if support else PLAIN_PREFIX) + label
    ChatRoom.objects.filter(name=name).delete()
    room = ChatRoom.objects.create(name=name, kind="group")
    for user in (opts.player, opts.staff):
        ChatMembership.objects.create(room=room, user=user, role="member")
    return room


def _message(room, user, body):
    from mojo.apps.chat.models import ChatMessage
    return ChatMessage.objects.create(room=room, user=user, body=body, kind="text")


def _token(opts, name):
    assert_true(opts.client.login(EMAILS[name], PASSWORD), f"login as {name} failed")
    return opts.client.access_token


def _socket(opts, token):
    ws = WsClient(WsClient.build_url_from_host(opts.host, path="ws/realtime/"), logger=opts.logger)
    ws.connect(timeout=10.0)
    auth = ws.authenticate(token, wait=True, timeout=10.0)
    assert_eq(auth.get("type"), "auth_success", f"socket auth failed: {auth}")
    return ws


def _subscribe(ws, topic):
    """True when the server confirms the subscribe, False on its refusal."""
    ws.send_json({"action": "subscribe", "topic": topic})
    msg = ws.wait_for_types(
        {"subscribed", "error"}, timeout=10.0,
        predicate=lambda d: d.get("topic") == topic or d.get("type") == "error").data
    return msg.get("type") == "subscribed"


def _frame(ws, kind, match=None, timeout=QUIET):
    """The first delivered `kind` frame (optionally matching), else None."""
    def check(data):
        inner = data.get("data") or {}
        return inner.get("type") == kind and (match is None or match(inner))
    try:
        return ws.wait_for_type("message", timeout=timeout, predicate=check).data["data"]
    except TimeoutError:
        return None


def _ask(ws, frame, types):
    ws.send_json(frame)
    return ws.wait_for_types(set(types), timeout=10.0).data


def _close(*sockets):
    for ws in sockets:
        ws.close()


@th.django_unit_test()
def test_subscribe_follows_the_hook(opts):
    """T1: a restricted player's subscribe to a plain room is refused, to a
    support room allowed, and their own user topic still works."""
    plain = _room(opts, "t1")
    support = _room(opts, "t1", support=True)
    _restrict(opts.player, True)
    with th.server_settings(REALTIME_SUBSCRIBE_HANDLER=HOOK):
        ws = _socket(opts, _token(opts, "player"))
        try:
            assert_true(not _subscribe(ws, plain.topic), "a restricted player may not subscribe to a plain room")
            assert_true(_subscribe(ws, support.topic), "a restricted player may subscribe to a support room")
            assert_true(_subscribe(ws, f"user:{opts.player.pk}"), "the player's own topic still works")
        finally:
            _close(ws)
        _restrict(opts.player, False)
        ws = _socket(opts, _token(opts, "player"))
        try:
            assert_true(_subscribe(ws, plain.topic), "an unrestricted player subscribes to a plain room")
        finally:
            _close(ws)


@th.django_unit_test()
def test_open_socket_stops_at_recheck_and_reconnect_is_refused(opts):
    """T2: with re-check 0, restricting a subscribed player stops plain-room
    delivery at the next frame; support keeps flowing. After disconnect_user
    the reconnect is refused the plain room and allowed the support room."""
    from mojo.apps import realtime

    plain = _room(opts, "t2")
    support = _room(opts, "t2", support=True)
    _restrict(opts.player, False)
    with th.server_settings(REALTIME_SUBSCRIBE_HANDLER=HOOK, WS_SUBSCRIPTION_RECHECK_SECONDS=0):
        ws = _socket(opts, _token(opts, "player"))
        try:
            assert_true(_subscribe(ws, plain.topic), "an unrestricted player subscribes to the plain room")
            assert_true(_subscribe(ws, support.topic), "an unrestricted player subscribes to the support room")
            realtime.publish_topic(plain.topic, {"type": "chat_message", "body": "before"})
            got = _frame(ws, "chat_message", lambda d: d.get("body") == "before", timeout=10.0)
            assert_true(got is not None, "control: the plain room delivers before the restriction")

            # One at a time: waiting for one frame drops every other frame it
            # reads past.
            _restrict(opts.player, True)
            realtime.publish_topic(plain.topic, {"type": "chat_message", "body": "after"})
            got = _frame(ws, "chat_message", lambda d: d.get("body") == "after")
            assert_true(got is None, "the plain room must stop delivering at the next re-check")
            realtime.publish_topic(support.topic, {"type": "chat_message", "body": "support after"})
            got = _frame(ws, "chat_message", lambda d: d.get("body") == "support after", timeout=10.0)
            assert_true(got is not None, "the support room keeps delivering after the restriction")

            realtime.disconnect_user("user", opts.player.pk)
        finally:
            _close(ws)
        ws = _socket(opts, _token(opts, "player"))
        try:
            assert_true(not _subscribe(ws, plain.topic), "the reconnect is refused the plain room")
            assert_true(_subscribe(ws, support.topic), "the reconnect is allowed the support room")
        finally:
            _close(ws)
    _restrict(opts.player, False)


@th.django_unit_test()
def test_client_frames_follow_the_hook(opts):
    """T3 and T8: in a plain room a restricted player's send, edit, react,
    read and typing are refused, writing and publishing nothing. In a
    server-created support room every one of them works."""
    from mojo.apps.chat.models import ChatMessage, ChatReaction, ChatReadReceipt

    plain = _room(opts, "t3")
    support = _room(opts, "t3", support=True)
    _restrict(opts.player, True)
    with th.server_settings(REALTIME_SUBSCRIBE_HANDLER=HOOK):
        player = _socket(opts, _token(opts, "player"))
        staff = _socket(opts, _token(opts, "staff"))
        try:
            assert_true(_subscribe(staff, plain.topic), "staff listens on the plain room")
            assert_true(_subscribe(staff, support.topic), "staff listens on the support room")
            for room, allowed in ((plain, False), (support, True)):
                where = "support" if allowed else "plain"
                own = _message(room, opts.player, f"own {where}")
                theirs = _message(room, opts.staff, f"theirs {where}")
                before = ChatMessage.objects.filter(room=room).count()

                got = _ask(player, {"type": "chat_message", "room_id": room.pk, "body": f"hello {where}"},
                           ("chat_message_ack", "error"))
                if allowed:
                    assert_eq(got.get("type"), "chat_message_ack", f"send works in the support room, got {got}")
                else:
                    assert_eq(got, {"type": "error", "error": "Not allowed in this room"},
                              f"send is refused in the plain room, got {got}")
                    assert_eq(ChatMessage.objects.filter(room=room).count(), before,
                              "a refused send stores nothing")

                got = _ask(player, {"type": "chat_edit", "message_id": own.pk, "body": f"edited {where}"},
                           ("chat_edit_ack", "error"))
                if allowed:
                    assert_eq(got.get("type"), "chat_edit_ack", f"edit works in the support room, got {got}")
                else:
                    assert_eq(got, {"type": "error", "error": "Cannot edit this message"},
                              f"edit is refused in the plain room, got {got}")
                    assert_eq(ChatMessage.objects.get(pk=own.pk).body, f"own {where}", "a refused edit saves nothing")

                got = _ask(player, {"type": "chat_react", "message_id": theirs.pk, "emoji": "👍", "action": "add"},
                           ("chat_react_ack", "error"))
                reacted = ChatReaction.objects.filter(message=theirs, user=opts.player).exists()
                if allowed:
                    assert_eq(got.get("type"), "chat_react_ack", f"react works in the support room, got {got}")
                    assert_true(reacted, "an allowed reaction is stored")
                else:
                    assert_eq(got, {"type": "error", "error": "Message not found"},
                              f"react is refused in the plain room with the uniform answer, got {got}")
                    assert_true(not reacted, "a refused reaction stores nothing")

                got = _ask(player, {"type": "chat_read", "room_id": room.pk, "up_to_message_id": theirs.pk},
                           ("chat_read_ack", "error"))
                receipt = ChatReadReceipt.objects.filter(message=theirs, user=opts.player).exists()
                if allowed:
                    assert_eq(got.get("type"), "chat_read_ack", f"read works in the support room, got {got}")
                    assert_true(receipt, "an allowed read stores its receipt")
                else:
                    assert_eq(got, {"type": "error", "error": "Not allowed in this room"},
                              f"read is refused in the plain room, got {got}")
                    assert_true(not receipt, "a refused read stores no receipt")

                staff.clear_messages()
                player.send_json({"type": "chat_typing", "room_id": room.pk})
                typing = _frame(staff, "chat_typing",
                                lambda d: d.get("user_id") == opts.player.pk and d.get("room_id") == room.pk,
                                timeout=10.0 if allowed else QUIET)
                if allowed:
                    assert_true(typing is not None, "typing is broadcast in the support room")
                else:
                    assert_true(typing is None, "typing is not broadcast in the plain room")
        finally:
            _close(player, staff)
    _restrict(opts.player, False)


@th.django_unit_test()
def test_rest_room_reads_follow_the_hook(opts):
    """T4 and T8: the four REST room reads answer 403 for a restricted player
    in a plain room and 200 in a support room."""
    from mojo.apps.chat.models import ChatMessage

    plain = _room(opts, "t4")
    support = _room(opts, "t4", support=True)
    for room in (plain, support):
        _message(room, opts.staff, "hello")
    _restrict(opts.player, True)
    with th.server_settings(REALTIME_SUBSCRIBE_HANDLER=HOOK):
        _token(opts, "player")
        for room, status in ((plain, 403), (support, 200)):
            up_to = ChatMessage.objects.filter(room=room).order_by("-pk").first().pk
            calls = (
                ("GET room/messages", lambda: opts.client.get("/api/chat/room/messages", params={"room_id": room.pk})),
                ("GET room/online", lambda: opts.client.get("/api/chat/room/online", params={"room_id": room.pk})),
                ("GET room/members", lambda: opts.client.get("/api/chat/room/members", params={"room_id": room.pk})),
                ("POST room/read", lambda: opts.client.post(
                    "/api/chat/room/read", {"room_id": room.pk, "up_to_message_id": up_to})),
            )
            for name, call in calls:
                resp = call()
                assert_eq(resp.status_code, status,
                          f"{name} for {room.name} answers {status}, got {resp.status_code}: {resp.json}")
    _restrict(opts.player, False)


@th.django_unit_test()
def test_existing_dm_reuse_follows_the_hook(opts):
    """T5: a restricted player asking for a DM they already share gets the
    uniform 404 and no room is created; unrestricted, the same room comes
    back as before (#8345)."""
    from mojo.apps.chat.models import ChatMembership, ChatRoom

    ChatRoom.objects.filter(kind="direct", memberships__user=opts.player).delete()
    room = ChatRoom.objects.create(kind="direct", user=opts.staff)
    ChatMembership.objects.create(room=room, user=opts.staff, role="owner")
    ChatMembership.objects.create(room=room, user=opts.player, role="member")
    _restrict(opts.player, True)
    with th.server_settings(REALTIME_SUBSCRIBE_HANDLER=HOOK):
        _token(opts, "player")
        resp = opts.client.post("/api/chat/dm", {"user_id": opts.staff.pk})
        assert_eq(resp.status_code, 404, f"a refused DM reuse answers 404, got {resp.status_code}: {resp.json}")
        assert_eq(resp.json.error, "User not found", "with the uniform refusal")
        count = ChatRoom.objects.filter(kind="direct", memberships__user=opts.player).count()
        assert_eq(count, 1, "a refused DM reuse creates no room")

        _restrict(opts.player, False)
        resp = opts.client.post("/api/chat/dm", {"user_id": opts.staff.pk})
        assert_eq(resp.status_code, 200, f"an unrestricted player gets the DM back, got {resp.status_code}: {resp.json}")
        assert_eq(resp.json.data.id, room.pk, "the existing DM is reused")


@th.django_unit_test()
def test_room_create_stays_open_but_the_room_is_unusable(opts):
    """T7: a restricted player may still create a room through REST, but the
    hook refuses them its topic, its history and sending into it."""
    from mojo.apps.chat.models import ChatRoom

    name = PLAIN_PREFIX + "t7-created"
    ChatRoom.objects.filter(name=name).delete()
    _restrict(opts.player, True)
    with th.server_settings(REALTIME_SUBSCRIBE_HANDLER=HOOK):
        token = _token(opts, "player")
        resp = opts.client.post("/api/chat/room", {"name": name, "kind": "group"})
        assert_eq(resp.status_code, 200, f"room create stays open, got {resp.status_code}: {resp.json}")
        room = ChatRoom.objects.get(pk=resp.json.data.id)
        resp = opts.client.get("/api/chat/room/messages", params={"room_id": room.pk})
        assert_eq(resp.status_code, 403, f"the creator may not read the new room, got {resp.status_code}")
        ws = _socket(opts, token)
        try:
            assert_true(not _subscribe(ws, room.topic), "the creator may not subscribe to the new room")
            got = _ask(ws, {"type": "chat_message", "room_id": room.pk, "body": "mine"},
                       ("chat_message_ack", "error"))
            assert_eq(got, {"type": "error", "error": "Not allowed in this room"},
                      f"the creator may not send into the new room, got {got}")
        finally:
            _close(ws)
    _restrict(opts.player, False)


@th.django_unit_test()
def test_unset_hook_changes_nothing(opts):
    """T9: with the setting unset, a player carrying the restricted flag
    subscribes, sends, reads history and gets the DM back as today."""
    from mojo.apps.chat.models import ChatMembership, ChatRoom

    plain = _room(opts, "t9")
    ChatRoom.objects.filter(kind="direct", memberships__user=opts.player).delete()
    dm = ChatRoom.objects.create(kind="direct", user=opts.staff)
    ChatMembership.objects.create(room=dm, user=opts.staff, role="owner")
    ChatMembership.objects.create(room=dm, user=opts.player, role="member")
    _restrict(opts.player, True)
    try:
        token = _token(opts, "player")
        ws = _socket(opts, token)
        try:
            assert_true(_subscribe(ws, plain.topic), "with no hook the plain room subscribes")
            got = _ask(ws, {"type": "chat_message", "room_id": plain.pk, "body": "no hook"},
                       ("chat_message_ack", "error"))
            assert_eq(got.get("type"), "chat_message_ack", f"with no hook send works, got {got}")
        finally:
            _close(ws)
        resp = opts.client.get("/api/chat/room/messages", params={"room_id": plain.pk})
        assert_eq(resp.status_code, 200, f"with no hook history reads, got {resp.status_code}")
        resp = opts.client.post("/api/chat/dm", {"user_id": opts.staff.pk})
        assert_eq(resp.status_code, 200, f"with no hook the DM comes back, got {resp.status_code}: {resp.json}")
        assert_eq(resp.json.data.id, dm.pk, "with no hook the existing DM is reused")
    finally:
        _restrict(opts.player, False)


@th.django_unit_test()
def test_broken_or_deny_all_hook_fails_closed(opts):
    """T10: a path that does not load, and the shipped deny_all, refuse chat
    for everyone; the user's own topic keeps working."""
    from mojo.apps import realtime

    support = _room(opts, "t10", support=True)
    _restrict(opts.player, False)
    for value in (f"{HOOKS}.no_such_hook", f"{POLICY}.deny_all"):
        with th.server_settings(REALTIME_SUBSCRIBE_HANDLER=value):
            ws = _socket(opts, _token(opts, "player"))
            try:
                assert_true(not _subscribe(ws, support.topic), f"{value} refuses chat subscribe")
                got = _ask(ws, {"type": "chat_message", "room_id": support.pk, "body": "closed"},
                           ("chat_message_ack", "error"))
                assert_eq(got, {"type": "error", "error": "Not allowed in this room"},
                          f"{value} refuses chat send, got {got}")
                # The own-topic subscribe made at connect is server-chosen and
                # never asks the hook, so the user's own channel still delivers.
                realtime.publish_topic(f"user:{opts.player.pk}", {"type": "notice", "body": value})
                got = _frame(ws, "notice", lambda d: d.get("body") == value, timeout=10.0)
                assert_true(got is not None, f"{value} keeps the user's own topic delivering")
            finally:
                _close(ws)
