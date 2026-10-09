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
    _count_queries, _deliver, _delivered, _handler, _member, _room, _run,
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


def _grant(fx, kind):
    """Give the fixture user chat through the account row or a group member
    row. Returns the model and the key of the row that holds the permission."""
    from mojo.apps.account.models import GroupMember, User

    if kind == "user":
        fx.user.add_permission("chat")
        return User, fx.user.pk
    member = fx.group.add_member(fx.user)
    member.add_permission("chat")
    return GroupMember, member.pk


@th.django_unit_test()
@th.requires_app("mojo.apps.chat")
def test_a_check_overtaken_by_an_announcement_is_not_remembered(opts):
    # Review 86781, finding 1. A subscribe whose database check has answered
    # yes is held; the permission is removed and the announcement processed;
    # the subscribe then finishes. Its older yes must not be remembered.
    import threading

    from mojo.apps.account.models import User

    with _room("ann-overtaken", group=True) as fx:
        fx.user.add_permission("chat")
        handler = _open_socket(fx)
        checked, release = threading.Event(), threading.Event()
        check = handler._can_receive_chat

        def held(topic):
            allowed = check(topic)
            checked.set()
            release.wait(5)
            return allowed

        def revoke():
            User.objects.get(pk=fx.user.pk).remove_permission("chat")

        with _announcements(fx.user.pk) as pump:
            async def overlap():
                handler._can_receive_chat = held
                subscribing = asyncio.create_task(handler.handle_subscribe({"topic": fx.topic}))
                assert await asyncio.to_thread(checked.wait, 5), \
                    "control: the subscribe check must reach the hold"
                await asyncio.to_thread(revoke)
                count = await asyncio.to_thread(pump, None)
                await handler.process_redis_message({"type": "access_changed"})
                release.set()
                await subscribing
                handler._can_receive_chat = check
                return count

            try:
                count = _run(overlap())
            finally:
                release.set()
                handler._can_receive_chat = check
        assert count == 1, f"the removal must publish one announcement, got {count}"
        assert not handler.topic_access.allows(fx.topic), \
            "a check that began before the announcement must not be remembered after it"
        _assert_stopped(handler, fx, "subscribe overlapping the announcement")


@th.django_unit_test()
def test_topic_access_epoch_guards_allow(opts):
    from mojo.apps.realtime.access import TopicAccess

    access = TopicAccess(recheck=lambda: 300)
    epoch = access.epoch
    access.allow("chat:1", epoch=epoch)
    assert access.allows("chat:1"), "control: an answer nothing overtook is remembered"
    access.forget_all()
    access.allow("chat:1", epoch=epoch)
    assert not access.allows("chat:1"), "forget_all must void a check that began before it"
    epoch = access.epoch
    access.forget("chat:2")
    access.allow("chat:1", epoch=epoch)
    assert not access.allows("chat:1"), "forget must void a check that began before it"
    access.allow("chat:1", epoch=access.epoch)
    assert access.allows("chat:1"), "a check that began after the change is remembered"


@th.django_unit_test()
@th.requires_app("mojo.apps.chat")
def test_removal_retried_after_a_rollback_is_announced(opts):
    # Review 86781, finding 2. Django does not undo Python state on rollback,
    # so what the instance noted inside the rolled-back block is not trusted.
    from django.db import transaction

    for kind in ("user", "member"):
        with _room(f"ann-rollback-{kind}", group=True) as fx:
            model, pk = _grant(fx, kind)
            handler = _open_socket(fx)
            with _announcements(fx.user.pk) as pump:
                row = model.objects.get(pk=pk)
                row.permissions = {}
                try:
                    with transaction.atomic():
                        row.save(update_fields=["permissions"])
                        raise RuntimeError("rolled back on purpose")
                except RuntimeError:
                    pass
                assert model.objects.get(pk=pk).permissions.get("chat"), \
                    f"control ({kind}): the rollback must keep the stored permission"
                assert pump(seconds=0.4) == 0, \
                    f"{kind}: a rolled-back removal must publish nothing"
                row.save(update_fields=["permissions"])
                assert pump(handler) == 1, \
                    f"{kind}: the retry that commits the removal must publish one announcement"
            _assert_stopped(handler, fx, f"{kind} removal retried after a rollback")


