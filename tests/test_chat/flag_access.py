"""
Flagging a message: who may, by membership status (maestro #11520).

Policy:
- A room admin flags on the strength of their membership row. A banned or
  removed admin is refused. A muted admin keeps the right, as with the REST
  moderator check: a mute stops content, not moderation.
- A group or global moderator (`moderate_chat` or `manage_chat`, in the
  room's group or globally, as on REST) flags on their own grant. Their room membership,
  or the lack of one, does not change that.
- A refusal saves nothing and publishes nothing.

`chat_flag` reaches the handler from any authenticated socket, subscribed to
the room or not, so the socket test flags without subscribing: the handler's
own check is the only gate. The matrix calls `_handle_flag` directly with the
`publisher=` seam.
"""
from testit import helpers as th
from testit.helpers import assert_eq, assert_true
from testit.ws_client import WsClient

TESTIT_TIER = "core"

PASSWORD = "TestPass1!"
PREFIX = "chat-flag-access-"
EMAILS = {name: f"{PREFIX}{name}@example.com"
          for name in ("author", "listener", "admin", "groupmod", "groupmgr", "staff", "globalmod")}
ROOM_PREFIX = f"test-{PREFIX}"
GROUP_NAME = f"test-{PREFIX}group"
REFUSED = {"type": "error", "error": "Permission denied"}


@th.django_unit_setup()
@th.requires_app("mojo.apps.chat")
def setup_flag_access(opts):
    from mojo.apps.account.models import Group, User
    from mojo.apps.chat.models import ChatRoom

    ChatRoom.objects.filter(name__startswith=ROOM_PREFIX).delete()
    User.objects.filter(email__in=EMAILS.values()).delete()
    Group.objects.filter(name=GROUP_NAME).delete()
    for name, email in EMAILS.items():
        user = User.objects.create_user(username=email, email=email, password=PASSWORD)
        user.is_email_verified = True
        user.save()
        setattr(opts, name, user)
    opts.staff.add_permission("manage_chat")
    opts.globalmod.add_permission("moderate_chat")
    opts.group = Group.objects.create(name=GROUP_NAME)
    opts.group.add_member(opts.groupmod).add_permission("moderate_chat")
    opts.group.add_member(opts.groupmgr).add_permission("manage_chat")
    # Room members hold the group's plain "chat" grant, so they can subscribe.
    for user in (opts.author, opts.listener, opts.admin):
        opts.group.add_member(user).add_permission("chat")


def _room(opts, label):
    """A room in the test group: author and listener are members, admin is a
    room admin. The group and global moderators have no row yet."""
    from mojo.apps.chat.models import ChatMembership, ChatRoom
    room = ChatRoom.objects.create(name=ROOM_PREFIX + label, kind="group", group=opts.group)
    for user, role in ((opts.author, "member"), (opts.listener, "member"), (opts.admin, "admin")):
        ChatMembership.objects.create(room=room, user=user, role=role)
    return room


def _message(room, user, body):
    from mojo.apps.chat.models import ChatMessage
    return ChatMessage.objects.create(room=room, user=user, body=body, kind="text")


def _set_row(room, user, status, role="admin"):
    """status None removes the row."""
    from mojo.apps.chat.models import ChatMembership
    if status is None:
        ChatMembership.objects.filter(room=room, user=user).delete()
        return
    ChatMembership.objects.update_or_create(
        room=room, user=user, defaults={"status": status, "role": role})


def _flagged(msg):
    from mojo.apps.chat.models import ChatMessage
    stored = ChatMessage.objects.get(pk=msg.pk)
    return stored.is_flagged, stored.flagged_by_id, stored.flagged_at


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


def _flag(ws, message_id):
    ws.send_json({"type": "chat_flag", "message_id": message_id})
    return ws.wait_for_types({"chat_flag_ack", "error"}, timeout=10.0).data


