"""
Editing a message needs an active membership, the same bar as sending
(maestro #8345, WMWX #4532 part 1).

The author branch of `chat_edit` used to skip the membership check, so a
removed, banned or muted author could still rewrite their message and have it
broadcast. Tests 1-3 run over a real WebSocket against the live test server;
the role matrix calls `_handle_edit` directly with the `publisher=` seam.

Also here: a server-created groupless room whose members share no group (the
WMWX support-room shape) still lets them send, receive, edit and see who is
online — the reachability rule governs only the client doors.
"""
import time

from testit import helpers as th
from testit.helpers import assert_eq, assert_true
from testit.ws_client import WsClient

TESTIT_TIER = "core"

PASSWORD = "TestPass1!"
PREFIX = "chat-edit-access-"
EMAILS = {name: f"{PREFIX}{name}@example.com" for name in ("author", "listener", "admin", "staff")}
ROOM_PREFIX = f"test-{PREFIX}"
REFUSED = {"type": "error", "error": "Cannot edit this message"}


@th.django_unit_setup()
@th.requires_app("mojo.apps.chat")
def setup_edit_access(opts):
    from mojo.apps.account.models import User
    from mojo.apps.chat.models import ChatRoom

    ChatRoom.objects.filter(name__startswith=ROOM_PREFIX).delete()
    User.objects.filter(email__in=EMAILS.values()).delete()
    for name, email in EMAILS.items():
        user = User.objects.create_user(username=email, email=email, password=PASSWORD)
        user.is_email_verified = True
        user.save()
        setattr(opts, name, user)
    opts.staff.add_permission("manage_chat")


def _room(opts, label):
    """A groupless room built by server code: author and listener are plain
    members, admin is a room admin. They share no group."""
    from mojo.apps.chat.models import ChatMembership, ChatRoom
    room = ChatRoom.objects.create(name=ROOM_PREFIX + label, kind="group")
    for user, role in ((opts.author, "member"), (opts.listener, "member"), (opts.admin, "admin")):
        ChatMembership.objects.create(room=room, user=user, role=role)
    return room


def _message(room, user, body):
    from mojo.apps.chat.models import ChatMessage
    return ChatMessage.objects.create(room=room, user=user, body=body, kind="text")


def _token(opts, name):
    assert_true(opts.client.login(EMAILS[name], PASSWORD), f"login as {name} failed")
    return opts.client.access_token


def _socket(opts, token, topic=None):
    ws = WsClient(WsClient.build_url_from_host(opts.host, path="ws/realtime/"), logger=opts.logger)
    ws.connect(timeout=10.0)
    auth = ws.authenticate(token, wait=True, timeout=10.0)
    assert_eq(auth.get("type"), "auth_success", f"socket auth failed: {auth}")
    if topic:
        sub = ws.subscribe(topic, wait=True, timeout=5.0)
        assert_eq(sub.get("type"), "subscribed", f"subscribe to {topic} failed: {sub}")
    return ws


def _edit(ws, message_id, body):
    ws.send_json({"type": "chat_edit", "message_id": message_id, "body": body})
    return ws.wait_for_types({"chat_edit_ack", "error"}, timeout=10.0).data


def _frame(ws, kind, message_id, timeout):
    """The `kind` frame for `message_id` on a subscribed socket, else None."""
    def match(data):
        inner = data.get("data") or {}
        return inner.get("type") == kind and inner.get("message_id") == message_id
    try:
        return ws.wait_for_type("message", timeout=timeout, predicate=match).data["data"]
    except TimeoutError:
        return None


def _set_status(room, user, status):
    from mojo.apps.chat.models import ChatMembership
    if status is None:
        ChatMembership.objects.filter(room=room, user=user).delete()
        return
    ChatMembership.objects.update_or_create(
        room=room, user=user, defaults={"status": status, "role": "member"})


@th.tier("core")
@th.django_unit_test()
def test_edit_over_socket_by_status(opts):
    """Active author: acked, saved, broadcast. Then removed (while the socket
    stays open), banned and muted: each refused, nothing saved, nothing sent."""
    from mojo.apps.chat.models import ChatMessage
    room = _room(opts, "socket")
    author_token = _token(opts, "author")
    listener_token = _token(opts, "listener")
    author = _socket(opts, author_token)
    listener = _socket(opts, listener_token, topic=room.topic)
    try:
        first = _message(room, opts.author, "first")
        ack = _edit(author, first.pk, "first, edited")
        assert_eq(ack.get("type"), "chat_edit_ack", f"an active author's edit is acked, got {ack}")
        assert_eq(ChatMessage.objects.get(pk=first.pk).body, "first, edited", "the edit is saved")
        frame = _frame(listener, "chat_message_edited", first.pk, timeout=10.0)
        assert_true(frame is not None, "the listener receives the edit (positive control)")

        for status in (None, "banned", "muted"):
            label = status or "removed"
            msg = _message(room, opts.author, f"before {label}")
            _set_status(room, opts.author, status)
            got = _edit(author, msg.pk, f"after {label}")
            assert_eq(got, REFUSED, f"a {label} author's edit is refused, got {got}")
            stored = ChatMessage.objects.get(pk=msg.pk)
            assert_eq(stored.body, f"before {label}", f"a {label} author's edit saves nothing")
            assert_true(stored.edited_at is None, f"a {label} author's edit sets no edited_at")
            assert_true(_frame(listener, "chat_message_edited", msg.pk, timeout=1.5) is None,
                        f"a {label} author's edit is not broadcast")
            _set_status(room, opts.author, "active")
    finally:
        author.close()
        listener.close()


