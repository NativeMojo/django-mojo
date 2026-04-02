"""
Regression tests for Member.send_invite routing.

Bug: send_invite always sent the group_invite notification template, even for
users who have never logged in and need an account-setup invite with a token link.

Issue: planning/issues/resend_invite_sends_wrong_email_for_new_user.md
"""
from unittest.mock import patch, MagicMock
from testit import helpers as th
from testit.helpers import assert_true, assert_eq

TEST_GROUP = "member_invite_group"
TEST_NEW_USER = "member_invite_new_user"
TEST_EXISTING_USER = "member_invite_existing_user"


@th.django_unit_setup()
def setup_member_invite(opts):
    from mojo.apps.account.models import User, Group, GroupMember
    from mojo.helpers import dates

    group, _ = Group.objects.get_or_create(name=TEST_GROUP)
    opts.group_id = group.pk

    # New user — has never logged in
    new_user, _ = User.objects.get_or_create(
        username=TEST_NEW_USER,
        defaults={"email": f"{TEST_NEW_USER}@example.com"},
    )
    new_user.last_login = None
    new_user.is_active = True
    new_user.save(update_fields=["last_login", "is_active", "modified"])
    new_member, _ = GroupMember.objects.get_or_create(user=new_user, group=group)
    opts.new_member_id = new_member.pk

    # Existing user — has logged in before
    existing_user, _ = User.objects.get_or_create(
        username=TEST_EXISTING_USER,
        defaults={"email": f"{TEST_EXISTING_USER}@example.com"},
    )
    existing_user.last_login = dates.utcnow()
    existing_user.is_active = True
    existing_user.save(update_fields=["last_login", "is_active", "modified"])
    existing_member, _ = GroupMember.objects.get_or_create(user=existing_user, group=group)
    opts.existing_member_id = existing_member.pk


# ---------------------------------------------------------------------------
# New user (never logged in) → user.send_invite called (regression)
# ---------------------------------------------------------------------------

@th.django_unit_test("member_invite: new user gets account-setup invite, not group_invite (regression)")
def test_new_user_gets_invite_email(opts):
    from mojo.apps.account.models import GroupMember

    member = GroupMember.objects.get(pk=opts.new_member_id)
    assert_true(member.user.last_login is None, "user should have no last_login for this test")

    with patch.object(member.user.__class__, "send_invite") as mock_send_invite, \
         patch.object(member.user.__class__, "send_template_email") as mock_send_template:

        member.send_invite()

        assert_true(mock_send_invite.called,
                    "user.send_invite should be called for a user who has never logged in")
        assert_true(not mock_send_template.called,
                    "send_template_email (group_invite) should NOT be called for a new user")

        call_kwargs = mock_send_invite.call_args
        assert_true(call_kwargs is not None, "send_invite should have been called with arguments")


# ---------------------------------------------------------------------------
# Existing user (has logged in) → group_invite template sent
# ---------------------------------------------------------------------------

@th.django_unit_test("member_invite: existing user gets group_invite notification (no regression)")
def test_existing_user_gets_group_invite(opts):
    from mojo.apps.account.models import GroupMember

    member = GroupMember.objects.get(pk=opts.existing_member_id)
    assert_true(member.user.last_login is not None, "user should have a last_login for this test")

    with patch.object(member.user.__class__, "send_invite") as mock_send_invite, \
         patch.object(member.user.__class__, "send_template_email") as mock_send_template:

        member.send_invite()

        assert_true(not mock_send_invite.called,
                    "user.send_invite should NOT be called for an existing user")
        assert_true(mock_send_template.called,
                    "send_template_email should be called for an existing user")

        template_name = mock_send_template.call_args[0][0]
        assert_eq(template_name, "group_invite",
                  f"existing user should receive group_invite template, got {template_name!r}")
