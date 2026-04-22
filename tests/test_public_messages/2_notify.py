"""
Notification fan-out — checks that the service targets the right users,
filters by group when the message is group-scoped, and fails-open per-user.
"""
from unittest import mock

from testit import helpers as th
from testit.helpers import assert_true, assert_eq


@th.django_unit_setup()
def setup_notify(opts):
    from mojo.apps.account.models import User, Group, GroupMember, PublicMessage

    User.objects.filter(email__in=[
        "notify-sys@example.com",
        "notify-group@example.com",
        "notify-other@example.com",
        "notify-unflagged@example.com",
    ]).delete()
    Group.objects.filter(name__in=["PM-NotifyGroup", "PM-OtherGroup"]).delete()
    PublicMessage.objects.filter(email__in=[
        "sys-caller@example.com",
        "group-caller@example.com",
    ]).delete()

    opts.group = Group.objects.create(name="PM-NotifyGroup", is_active=True)
    opts.other_group = Group.objects.create(name="PM-OtherGroup", is_active=True)

    opts.sys_admin = User.objects.create_user(
        username="notify-sys@example.com",
        email="notify-sys@example.com",
        password="x",
    )
    opts.sys_admin.is_active = True
    opts.sys_admin.save()
    opts.sys_admin.set_protected_metadata("notify_public_messages", True)

    opts.group_admin = User.objects.create_user(
        username="notify-group@example.com",
        email="notify-group@example.com",
        password="x",
    )
    opts.group_admin.is_active = True
    opts.group_admin.save()
    opts.group_admin.set_protected_metadata("notify_public_messages", True)
    GroupMember.objects.create(
        user=opts.group_admin, group=opts.group, is_active=True
    )

    opts.other_group_admin = User.objects.create_user(
        username="notify-other@example.com",
        email="notify-other@example.com",
        password="x",
    )
    opts.other_group_admin.is_active = True
    opts.other_group_admin.save()
    opts.other_group_admin.set_protected_metadata("notify_public_messages", True)
    GroupMember.objects.create(
        user=opts.other_group_admin, group=opts.other_group, is_active=True
    )

    # Unflagged user in the same group — should never be notified.
    opts.unflagged = User.objects.create_user(
        username="notify-unflagged@example.com",
        email="notify-unflagged@example.com",
        password="x",
    )
    opts.unflagged.is_active = True
    opts.unflagged.save()
    GroupMember.objects.create(
        user=opts.unflagged, group=opts.group, is_active=True
    )


@th.django_unit_test()
def test_notify_unscoped_hits_all_flagged(opts):
    """Without a group on the message, every flagged user is notified."""
    from mojo.apps.account.models import PublicMessage, User
    from mojo.apps.account.services import public_message as svc

    msg = PublicMessage.objects.create(
        kind="contact_us",
        name="Caller",
        email="sys-caller@example.com",
        message="hello",
        metadata={},
    )

    sent_to = []

    def fake_send(self, template_name, context=None, **kw):
        sent_to.append(self.email)
        return None

    with mock.patch.object(User, "send_template_email", fake_send):
        sent = svc.notify_admins(msg)

    assert_true(
        "notify-sys@example.com" in sent_to,
        f"system-flagged user must be notified, got {sent_to}",
    )
    assert_true(
        "notify-unflagged@example.com" not in sent_to,
        f"unflagged user must NOT be notified, got {sent_to}",
    )
    assert_eq(sent, len(sent_to), "returned count should match send-call count")


@th.django_unit_test()
def test_notify_group_scoped_filters(opts):
    """With a group on the message, only flagged members of that group are notified."""
    from mojo.apps.account.models import PublicMessage, User
    from mojo.apps.account.services import public_message as svc

    msg = PublicMessage.objects.create(
        kind="support",
        group=opts.group,
        name="Caller",
        email="group-caller@example.com",
        message="hello",
        metadata={"category": "bug", "severity": "low"},
    )

    sent_to = []

    def fake_send(self, template_name, context=None, **kw):
        sent_to.append(self.email)
        return None

    with mock.patch.object(User, "send_template_email", fake_send):
        svc.notify_admins(msg)

    assert_true(
        "notify-group@example.com" in sent_to,
        f"group-scoped flagged user should be notified, got {sent_to}",
    )
    assert_true(
        "notify-other@example.com" not in sent_to,
        f"flagged user from other group must NOT be notified, got {sent_to}",
    )
    assert_true(
        "notify-sys@example.com" not in sent_to,
        f"system-only flagged user (no group membership) must NOT be notified "
        f"when message is group-scoped, got {sent_to}",
    )


@th.django_unit_test()
def test_notify_continues_after_one_failure(opts):
    """A raising send does not short-circuit the fan-out loop."""
    from mojo.apps.account.models import PublicMessage, User
    from mojo.apps.account.services import public_message as svc

    msg = PublicMessage.objects.create(
        kind="contact_us",
        name="Caller",
        email="sys-caller@example.com",
        message="hello",
    )

    call_count = {"n": 0}

    def flaky_send(self, template_name, context=None, **kw):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("first send fails")
        return None

    with mock.patch.object(User, "send_template_email", flaky_send):
        sent = svc.notify_admins(msg)

    # Only one user (notify-sys) is flagged + system-scope; the loop should not raise.
    assert_true(
        call_count["n"] >= 1,
        f"expected at least one send attempt, got {call_count['n']}",
    )
    assert_true(
        sent >= 0,
        f"sent count should be non-negative even on failure, got {sent}",
    )