def _frame(ws, kind, message_id, timeout):
    def match(data):
        inner = data.get("data") or {}
        return inner.get("type") == kind and inner.get("message_id") == message_id
    try:
        return ws.wait_for_type("message", timeout=timeout, predicate=match).data["data"]
    except TimeoutError:
        return None


@th.tier("core")
@th.django_unit_test()
def test_flag_over_socket_by_admin_status(opts):
    """An unsubscribed room admin's socket: banned and removed are refused
    with nothing saved or sent; active and muted flag and broadcast."""
    room = _room(opts, "socket")
    admin = _socket(opts, _token(opts, "admin"))
    listener = _socket(opts, _token(opts, "listener"), topic=room.topic)
    try:
        for status in ("banned", None):
            label = status or "removed"
            msg = _message(room, opts.author, f"flag by {label}")
            _set_row(room, opts.admin, status)
            got = _flag(admin, msg.pk)
            assert_eq(got, REFUSED, f"a {label} admin's flag is refused, got {got}")
            assert_eq(_flagged(msg), (False, None, None), f"a {label} admin's flag saves nothing")
            assert_true(_frame(listener, "chat_message_flagged", msg.pk, timeout=1.5) is None,
                        f"a {label} admin's flag is not broadcast")

        for status in ("active", "muted"):
            _set_row(room, opts.admin, status)
            msg = _message(room, opts.author, f"flag by {status}")
            got = _flag(admin, msg.pk)
            assert_eq(got.get("type"), "chat_flag_ack", f"a {status} admin flags, got {got}")
            flagged, by, _ = _flagged(msg)
            assert_true(flagged and by == opts.admin.pk, f"a {status} admin's flag is saved")
            assert_true(_frame(listener, "chat_message_flagged", msg.pk, timeout=10.0) is not None,
                        f"a {status} admin's flag is broadcast")
    finally:
        admin.close()
        listener.close()


@th.tier("core")
@th.django_unit_test()
def test_flag_role_matrix(opts):
    """Direct calls over every membership status for each kind of flagger."""
    from mojo.apps.chat.handler import _handle_flag
    room = _room(opts, "matrix")

    def attempt(user):
        msg = _message(room, opts.author, "target")
        sent = []
        out = _handle_flag(user, {"message_id": msg.pk},
                           publisher=lambda topic, data: sent.append(data))
        return out, sent, msg

    def refused(user, label):
        out, sent, msg = attempt(user)
        assert_eq(out, REFUSED, f"{label} is refused, got {out}")
        assert_eq(sent, [], f"{label}: nothing is published")
        assert_eq(_flagged(msg), (False, None, None), f"{label}: nothing is saved")

    def allowed(user, label):
        out, sent, msg = attempt(user)
        assert_eq(out.get("type"), "chat_flag_ack", f"{label} flags, got {out}")
        assert_eq(len(sent), 1, f"{label}: the flag is published once")
        flagged, by, _ = _flagged(msg)
        assert_true(flagged and by == user.pk, f"{label}: the flag is saved as theirs")

    # Room admin: a banned or removed row grants nothing; active and muted do.
    for status in ("banned", None):
        _set_row(room, opts.admin, status)
        refused(opts.admin, f"a {status or 'removed'} room admin")
    for status in ("active", "muted"):
        _set_row(room, opts.admin, status)
        allowed(opts.admin, f"a {status} room admin")

    # The author and an active plain member hold no moderator right.
    refused(opts.author, "the author as a plain member")
    refused(opts.listener, "an active plain member")

    # Group and global moderators flag on their own grants, whatever their row.
    for user, label in ((opts.groupmod, "a group moderate_chat holder"),
                        (opts.groupmgr, "a group manage_chat holder"),
                        (opts.globalmod, "a global moderate_chat holder"),
                        (opts.staff, "a global manage_chat holder")):
        for status in (None, "active", "muted", "banned"):
            _set_row(room, user, status, role="member")
            allowed(user, f"{label} with {'no' if status is None else 'a ' + status} room row")
