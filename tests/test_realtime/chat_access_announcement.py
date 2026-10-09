"""Chat delivery after access is removed without a chat frame (maestro #7498).

A permission removed from a user or a group member, or either of them
deactivated, publishes ``access_changed`` on the user's access channel. An
open socket then forgets its remembered chat decisions, so the next frame is
re-checked instead of waiting for the periodic re-check."""
import asyncio
import json
import time
from contextlib import contextmanager

from testit import helpers as th

from tests.test_realtime.chat_delivery import (
    _count_queries, _deliver, _delivered, _handler, _member, _room,
    _socket_user, _subscribe,
)

TESTIT_TIER = "framework"


@contextmanager
def _announcements(user_id):
    """Listen on a user's access channel the way an open socket does. The
    yielded ``pump`` waits for what was published there, hands it to the
    handler as its pub/sub loop would, and returns how many arrived."""
    from mojo.apps.realtime.channels import access_channel
    from mojo.helpers.redis import get_connection
    from tests.test_realtime.chat_delivery import _run

    pubsub = get_connection().pubsub()
    pubsub.subscribe(access_channel("user", user_id))
    end = time.time() + 2
    while time.time() < end:
        msg = pubsub.get_message(timeout=0.1)
        if msg and msg.get("type") == "subscribe":
            break

    def pump(handler=None, seconds=2.0):
        got = []
        end = time.time() + seconds
        while time.time() < end:
            msg = pubsub.get_message(timeout=0.1)
            if msg and msg.get("type") == "message":
                got.append(json.loads(msg["data"]))
                end = min(end, time.time() + 0.3)
        for data in got:
            if handler is not None:
                _run(handler.process_redis_message(data))
        return len(got)

    try:
        yield pump
    finally:
        pubsub.close()


def _open_socket(fx):
    handler = _handler(_socket_user(fx))
    _subscribe(handler, fx.topic)
    assert fx.topic in handler.subscribed_topics, \
        f"control: the user must be able to subscribe before the change, sent: {handler.sent}"
    _deliver(handler, fx.topic)
    assert len(_delivered(handler)) == 1, "control: chat must reach the user before the change"
    return handler


def _assert_stopped(handler, fx, what):
    _deliver(handler, fx.topic)
    assert len(_delivered(handler)) == 1, \
        f"{what}: no chat frame may be delivered after the change, got {len(_delivered(handler)) - 1}"
    assert handler.removed == [fx.topic], f"{what}: the room must be unsubscribed"


@th.django_unit_test()
@th.requires_app("mojo.apps.chat")
def test_user_permission_removed_stops_delivery_at_once(opts):
    from mojo.apps.account.models import User

    with _room("ann-user-perm", group=True) as fx:
        fx.user.add_permission("chat")
        handler = _open_socket(fx)
        sent_before = len(handler.sent)
        with _announcements(fx.user.pk) as pump:
            User.objects.get(pk=fx.user.pk).remove_permission("chat")
            assert pump(handler) == 1, \
                "removing a user permission must publish one announcement"
        assert len(handler.sent) == sent_before, "the announcement itself must not be sent to the client"
        _assert_stopped(handler, fx, "user permission removed")


@th.django_unit_test()
@th.requires_app("mojo.apps.chat")
def test_superuser_and_active_flags_switched_off_stop_delivery(opts):
    from mojo.apps.account.models import User

    with _room("ann-superuser", group=True) as fx:
        User.objects.filter(pk=fx.user.pk).update(is_superuser=True)
        handler = _open_socket(fx)
        with _announcements(fx.user.pk) as pump:
            user = User.objects.get(pk=fx.user.pk)
            user.is_superuser = False
            user.save()
            assert pump(handler) == 1, "switching is_superuser off must publish one announcement"
        _assert_stopped(handler, fx, "is_superuser switched off")

    with _room("ann-inactive") as fx:
        _member(fx)
        handler = _open_socket(fx)
        with _announcements(fx.user.pk) as pump:
            user = User.objects.get(pk=fx.user.pk)
            user.is_active = False
            user.save(update_fields=["is_active"])
            assert pump(handler) == 1, "deactivating the account must publish one announcement"
        _assert_stopped(handler, fx, "account deactivated")