@th.tier("core")
@th.django_unit_test()
def test_edit_role_matrix(opts):
    """Direct calls: a banned or muted admin, a banned author-admin and a
    global manage_chat holder with no row are refused; an active admin edits."""
    from mojo.apps.chat.handler import _handle_edit
    from mojo.apps.chat.models import ChatMembership, ChatMessage
    room = _room(opts, "matrix")

    def attempt(user, msg, body):
        sent = []
        out = _handle_edit(user, {"message_id": msg.pk, "body": body},
                           publisher=lambda topic, data: sent.append(data))
        return out, sent

    msg = _message(room, opts.author, "original")
    for status in ("banned", "muted"):
        ChatMembership.objects.filter(room=room, user=opts.admin).update(status=status)
        out, sent = attempt(opts.admin, msg, f"by {status} admin")
        assert_eq(out, REFUSED, f"a {status} admin is refused, got {out}")
        assert_eq(sent, [], f"a {status} admin's edit publishes nothing")
    assert_eq(ChatMessage.objects.get(pk=msg.pk).body, "original", "refused edits saved nothing")

    ChatMembership.objects.filter(room=room, user=opts.admin).update(status="active")
    out, sent = attempt(opts.admin, msg, "by active admin")
    assert_eq(out.get("type"), "chat_edit_ack", f"an active admin edits, got {out}")
    assert_eq(len(sent), 1, "an accepted edit publishes once")

    own = _message(room, opts.author, "mine")
    ChatMembership.objects.filter(room=room, user=opts.author).update(role="admin", status="banned")
    out, sent = attempt(opts.author, own, "mine, edited")
    assert_eq(out, REFUSED, f"a banned author-admin is refused on their own message, got {out}")
    assert_eq(sent, [], "and nothing is published")

    out, sent = attempt(opts.staff, msg, "by staff")
    assert_eq(out, REFUSED, f"global manage_chat with no row is refused, got {out}")
    out, sent = attempt(opts.listener, msg, "by listener")
    assert_eq(out, REFUSED, f"an active non-author non-admin is refused, got {out}")
    assert_eq(sent, [], "and nothing is published")


@th.tier("core")
@th.django_unit_test()
def test_server_created_groupless_room_still_works(opts):
    """Members a server added to a groupless room, sharing no group, still
    send, receive, edit and see each other online."""
    room = _room(opts, "support")
    author = _socket(opts, _token(opts, "author"), topic=room.topic)
    listener = _socket(opts, _token(opts, "listener"), topic=room.topic)
    try:
        author.send_json({"type": "chat_message", "room_id": room.pk, "body": "hello support"})
        ack = author.wait_for_types({"chat_message_ack", "error"}, timeout=10.0).data
        assert_eq(ack.get("type"), "chat_message_ack", f"the member sends, got {ack}")
        got = _frame(listener, "chat_message", ack["message_id"], timeout=10.0)
        assert_true(got is not None, "the other member receives it")

        edit = _edit(author, ack["message_id"], "hello support, edited")
        assert_eq(edit.get("type"), "chat_edit_ack", f"the member edits, got {edit}")
        assert_true(_frame(listener, "chat_message_edited", ack["message_id"], timeout=10.0) is not None,
                    "the other member receives the edit")

        assert_true(opts.client.login(EMAILS["listener"], PASSWORD), "listener login failed")
        online = []
        end = time.time() + 5
        while time.time() < end:
            resp = opts.client.get("/api/chat/room/online", params={"room_id": room.pk})
            assert_eq(resp.status_code, 200, f"a member reads who is online, got {resp.status_code}")
            online = [row["user_id"] for row in resp.json.data]
            if opts.author.pk in online:
                break
            time.sleep(0.2)
        assert_true(opts.author.pk in online, f"the connected author shows online, got {online}")
    finally:
        author.close()
        listener.close()