@th.django_unit_test()
@th.requires_app("mojo.apps.chat")
def test_removal_after_a_savepoint_rollback_or_a_rolled_back_read_is_announced(opts):
    from django.db import transaction

    for kind in ("user", "member"):
        # The removal is rolled back to a savepoint, then saved again in the
        # same outer transaction, which commits.
        with _room(f"ann-savepoint-{kind}", group=True) as fx:
            model, pk = _grant(fx, kind)
            handler = _open_socket(fx)
            with _announcements(fx.user.pk) as pump:
                row = model.objects.get(pk=pk)
                row.permissions = {}
                with transaction.atomic():
                    try:
                        with transaction.atomic():
                            row.save(update_fields=["permissions"])
                            raise RuntimeError("rolled back on purpose")
                    except RuntimeError:
                        pass
                    row.save(update_fields=["permissions"])
                    assert pump(seconds=0.4) == 0, \
                        f"{kind}: nothing may be published before the commit"
                assert pump(handler) >= 1, \
                    f"{kind}: the removal saved after a savepoint rollback must be announced"
            _assert_stopped(handler, fx, f"{kind} removal after a savepoint rollback")

        # The instance is read inside a transaction that sees an uncommitted
        # removal and is then rolled back. Saving it afterwards is a removal.
        with _room(f"ann-rolled-read-{kind}", group=True) as fx:
            model, pk = _grant(fx, kind)
            handler = _open_socket(fx)
            with _announcements(fx.user.pk) as pump:
                try:
                    with transaction.atomic():
                        model.objects.filter(pk=pk).update(permissions={})
                        row = model.objects.get(pk=pk)
                        raise RuntimeError("rolled back on purpose")
                except RuntimeError:
                    pass
                assert row.permissions == {}, "control: the instance carries the rolled-back value"
                row.save(update_fields=["permissions"])
                assert pump(handler) == 1, \
                    f"{kind}: saving a value read in a rolled-back transaction must be announced"
            _assert_stopped(handler, fx, f"{kind} removal through a rolled-back read")


@th.django_unit_test()
@th.requires_app("mojo.apps.chat")
def test_instance_built_with_an_existing_key_is_announced(opts):
    # Review 86781, finding 3. Such an instance updates the row without ever
    # having read it: when unsure, the sockets re-check.
    for kind in ("user", "member"):
        for partial in (True, False):
            label = f"{kind}, {'partial' if partial else 'full'} save"
            with _room(f"ann-built-{kind}-{int(partial)}", group=True) as fx:
                model, pk = _grant(fx, kind)
                handler = _open_socket(fx)
                row = model.objects.get(pk=pk)
                built = model(**{
                    f.attname: getattr(row, f.attname) for f in model._meta.concrete_fields})
                built.permissions = {}
                with _announcements(fx.user.pk) as pump:
                    if partial:
                        built.save(update_fields=["permissions"])
                    else:
                        built.save()
                    assert pump(handler) == 1, \
                        f"{label}: an instance built with an existing key must publish one announcement"
                assert not model.objects.get(pk=pk).permissions.get("chat"), \
                    f"control ({label}): the save must have updated the existing row"
                _assert_stopped(handler, fx, label)


@th.django_unit_test()
@th.requires_app("mojo.apps.chat")
def test_committed_transaction_keeps_what_the_instance_saw(opts):
    # The other side of the rollback rule: after a commit the instance's
    # note is trusted again, so grants and plain saves stay silent.
    from django.db import transaction
    from mojo.apps.account.models import User

    with _room("ann-committed") as fx:
        with _announcements(fx.user.pk) as pump:
            with transaction.atomic():
                user = User.objects.get(pk=fx.user.pk)
                user.add_permission("chat")
            user.display_name = "After Commit"
            user.save()
            assert pump(seconds=0.6) == 0, \
                "a grant in a committed transaction and a plain save after it must publish nothing"
            user.remove_permission("chat")
            assert pump() == 1, "the removal after the commit must publish once"


