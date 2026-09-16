"""#4618: opt-in group topic authorization, using real ORM and handler paths.

This file belongs in the existing extended, serial realtime package because
its restoring Django setting override is process-wide.
"""
import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from testit import helpers as th

PERMISSIONS = ["admin_compliance", "admin_verify", "view_verify", "manage_verification"]
ABSENT = object()
PREFIX = "testit_rt4618_"


@contextmanager
def _policy(value):
    import django.conf
    name = "REALTIME_GROUP_TOPIC_PERMISSIONS"
    original = getattr(django.conf.settings, name, ABSENT)
    if value is ABSENT:
        if original is not ABSENT:
            delattr(django.conf.settings, name)
    else:
        setattr(django.conf.settings, name, value)
    try:
        yield
    finally:
        if original is ABSENT:
            if hasattr(django.conf.settings, name):
                delattr(django.conf.settings, name)
        else:
            setattr(django.conf.settings, name, original)


@contextmanager
def _fixture(label):
    from mojo.apps.account.models import Group, User
    name = PREFIX + label
    # Long-lived test databases: clear exactly this test's old fixtures first.
    User.objects.filter(username=name).delete()
    Group.objects.filter(name__in=[name, name + "_parent"]).delete()
    parent = Group.objects.create(name=name + "_parent", is_active=True)
    group = Group.objects.create(name=name, parent=parent, is_active=True)
    user = User.objects.create(username=name, email=name + "@example.com",
                               is_active=True, is_superuser=False, permissions={})
    try:
        yield SimpleNamespace(user=user, group=group, parent=parent,
                              topic=f"group:{group.pk}")
    finally:
        User.objects.filter(username=name).delete()
        Group.objects.filter(name__in=[name, name + "_parent"]).delete()


def _member(fixture, permissions=None, inherited=False, active=True):
    from mojo.apps.account.models import GroupMember
    return GroupMember.objects.create(user=fixture.user,
                                      group=fixture.parent if inherited else fixture.group,
                                      permissions=permissions or {}, is_active=active)


class _RecordingSocket:
    scope = {"headers": [], "client": ("127.0.0.1", 12345)}

    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(payload)


class _RecordingRedis:
    def __init__(self):
        self.calls = []

    def sadd(self, *args):
        self.calls.append(("sadd", args))

    def expire(self, *args):
        self.calls.append(("expire", args))

    def subscribe(self, *args):
        self.calls.append(("subscribe", args))


def _handler(user, topic=None):
    from mojo.apps.realtime.handler import WebSocketHandler
    handler = WebSocketHandler(_RecordingSocket(), "/ws/realtime/")
    handler.user = user
    handler.user_type = "user"
    handler.authenticated = True
    handler.sent = []
    handler.unsubscribed = []
    recorder = _RecordingRedis()
    handler.redis_client = recorder
    handler.pubsub = recorder
    if topic:
        handler.subscribed_topics.add(topic)

    async def send(message):
        handler.sent.append(message)

    async def unsubscribe(topic):
        handler.unsubscribed.append(topic)
        handler.subscribed_topics.discard(topic)

    handler.send_message = send
    handler.unsubscribe_from_topic = unsubscribe
    return handler


def _deliver(handler, topic):
    asyncio.run(handler.process_redis_message({
        "type": "topic_message", "topic": topic,
        "data": {"secret": "test-owned payload"}, "timestamp": 123,
    }))


@th.django_unit_test()
def test_disabled_policy_preserves_subscription_permissions(opts):
    from mojo.apps.account.models import User
    for setting in (ABSENT, None):
        with _policy(setting), _fixture("legacy") as f:
            member = _member(f)
            assert f.user.on_realtime_can_subscribe(f.topic), "Default must retain bare membership access"
            member.delete()
            for permission in ("view_groups", "manage_groups"):
                User.objects.filter(pk=f.user.pk).update(permissions={permission: True})
                f.user.refresh_from_db()
                assert f.user.on_realtime_can_subscribe(f.topic), f"Default must retain global {permission} access"