@th.django_unit_test()
@th.requires_app("mojo.apps.chat")
def test_group_member_changes_stop_delivery_at_once(opts):
    from mojo.apps.account.models import GroupMember

    with _room("ann-member-perm", group=True) as fx:
        fx.group.add_member(fx.user).add_permission("chat")
        handler = _open_socket(fx)
        with _announcements(fx.user.pk) as pump:
            GroupMember.objects.get(group=fx.group, user=fx.user).remove_permission("chat")
            assert pump(handler) == 1, "removing a member permission must publish one announcement"
        _assert_stopped(handler, fx, "group member permission removed")

    with _room("ann-member-off", group=True) as fx:
        fx.group.add_member(fx.user).add_permission("chat")
        handler = _open_socket(fx)
        with _announcements(fx.user.pk) as pump:
            member = GroupMember.objects.get(group=fx.group, user=fx.user)
            member.is_active = False
            member.save()
            assert pump(handler) == 1, "deactivating a member must publish one announcement"
        _assert_stopped(handler, fx, "group member deactivated")

    with _room("ann-member-gone", group=True) as fx:
        fx.group.add_member(fx.user).add_permission("chat")
        handler = _open_socket(fx)
        with _announcements(fx.user.pk) as pump:
            GroupMember.objects.get(group=fx.group, user=fx.user).delete()
            assert pump(handler) == 1, "deleting a member must publish one announcement"
        _assert_stopped(handler, fx, "group member deleted")


@th.django_unit_test()
@th.requires_app("mojo.apps.chat")
def test_member_permission_removed_with_membership_deleted_in_the_same_save(opts):
    # The shape a consumer reported: the member loses a permission, and a
    # signal on that save deletes the chat membership with a queryset delete,
    # outside the chat endpoints. The room is not group-linked, so only the
    # membership row decides access.
    from django.db.models.signals import post_save
    from mojo.apps.account.models import Group, GroupMember
    from mojo.apps.chat.models import ChatMembership

    with _room("ann-combined") as fx:
        Group.objects.filter(name="test-realtime-chat-ann-combined-staff").delete()
        staff = Group.objects.create(name="test-realtime-chat-ann-combined-staff")
        try:
            staff.add_member(fx.user).add_permission("support")
            _member(fx)
            handler = _open_socket(fx)

            def drop_membership(sender, instance, **kwargs):
                if instance.user_id == fx.user.pk and not instance.has_permission("support"):
                    ChatMembership.objects.filter(room=fx.room, user_id=fx.user.pk).delete()

            post_save.connect(drop_membership, sender=GroupMember)
            try:
                with _announcements(fx.user.pk) as pump:
                    GroupMember.objects.get(group=staff, user=fx.user).remove_permission("support")
                    assert pump(handler) == 1, "the member save must publish one announcement"
            finally:
                post_save.disconnect(drop_membership, sender=GroupMember)
            assert not ChatMembership.objects.filter(room=fx.room, user_id=fx.user.pk).exists(), \
                "control: the signal must have deleted the membership row"
            _assert_stopped(handler, fx, "permission removed and membership deleted together")
        finally:
            Group.objects.filter(name="test-realtime-chat-ann-combined-staff").delete()