@th.django_unit_test()
@th.requires_app("mojo.apps.chat")
def test_outer_commit_does_not_vouch_for_a_rolled_back_inner_save(opts):
    # Review 86820. The instance is read in the outer transaction, the removal
    # is saved and rolled back in an inner block (an atomic block, or a
    # savepoint made by hand), and the outer transaction commits. The retry
    # after that commit is the removal, and must be announced.
    from django.db import transaction

    def atomic_block(row):
        try:
            with transaction.atomic():
                row.save(update_fields=["permissions"])
                raise RuntimeError("rolled back on purpose")
        except RuntimeError:
            pass

    def manual_savepoint(row):
        sid = transaction.savepoint()
        row.save(update_fields=["permissions"])
        transaction.savepoint_rollback(sid)

    for kind in ("user", "member"):
        for label, inner in (("atomic", atomic_block), ("manual", manual_savepoint)):
            what = f"{kind}, {label} savepoint"
            with _room(f"ann-outer-{kind}-{label}", group=True) as fx:
                model, pk = _grant(fx, kind)
                handler = _open_socket(fx)
                with _announcements(fx.user.pk) as pump:
                    with transaction.atomic():
                        row = model.objects.get(pk=pk)
                        row.permissions = {}
                        inner(row)
                    assert model.objects.get(pk=pk).permissions.get("chat"), \
                        f"control ({what}): the inner rollback must keep the stored permission"
                    early = pump(seconds=0.4)
                    # Django keeps a callback registered after a savepoint
                    # made by hand, so that rollback may still announce. An
                    # extra announcement only costs a check.
                    assert early == 0 or label == "manual", \
                        f"{what}: a rolled-back removal must publish nothing, got {early}"
                    row.save(update_fields=["permissions"])
                    assert pump(handler) == 1, \
                        f"{what}: the retry after the outer commit must publish one announcement"
                _assert_stopped(handler, fx, f"{what}, retried after the outer commit")


@th.django_unit_test()
@th.requires_app("mojo.apps.chat")
def test_clean_nested_commit_keeps_what_the_instance_saw(opts):
    # The other side: nested blocks that all commit leave the instance's note
    # trusted, so a grant made in an inner block and a plain save stay silent.
    from django.db import transaction
    from mojo.apps.account.models import User

    with _room("ann-nested-clean") as fx:
        with _announcements(fx.user.pk) as pump:
            with transaction.atomic():
                user = User.objects.get(pk=fx.user.pk)
                with transaction.atomic():
                    user.add_permission("chat")
            user.display_name = "After Nested Commit"
            user.save()
            assert pump(seconds=0.6) == 0, \
                "a grant in a committed inner block and a plain save after it must publish nothing"
            user.remove_permission("chat")
            assert pump() == 1, "the removal after the commit must publish once"


@contextmanager
def _autocommit_off():
    """Manual transaction management, as transaction.set_autocommit(False)
    gives it. Unless the caller switched autocommit back on, the open
    transaction is rolled back on the way out and autocommit restored."""
    from django.db import transaction

    transaction.set_autocommit(False)
    try:
        yield
    finally:
        if not transaction.get_autocommit():
            transaction.rollback()
            transaction.set_autocommit(True)


@th.django_unit_test()
@th.requires_app("mojo.apps.chat")
def test_read_with_autocommit_off_is_not_trusted_after_a_rollback(opts):
    # Review 86881. With autocommit switched off, a row read outside an
    # atomic block sits in a transaction the caller ends by hand. After a
    # rollback the instance still carries the rolled-back value, so saving
    # it is a removal and must be announced.
    from django.db import transaction

    for kind in ("user", "member"):
        with _room(f"ann-manual-read-{kind}", group=True) as fx:
            model, pk = _grant(fx, kind)
            handler = _open_socket(fx)
            with _announcements(fx.user.pk) as pump:
                with _autocommit_off():
                    with transaction.atomic():
                        row = model.objects.get(pk=pk)
                        row.permissions = {}
                        row.save(update_fields=["permissions"])
                    row = model.objects.get(pk=pk)
                    assert row.permissions == {}, \
                        "control: the instance is read with the uncommitted removal"
                    transaction.rollback()
                assert model.objects.get(pk=pk).permissions.get("chat"), \
                    f"control ({kind}): the rollback must keep the stored permission"
                assert pump(seconds=0.4) == 0, \
                    f"{kind}: a rolled-back removal must publish nothing"
                row.save(update_fields=["permissions"])
                assert pump(handler) == 1, \
                    f"{kind}: saving a value read in a rolled-back manual transaction must be announced"
            _assert_stopped(handler, fx, f"{kind} removal through a read with autocommit off")


