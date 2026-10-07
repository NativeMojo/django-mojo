"""Chat delivery: access is checked once per subscription and remembered, and
loss of access still stops delivery on an open socket — within one frame when
the change publishes a frame, at the next re-check when it does not."""
import asyncio
import concurrent.futures
import threading
from contextlib import ExitStack, contextmanager
from types import SimpleNamespace

from testit import helpers as th

TESTIT_TIER = "core"


def _capture():
    """Capture SQL on every configured database alias for the calling thread."""
    from django.db import connections
    from django.test.utils import CaptureQueriesContext

    stack = ExitStack()
    contexts = [stack.enter_context(CaptureQueriesContext(connections[alias]))
                for alias in connections]
    return stack, contexts


class _Counter:
    def __init__(self):
        self.count = 0

    def add(self, contexts):
        self.count += sum(len(ctx.captured_queries) for ctx in contexts)


class _CountingExecutor(concurrent.futures.ThreadPoolExecutor):
    """The handler sends authorization work to the loop's executor; count the
    SQL that work issues on the executor thread."""

    def __init__(self, counter):
        super().__init__(max_workers=1)
        self.counter = counter

    def submit(self, fn, /, *args, **kwargs):
        counter = self.counter

        def counted():
            stack, contexts = _capture()
            try:
                with stack:
                    return fn(*args, **kwargs)
            finally:
                counter.add(contexts)
        return super().submit(counted)


# Per test thread: the counter the next handler call's executor reports to.
_STATE = threading.local()


def _run(coro):
    loop = asyncio.new_event_loop()
    counter = getattr(_STATE, "counter", None)
    if counter is not None:
        loop.set_default_executor(_CountingExecutor(counter))
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.run_until_complete(loop.shutdown_default_executor())
        loop.close()


@contextmanager
def _count_queries():
    """Count SQL issued by handler calls inside the block, on any thread."""
    counter = _Counter()
    stack, contexts = _capture()
    _STATE.counter = counter
    try:
        with stack:
            yield counter
    finally:
        _STATE.counter = None
        counter.add(contexts)


def _handler(user, topic=None, clock=None):
    from mojo.apps.realtime.access import TopicAccess
    from mojo.apps.realtime.handler import WebSocketHandler

    handler = object.__new__(WebSocketHandler)
    handler.user = user
    handler.authenticated = True
    handler.subscribed_topics = {topic} if topic else set()
    handler.topic_access = TopicAccess(clock=clock)
    handler.sent = []
    handler.removed = []
    handler.errors = []

    async def send(message):
        handler.sent.append(message)

    async def subscribe(value):
        handler.subscribed_topics.add(value)

    async def unsubscribe(value):
        handler.removed.append(value)
        handler.subscribed_topics.discard(value)

    async def incident(*args, **kwargs):
        return None

    handler.send_message = send
    handler.subscribe_to_topic = subscribe
    handler.unsubscribe_from_topic = unsubscribe
    handler.report_incident = incident
    handler._log_exception = handler.errors.append
    return handler


def _delivered(handler):
    return [m for m in handler.sent if m.get("type") == "message"]


def _deliver(handler, topic, payload=None, kind="topic_message"):
    _run(handler.process_redis_message({
        "type": kind, "topic": topic, "timestamp": 123,
        "data": payload or {"type": "chat_message", "body": "Private message"},
    }))


def _subscribe(handler, topic):
    _run(handler.handle_subscribe({"topic": topic}))


@contextmanager
def _room(label, group=False):
    """A user and a room, optionally group-linked. Clears its own leftovers."""
    from mojo.apps.account.models import Group, User
    from mojo.apps.chat.models import ChatRoom

    name = f"test-realtime-chat-{label}"
    ChatRoom.objects.filter(name=name).delete()
    Group.objects.filter(name=name).delete()
    User.objects.filter(username=name).delete()
    user = User.objects.create(username=name)
    grp = Group.objects.create(name=name) if group else None
    room = ChatRoom.objects.create(name=name, kind="group", group=grp, user=None)
    try:
        yield SimpleNamespace(user=user, room=room, group=grp, topic=f"chat:{room.pk}")
    finally:
        ChatRoom.objects.filter(name=name).delete()
        Group.objects.filter(name=name).delete()
        User.objects.filter(username=name).delete()