@th.django_unit_test()
@th.requires_app("mojo.apps.chat")
def test_saves_that_remove_nothing_announce_nothing(opts):
    from mojo.apps.account.models import GroupMember, User

    with _room("ann-quiet", group=True) as fx:
        member = fx.group.add_member(fx.user)
        member.add_permission("chat")
        handler = _open_socket(fx)
        with _announcements(fx.user.pk) as pump:
            user = User.objects.get(pk=fx.user.pk)
            user.display_name = "Quiet Save"
            user.save()
            user.add_permission("view_groups")
            user.is_active = False
            user.save(update_fields=["display_name"])
            member = GroupMember.objects.get(pk=member.pk)
            member.add_permission("manage_chat")
            member.save()
            assert pump(handler, seconds=0.6) == 0, \
                "a plain save, a grant and a field that is not written must publish nothing"
            # The same instance now writes is_active off: that is a removal.
            user.save()
            assert pump(seconds=2.0) == 1, \
                "the later save that does write is_active off must still be announced"
            User.objects.filter(pk=user.pk).update(is_active=True)
        with _count_queries() as frame_queries:
            for _ in range(5):
                _deliver(handler, fx.topic)
        assert len(_delivered(handler)) == 6, "delivery must continue after saves that remove nothing"
        assert frame_queries.count == 0, \
            f"delivery must still issue no SQL after those saves, issued {frame_queries.count}"


@th.django_unit_test()
@th.requires_app("mojo.apps.chat")
def test_a_permission_added_and_removed_on_one_instance_is_announced(opts):
    # The baseline follows each save, so a grant and a later removal through
    # the same instance is still seen as a removal. A new row announces nothing.
    from mojo.apps.account.models import User

    with _room("ann-same-instance") as fx:
        with _announcements(fx.user.pk) as pump:
            user = User.objects.get(pk=fx.user.pk)
            user.add_permission("chat")
            assert pump(seconds=0.6) == 0, "a grant must publish nothing"
            user.remove_permission("chat")
            assert pump() == 1, "the removal after the grant must publish once"

    User.objects.filter(username="test-realtime-chat-ann-created").delete()
    created = User(username="test-realtime-chat-ann-created", is_active=False)
    created.save()
    try:
        with _announcements(created.pk) as pump:
            created.display_name = "Still Inactive"
            created.save()
            assert pump(seconds=0.6) == 0, \
                "saving a row that was created inactive must publish nothing"
    finally:
        User.objects.filter(username="test-realtime-chat-ann-created").delete()


@th.django_unit_test()
@th.requires_app("mojo.apps.chat")
def test_announcement_only_forces_a_check(opts):
    with _room("ann-still-allowed") as fx:
        _member(fx)
        handler = _open_socket(fx)
        _deliver(handler, fx.topic, kind="access_changed")
        assert not handler.topic_access.allows(fx.topic), \
            "an announcement must forget the remembered decision"
        assert len(_delivered(handler)) == 1, "an announcement must never reach the client"
        with _count_queries() as recheck_queries:
            _deliver(handler, fx.topic)
        assert recheck_queries.count > 0, "the frame after an announcement must be re-checked"
        assert len(_delivered(handler)) == 2, "a user who still has access keeps receiving chat"
        assert not handler.removed, "a re-check that allows must not unsubscribe"

    with _room("ann-stranger") as fx:
        handler = _handler(_socket_user(fx), fx.topic)
        _deliver(handler, fx.topic, kind="access_changed")
        _deliver(handler, fx.topic)
        assert not _delivered(handler), "an announcement must never grant access"
        assert handler.removed == [fx.topic], "the denied re-check must unsubscribe"


@th.django_unit_test()
@th.requires_app("mojo.apps.chat")
def test_another_users_announcement_goes_to_another_channel(opts):
    from mojo.apps.account.models import User

    with _room("ann-other") as fx:
        _member(fx)
        handler = _open_socket(fx)
        User.objects.filter(username="test-realtime-chat-ann-other-2").delete()
        other = User.objects.create(username="test-realtime-chat-ann-other-2")
        try:
            other.add_permission("chat")
            with _announcements(fx.user.pk) as mine, _announcements(other.pk) as theirs:
                User.objects.get(pk=other.pk).remove_permission("chat")
                assert theirs() == 1, "control: the changed user's channel must carry the announcement"
                assert mine(handler, seconds=0.6) == 0, "nothing may arrive on another user's channel"
            assert handler.topic_access.allows(fx.topic), \
                "another user's change must leave this socket's decision remembered"
        finally:
            User.objects.filter(username="test-realtime-chat-ann-other-2").delete()