@th.django_unit_test()
@th.requires_app("mojo.apps.chat")
def test_save_with_autocommit_off_announces_on_commit_and_not_on_rollback(opts):
    # Review 86881, supporting evidence: User.save outside an atomic block
    # with autocommit switched off raised after it had saved, because Django
    # refuses on_commit there. The save must not raise; the announcement
    # follows the caller's commit and a rollback drops it.
    # Review 86920: the announcement must go out at the commit itself, not
    # when autocommit is switched back on, and a later transaction that is
    # rolled back before that must not take it away.
    from django.db import transaction

    for kind in ("user", "member"):
        # Rolled back, then retried in autocommit mode.
        with _room(f"ann-manual-rollback-{kind}", group=True) as fx:
            model, pk = _grant(fx, kind)
            handler = _open_socket(fx)
            with _announcements(fx.user.pk) as pump:
                row = model.objects.get(pk=pk)
                row.permissions = {}
                with _autocommit_off():
                    row.save(update_fields=["permissions"])
                    transaction.rollback()
                assert model.objects.get(pk=pk).permissions.get("chat"), \
                    f"control ({kind}): the rollback must keep the stored permission"
                assert pump(seconds=0.4) == 0, \
                    f"{kind}: a rolled-back removal must publish nothing"
                row.save(update_fields=["permissions"])
                assert pump(handler) == 1, \
                    f"{kind}: the retry that commits the removal must publish one announcement"
            _assert_stopped(handler, fx, f"{kind} removal retried after a manual rollback")

        # Committed by hand.
        with _room(f"ann-manual-commit-{kind}", group=True) as fx:
            model, pk = _grant(fx, kind)
            handler = _open_socket(fx)
            with _announcements(fx.user.pk) as pump:
                row = model.objects.get(pk=pk)
                row.permissions = {}
                with _autocommit_off():
                    row.save(update_fields=["permissions"])
                    assert pump(seconds=0.4) == 0, \
                        f"{kind}: nothing may be published before the commit"
                    transaction.commit()
                    # Autocommit is still off here.
                    assert pump(handler) == 1, \
                        f"{kind}: a removal committed by hand must be announced at the commit"
                    _assert_stopped(handler, fx, f"{kind} removal committed with autocommit off")
                assert not model.objects.get(pk=pk).permissions.get("chat"), \
                    f"control ({kind}): the commit must store the removal"
                assert pump(seconds=0.4) == 0, \
                    f"{kind}: switching autocommit back on must not announce again"

        # Committed by hand inside an atomic block, which is only a savepoint
        # in this mode; then a later transaction is rolled back before
        # autocommit is switched back on.
        with _room(f"ann-manual-atomic-{kind}", group=True) as fx:
            model, pk = _grant(fx, kind)
            handler = _open_socket(fx)
            with _announcements(fx.user.pk) as pump:
                with _autocommit_off():
                    with transaction.atomic():
                        row = model.objects.get(pk=pk)
                        row.permissions = {}
                        row.save(update_fields=["permissions"])
                    assert pump(seconds=0.4) == 0, \
                        f"{kind}: nothing may be published before the commit"
                    transaction.commit()
                    model.objects.get(pk=pk)
                    transaction.rollback()
                assert not model.objects.get(pk=pk).permissions.get("chat"), \
                    f"control ({kind}): the commit must store the removal"
                assert pump(handler) == 1, \
                    f"{kind}: a later rollback must not take a committed removal's announcement away"
            _assert_stopped(handler, fx, f"{kind} removal committed by hand, then a later rollback")