def _member(fx, status="active"):
    from mojo.apps.chat.models import ChatMembership
    return ChatMembership.objects.create(
        room=fx.room, user=fx.user, role="member", status=status)


def _socket_user(fx):
    """The User a socket holds: loaded at connect, never refreshed."""
    from mojo.apps.account.models import User
    return User.objects.get(pk=fx.user.pk)


@th.tier("framework")
@th.django_unit_test()
@th.requires_app("mojo.apps.chat")
def test_subscribed_socket_delivers_chat_without_sql(opts):
    with _room("no-sql") as fx:
        _member(fx)
        handler = _handler(_socket_user(fx))
        with _count_queries() as subscribe_queries:
            _subscribe(handler, fx.topic)
        assert fx.topic in handler.subscribed_topics, \
            f"an active member must be able to subscribe, sent: {handler.sent}"
        assert subscribe_queries.count > 0, \
            "control: the subscribe-time access check must be visible to the query counter"

        with _count_queries() as frame_queries:
            for index in range(100):
                _deliver(handler, fx.topic, {"type": "chat_message", "body": f"frame {index}"})
        assert len(_delivered(handler)) == 100, \
            f"all 100 frames must reach the member, got {len(_delivered(handler))}"
        assert frame_queries.count == 0, \
            f"delivering 100 frames after subscribe must issue no SQL, issued {frame_queries.count}"

        with _count_queries() as other_queries:
            _deliver(handler, fx.topic, {
                "type": "chat_member_left", "room_id": fx.room.pk, "user_id": fx.user.pk + 1000003})
        assert len(_delivered(handler)) == 101, "another member leaving must still be delivered"
        assert other_queries.count == 0, \
            f"another member's access change must not re-check this socket, issued {other_queries.count}"


@th.django_unit_test()
@th.requires_app("mojo.apps.chat")
def test_access_change_frame_stops_delivery_within_one_frame(opts):
    # Removed by an admin: the membership row is gone.
    with _room("removed") as fx:
        member = _member(fx)
        handler = _handler(_socket_user(fx))
        _subscribe(handler, fx.topic)
        _deliver(handler, fx.topic)
        assert len(_delivered(handler)) == 1, "an active member must receive chat"
        member.delete()
        _deliver(handler, fx.topic, {
            "type": "chat_member_removed", "room_id": fx.room.pk, "user_id": fx.user.pk})
        assert len(_delivered(handler)) == 1, \
            "the removal frame must be re-checked and withheld from the removed member"
        assert handler.removed == [fx.topic], "removal must unsubscribe the room"
        _deliver(handler, fx.topic)
        assert len(_delivered(handler)) == 1, "a frame queued after the unsubscribe must stay suppressed"

    # Banned by a moderator: the row stays, with status banned.
    with _room("banned") as fx:
        member = _member(fx)
        handler = _handler(_socket_user(fx))
        _subscribe(handler, fx.topic)
        member.status = "banned"
        member.save(update_fields=["status"])
        _deliver(handler, fx.topic, {
            "type": "chat_member_banned", "room_id": fx.room.pk, "user_id": str(fx.user.pk)})
        assert not _delivered(handler), "a banned member must not receive the ban frame or later chat"
        assert handler.removed == [fx.topic], "a ban must unsubscribe the room"

    # A muted member is re-checked by the same kind of frame and keeps reading.
    with _room("muted") as fx:
        member = _member(fx)
        handler = _handler(_socket_user(fx))
        _subscribe(handler, fx.topic)
        member.status = "muted"
        member.save(update_fields=["status"])
        _deliver(handler, fx.topic, {
            "type": "chat_member_left", "room_id": fx.room.pk, "user_id": fx.user.pk})
        assert len(_delivered(handler)) == 1, "a re-check that still allows must deliver the frame"
        assert not handler.removed, "a muted member retains read access"

    # The room itself deleted.
    with _room("deleted") as fx:
        _member(fx)
        handler = _handler(_socket_user(fx))
        _subscribe(handler, fx.topic)
        room_id = fx.room.pk
        fx.room.delete()
        _deliver(handler, fx.topic, {"type": "chat_room_deleted", "room_id": room_id})
        assert not _delivered(handler), "no frame may be delivered for a deleted room"
        assert handler.removed == [fx.topic], "room deletion must unsubscribe the room"