@th.django_unit_test()
def test_socket_listens_on_its_access_channel(opts):
    from types import SimpleNamespace
    from mojo.apps.realtime.channels import access_channel, broadcast_channel, messages_channel
    from mojo.apps.realtime.handler import WebSocketHandler

    class PubSub:
        def __init__(self):
            self.channels = []

        async def subscribe(self, channel):
            self.channels.append(channel)

        async def get_message(self, timeout=None):
            await asyncio.sleep(0)
            return None

        async def aclose(self):
            return None

    pubsub = PubSub()
    handler = object.__new__(WebSocketHandler)
    handler.pubsub = None
    handler.running = False
    handler.connection_id = "conn-7498"
    handler.user = SimpleNamespace(id=41)
    handler.user_type = "user"

    async def start():
        await handler.start_redis_messages(pubsub=pubsub)
        await handler._redis_task

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(start())
    finally:
        loop.close()
    assert pubsub.channels == [
        messages_channel("conn-7498"), broadcast_channel(), access_channel("user", 41)], \
        f"an authenticated socket must listen on its identity's access channel, got {pubsub.channels}"


@th.django_unit_test()
def test_publish_failure_is_never_raised(opts):
    from mojo.apps.realtime import manager
    from mojo.apps.realtime.channels import access_channel

    sent = []
    manager.publish_access_changed("user", 41, publisher=lambda channel, message: sent.append((channel, message)))
    assert sent == [(access_channel("user", 41), json.dumps({"type": "access_changed"}))], \
        f"control: the announcement must go to the identity's access channel, got {sent}"

    def broken(channel, message):
        raise RuntimeError("Redis unavailable")

    try:
        manager.publish_access_changed("user", 41, publisher=broken)
    except Exception as exc:
        raise AssertionError(f"a failed announcement must never fail the write, raised {exc!r}")


@th.django_unit_test()
@th.requires_app("mojo.apps.chat")
def test_recheck_seconds_zero_checks_every_frame(opts):
    # WS_SUBSCRIPTION_RECHECK_SECONDS <= 0: nothing is remembered, so a removal
    # that announces nothing still stops delivery on the next frame.
    from mojo.apps.chat.models import ChatMembership
    from mojo.apps.realtime.access import TopicAccess

    for seconds in (0, -5):
        with _room(f"ann-zero-{abs(seconds)}") as fx:
            _member(fx)
            handler = _handler(_socket_user(fx))
            handler.topic_access = TopicAccess(recheck=lambda: seconds)
            _subscribe(handler, fx.topic)
            assert fx.topic in handler.subscribed_topics, "control: a member must be able to subscribe"
            assert not handler.topic_access.allows(fx.topic), \
                f"with {seconds} seconds a subscribe must remember nothing"
            with _count_queries() as frame_queries:
                for _ in range(3):
                    _deliver(handler, fx.topic)
            assert len(_delivered(handler)) == 3, "a member must still receive chat"
            assert frame_queries.count > 0, \
                f"with {seconds} seconds every frame must be checked against the database"
            assert not handler.topic_access.allows(fx.topic), \
                f"with {seconds} seconds a passed check must remember nothing"
            # A bulk delete: no frame and no announcement.
            ChatMembership.objects.filter(room=fx.room, user=fx.user).delete()
            _deliver(handler, fx.topic)
            assert len(_delivered(handler)) == 3, \
                f"with {seconds} seconds the very next frame after a silent removal must be withheld"
            assert handler.removed == [fx.topic], "the denied check must unsubscribe the room"