@th.django_unit_test()
def test_configured_policy_denies_unrelated_permissions_and_membership(opts):
    from mojo.apps.account.models import User
    with _policy(PERMISSIONS), _fixture("unrelated") as f:
        _member(f)
        for permissions in ({}, {"view_groups": True}, {"manage_groups": True}):
            User.objects.filter(pk=f.user.pk).update(permissions=permissions)
            f.user.refresh_from_db()
            assert not f.user.on_realtime_can_subscribe(f.topic), f"Unrelated permissions must not grant access: {permissions}"


@th.django_unit_test()
def test_each_configured_permission_grants_global_direct_and_inherited_access(opts):
    from mojo.apps.account.models import User
    with _policy(PERMISSIONS):
        for index, permission in enumerate(PERMISSIONS):
            for source in ("global", "direct", "inherited"):
                with _fixture(f"grant_{index}_{source}") as f:
                    if source == "global":
                        User.objects.filter(pk=f.user.pk).update(permissions={permission: True})
                        f.user.refresh_from_db()
                    else:
                        _member(f, {permission: True}, inherited=source == "inherited")
                    assert f.user.on_realtime_can_subscribe(f.topic), f"{source} {permission} must authorize"
                    handler = _handler(f.user, f.topic)
                    _deliver(handler, f.topic)
                    assert len(handler.sent) == 1, f"Authorized {source} {permission} must receive the message"
                    assert handler.sent[0]["topic"] == f.topic, "Delivery must retain its group topic"
                    assert not handler.unsubscribed, "Authorized subscription must remain active"


@th.django_unit_test()
def test_first_active_membership_shadows_inherited_permissions(opts):
    with _policy(PERMISSIONS), _fixture("shadow") as f:
        _member(f, {PERMISSIONS[0]: True}, inherited=True)
        direct = _member(f)
        assert not f.user.on_realtime_can_subscribe(f.topic), "Active direct membership without a grant must shadow parent grant"
        direct.is_active = False
        direct.save(update_fields=["is_active"])
        assert f.user.on_realtime_can_subscribe(f.topic), "Inactive direct membership must allow the active parent membership to resolve"


@th.django_unit_test()
def test_configured_policy_rejects_invalid_topics_and_inactive_groups(opts):
    from mojo.apps.account.models import Group, User
    with _policy(PERMISSIONS), _fixture("topics") as f:
        User.objects.filter(pk=f.user.pk).update(permissions={PERMISSIONS[0]: True})
        f.user.refresh_from_db()
        missing = Group.objects.create(name=PREFIX + "missing")
        missing_pk = missing.pk
        missing.delete()
        invalid = ["group:", "group:abc", "group:0", "group:-1", "group:+1",
                   "group: 1", "group:01", f"group:{f.group.pk}:extra",
                   f"group:{missing_pk}"]
        for topic in invalid:
            assert not f.user.on_realtime_can_subscribe(topic), f"Invalid or missing group topic must deny without raising: {topic!r}"
        Group.objects.filter(pk=f.group.pk).update(is_active=False)
        assert not f.user.on_realtime_can_subscribe(f.topic), "Global grant must not bypass inactive group"
        Group.objects.filter(pk=f.group.pk).update(is_active=True)
        Group.objects.filter(pk=f.parent.pk).update(is_active=False)
        assert not f.user.on_realtime_can_subscribe(f.topic), "Inactive parent makes the target group effectively inactive"


@th.django_unit_test()
def test_empty_and_malformed_policy_fail_closed(opts):
    from mojo.apps.account.models import User
    with _fixture("bad_config") as f:
        User.objects.filter(pk=f.user.pk).update(permissions={key: True for key in PERMISSIONS})
        f.user.refresh_from_db()
        _member(f, {key: True for key in PERMISSIONS})
        for value in ([], "view_verify", {}, False, 1, [""], [None], [1], ["view_verify", None]):
            with _policy(value):
                assert not f.user.on_realtime_can_subscribe(f.topic), f"Invalid policy must deny even a granted user: {value!r}"
                handler = _handler(f.user, f.topic)
                _deliver(handler, f.topic)
                assert not handler.sent, f"Invalid policy must suppress subscribed group data: {value!r}"