@th.django_unit_test()
@th.requires_app("mojo.apps.chat")
def test_remembered_access_expires_and_rechecks(opts):
    from mojo.apps.realtime.access import recheck_seconds

    ttl = recheck_seconds()
    assert ttl > 0, f"this test needs the remembered-access window enabled, got {ttl}"
    now = [1000.0]
    with _room("expiry") as fx:
        member = _member(fx)
        handler = _handler(_socket_user(fx), clock=lambda: now[0])
        _subscribe(handler, fx.topic)
        _deliver(handler, fx.topic)
        assert len(_delivered(handler)) == 1, "an active member must receive chat"

        now[0] += ttl + 1
        with _count_queries() as recheck_queries:
            _deliver(handler, fx.topic)
        assert len(_delivered(handler)) == 2, "a member still allowed at the re-check keeps receiving"
        assert recheck_queries.count > 0, "an expired decision must be re-checked against the database"
        with _count_queries() as cached_queries:
            _deliver(handler, fx.topic)
        assert len(_delivered(handler)) == 3, "delivery continues after a successful re-check"
        assert cached_queries.count == 0, \
            f"a successful re-check must be remembered again, issued {cached_queries.count}"

        # A change that publishes no frame is bounded by the next re-check.
        member.delete()
        now[0] += ttl + 1
        _deliver(handler, fx.topic)
        assert len(_delivered(handler)) == 3, "removal without a frame must stop delivery at the re-check"
        assert handler.removed == [fx.topic], "the failed re-check must unsubscribe the room"


@th.django_unit_test()
@th.requires_app("mojo.apps.chat")
def test_open_socket_recheck_reads_current_account_state(opts):
    from mojo.apps.account.models import User
    from mojo.apps.realtime.access import recheck_seconds

    ttl = recheck_seconds()
    now = [1000.0]
    with _room("stale-user", group=True) as fx:
        _member(fx)
        fx.user.add_permission("manage_chat")
        # The socket keeps the User loaded at connect; later changes land on
        # other copies of the row, exactly as an admin edit would.
        handler = _handler(_socket_user(fx), clock=lambda: now[0])
        _subscribe(handler, fx.topic)
        _deliver(handler, fx.topic)
        assert len(_delivered(handler)) == 1, "a permitted staff user must receive group-room chat"

        User.objects.get(pk=fx.user.pk).remove_permission("manage_chat")
        now[0] += ttl + 1
        _deliver(handler, fx.topic)
        assert len(_delivered(handler)) == 1, "removing the staff permission must stop delivery at the re-check"
        assert handler.removed == [fx.topic], "permission removal must unsubscribe the room"

        User.objects.get(pk=fx.user.pk).add_permission("manage_chat")
        connected_before = _socket_user(fx)
        handler = _handler(_socket_user(fx), clock=lambda: now[0])
        _subscribe(handler, fx.topic)
        _deliver(handler, fx.topic)
        assert len(_delivered(handler)) == 1, "a re-granted staff user must receive chat"
        User.objects.filter(pk=fx.user.pk).update(is_active=False)
        now[0] += ttl + 1
        _deliver(handler, fx.topic)
        assert len(_delivered(handler)) == 1, "a disabled account must stop receiving chat at the re-check"
        assert handler.removed == [fx.topic], "account deactivation must unsubscribe the room"

        # The decision a subscribe remembers must come from the current row:
        # this socket's User was loaded while the account was still active.
        stale = _handler(connected_before)
        _subscribe(stale, fx.topic)
        assert fx.topic not in stale.subscribed_topics, \
            "subscribe must check the current account row, not the connect-time User"
        assert not stale.topic_access.allows(fx.topic), "a refused subscribe must remember nothing"