@th.django_unit_test()
def test_delivery_rechecks_revocation_and_user_group_lifecycle(opts):
    from mojo.apps.account.models import Group, GroupMember, User
    cases = ("global_revoke", "member_revoke", "member_inactive", "member_delete",
             "user_inactive", "user_delete", "group_inactive", "group_delete", "parent_inactive")
    with _policy(PERMISSIONS):
        for case in cases:
            with _fixture(case) as f:
                if case == "global_revoke":
                    User.objects.filter(pk=f.user.pk).update(permissions={PERMISSIONS[0]: True})
                    f.user.refresh_from_db()
                else:
                    member = _member(f, {PERMISSIONS[0]: True})
                handler = _handler(f.user, f.topic)
                _deliver(handler, f.topic)
                assert len(handler.sent) == 1, f"{case}: initial authorization must deliver"
                handler.sent.clear()
                if case == "global_revoke":
                    User.objects.filter(pk=f.user.pk).update(permissions={})
                elif case == "member_revoke":
                    GroupMember.objects.filter(pk=member.pk).update(permissions={})
                elif case == "member_inactive":
                    GroupMember.objects.filter(pk=member.pk).update(is_active=False)
                elif case == "member_delete":
                    GroupMember.objects.filter(pk=member.pk).delete()
                elif case == "user_inactive":
                    User.objects.filter(pk=f.user.pk).update(is_active=False)
                elif case == "user_delete":
                    User.objects.filter(pk=f.user.pk).delete()
                elif case == "group_inactive":
                    Group.objects.filter(pk=f.group.pk).update(is_active=False)
                elif case == "group_delete":
                    Group.objects.filter(pk=f.group.pk).delete()
                else:
                    Group.objects.filter(pk=f.parent.pk).update(is_active=False)
                # Deliberately never refresh handler.user: it is the stale identity
                # retained by a socket authenticated before the database change.
                _deliver(handler, f.topic)
                assert not handler.sent, f"{case}: no payload may pass after revocation"
                assert f.topic in handler.unsubscribed, f"{case}: revoked topic must be unsubscribed"
                assert f.topic not in handler.subscribed_topics, f"{case}: stale topic must leave local subscription state"


@th.django_unit_test()
def test_subscribe_to_topic_enforces_policy_for_hook_returned_topics(opts):
    with _policy(PERMISSIONS), _fixture("low_level") as f:
        member = _member(f)
        handler = _handler(f.user)
        asyncio.run(handler.subscribe_to_topic(f.topic))
        assert f.topic not in handler.subscribed_topics, "Low-level subscribe must deny bare membership even when a hook requested it"
        assert not handler.redis_client.calls, "Denied subscription must not register Redis membership or channels"
        member.permissions = {PERMISSIONS[0]: True}
        member.save(update_fields=["permissions"])
        asyncio.run(handler.subscribe_to_topic(f.topic))
        assert f.topic in handler.subscribed_topics, "Low-level subscribe must accept a newly granted member"
        assert any(call[0] == "subscribe" for call in handler.redis_client.calls), "Authorized low-level subscribe must register the Redis channel"
        member.permissions = {}
        member.save(update_fields=["permissions"])
        asyncio.run(handler.subscribe_to_topic(f.topic))
        assert f.topic not in handler.subscribed_topics, "An already-subscribed topic must still be reauthorized"


@th.django_unit_test()
def test_hookless_identity_cannot_borrow_user_with_same_pk(opts):
    from mojo.apps.account.models import User
    with _policy(PERMISSIONS), _fixture("identity") as f:
        User.objects.filter(pk=f.user.pk).update(permissions={PERMISSIONS[0]: True})
        principal = SimpleNamespace(pk=f.user.pk, id=f.user.pk, is_active=True)
        handler = _handler(principal, f.topic)
        _deliver(handler, f.topic)
        assert not handler.sent, "A hookless principal must not inherit a database User's permissions through a coincident PK"


@th.django_unit_test()
def test_own_user_and_non_group_delivery_keep_existing_behavior(opts):
    with _policy([]), _fixture("nongroup") as f:
        assert f.user.on_realtime_can_subscribe(f"user:{f.user.pk}"), "Own-user subscription must remain allowed"
        assert f.user.on_realtime_can_subscribe("general_announcements"), "General announcements must remain allowed"
        assert not f.user.on_realtime_can_subscribe(f"user:{f.user.pk + 1}"), "Other-user subscription must remain denied"
        handler = _handler(f.user)
        for topic in (f"user:{f.user.pk}", "general_announcements"):
            _deliver(handler, topic)
        for message_type in ("broadcast", "direct_message", "direct_event"):
            asyncio.run(handler.process_redis_message({"type": message_type, "data": {"type": "custom", "ok": True}}))
        assert len(handler.sent) == 5, "A closed group policy must not suppress non-group delivery"
        assert handler.sent[-1] == {"type": "custom", "ok": True}, "direct_event must retain its unwrapped payload"
        assert not handler.unsubscribed, "Non-group traffic must not trigger policy unsubscribe"


@th.django_unit_test()
def test_disabled_delivery_has_no_database_or_authorization_recheck(opts):
    from django.db.backends.utils import CursorWrapper
    with _fixture("no_recheck") as f:
        handler = _handler(f.user, f.topic)
        calls = []
        queries = []
        original_execute = CursorWrapper.execute

        def record_query(cursor, sql, params=None):
            queries.append(sql)
            return original_execute(cursor, sql, params)

        def record_hook(topic):
            calls.append(topic)
            return False

        handler.user.on_realtime_can_subscribe = record_hook
        # Instrument cursor execution across async executor threads as well as
        # this thread. This is safe only in this package's serial opt-in tier.
        for value in (ABSENT, None):
            handler.sent.clear()
            with _policy(value), patch.object(CursorWrapper, "execute", record_query):
                _deliver(handler, f.topic)
            assert len(handler.sent) == 1, "Disabled policy must retain legacy group delivery"
        assert not calls, "Disabled group delivery must not invoke permission hooks"
        assert not queries, "Disabled group delivery must not perform database queries"
        assert not handler.unsubscribed, "Disabled group delivery must not unsubscribe existing topics"


@th.django_unit_test()
def test_custom_hooks_cannot_bypass_policy_or_receive_false_success(opts):
    with _policy(PERMISSIONS), _fixture("custom_hook") as f:
        handler = _handler(f.user)
        handler.user.on_realtime_can_subscribe = lambda topic: True
        asyncio.run(handler.handle_subscribe({"topic": f.topic}))
        assert not any(message.get("type") == "subscribed" for message in handler.sent), "A permissive hook must not produce subscribed after policy denial"
        assert f.topic not in handler.subscribed_topics, "Custom hook cannot bypass the protected registration boundary"
        handler.sent.clear()
        asyncio.run(handler._process_hook_response({"subscriptions": [f.topic]}))
        assert f.topic not in handler.subscribed_topics, "Hook-returned subscriptions must enforce the same policy"

        _member(f, {PERMISSIONS[0]: True})
        handler.user.on_realtime_can_subscribe = lambda topic: False

        async def report(*args, **kwargs):
            return None

        handler.report_incident = report
        handler.sent.clear()
        asyncio.run(handler.handle_subscribe({"topic": f.topic}))
        assert f.topic not in handler.subscribed_topics, "Configured permission must not override a custom hook veto"
        assert not any(message.get("type") == "subscribed" for message in handler.sent), "A veto must never acknowledge successful subscription"


@th.django_unit_test()
def test_authorization_errors_drop_delivery(opts):
    with _policy(PERMISSIONS), _fixture("check_error") as f:
        _member(f, {PERMISSIONS[0]: True})
        handler = _handler(f.user, f.topic)
        with patch("mojo.apps.realtime.handler.can_access_group_topic",
                   side_effect=RuntimeError("test-owned authorization failure")):
            _deliver(handler, f.topic)
        assert not handler.sent, "Authorization failure must not expose the queued payload"
        assert f.topic in handler.unsubscribed, "Authorization failure must remove the protected subscription"


@th.django_unit_test()
def test_permission_check_query_cost_is_bounded(opts):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext
    with _policy(tuple(PERMISSIONS)), _fixture("query_cost") as f:
        _member(f, {PERMISSIONS[0]: True})
        with CaptureQueriesContext(connection) as queries:
            allowed = f.user.on_realtime_can_subscribe(f.topic)
        assert allowed, "Tuple configuration must authorize the active member"
        assert 0 < len(queries) <= 10, f"One parent and direct membership must take at most 10 queries, got {len(queries)}"