@th.django_unit_test()
@th.requires_app("mojo.apps.chat")
def test_group_permission_access_is_unchanged(opts):
    with _room("group-perm", group=True) as fx:
        # No ChatMembership row: access comes from the group's chat permission.
        fx.group.add_member(fx.user).add_permission("chat")
        handler = _handler(_socket_user(fx))
        _subscribe(handler, fx.topic)
        assert fx.topic in handler.subscribed_topics, \
            f"a group member with the chat permission must subscribe, sent: {handler.sent}"
        with _count_queries() as frame_queries:
            for _ in range(5):
                _deliver(handler, fx.topic)
        assert len(_delivered(handler)) == 5, "group-permission access must receive chat"
        assert frame_queries.count == 0, \
            f"group-permission delivery must also be served without SQL, issued {frame_queries.count}"

    with _room("group-stranger", group=True) as fx:
        handler = _handler(_socket_user(fx))
        _subscribe(handler, fx.topic)
        assert fx.topic not in handler.subscribed_topics, \
            "a user without the group's chat permission must be refused at subscribe"
        assert any(m.get("type") == "error" for m in handler.sent), "the refusal must be reported to the client"


@th.django_unit_test()
def test_chat_authorization_error_fails_closed(opts):
    class FailingUser:
        def on_realtime_can_subscribe(self, topic):
            raise RuntimeError("Authorization storage unavailable")

    topic = "chat:123"
    handler = _handler(FailingUser(), topic)
    _deliver(handler, topic)
    assert not _delivered(handler), "Authorization exceptions must never expose chat payloads"
    assert handler.removed == [topic], "Authorization exceptions must unsubscribe chat"
    assert handler.errors, "Authorization failure must be logged"

    handler = _handler(FailingUser())
    _subscribe(handler, topic)
    assert topic not in handler.subscribed_topics, "a failing subscribe check must not subscribe"
    assert not handler.topic_access.allows(topic), "a failing subscribe check must remember nothing"


@th.django_unit_test()
def test_chat_requires_authenticated_identity_with_authorization_hook(opts):
    topic = "chat:123"
    handler = _handler(object(), topic)
    _deliver(handler, topic)
    assert not _delivered(handler), "Missing authorization hook must fail closed for chat"
    assert handler.removed == [topic], "Missing authorization hook must unsubscribe chat"

    class PermittedUser:
        def on_realtime_can_subscribe(self, value):
            return True

    handler = _handler(PermittedUser(), topic)
    handler.topic_access.allow(topic)
    handler.authenticated = False
    _deliver(handler, topic)
    assert not _delivered(handler), "Unauthenticated sockets must never receive chat, remembered or not"
    assert handler.removed == [topic], "Unauthenticated chat subscriptions must be removed"


@th.django_unit_test()
def test_non_chat_delivery_keeps_existing_behavior(opts):
    class UnexpectedAuthorization:
        def on_realtime_can_subscribe(self, topic):
            raise AssertionError("Non-chat delivery must not call the chat gate")

    handler = _handler(UnexpectedAuthorization(), "user:123")
    for kind in ("topic_message", "direct_message", "broadcast"):
        _deliver(handler, "user:123", kind=kind)
    delivered = _delivered(handler)
    assert len(delivered) == 3, "Existing non-chat delivery must remain unchanged"
    assert delivered[0]["topic"] == "user:123", "Topic envelopes must be preserved"
    assert not handler.removed and not handler.errors, "Non-chat delivery must not invoke chat authorization"
