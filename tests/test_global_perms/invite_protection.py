"""The group member-invite path now enforces MEMBER_PERMS_PROTECTION.

Before the fix, POST /api/group/member/invite wrote the `permissions` payload
via on_rest_update_jsonfield directly — bypassing can_change_permission (and
thus MEMBER_PERMS_PROTECTION) entirely. After the fix it routes through
set_permissions. Combined with reading MEMBER_PERMS_PROTECTION as kind="dict",
a configured protection map is honored on the invite path.

Uses a test-only protected key so real member flows in parallel are untouched.
The protection map is written as a global Setting row (a JSON string), so the
403 here also proves member.py reads it with kind="dict" — the buggy string
read would raise a TypeError (500) at `member_perms_protection[perm]`, not 403.
"""
import uuid as _uuid
from testit import helpers as th

PROTECTED_PERM = "itest_gp_protected_perm"
# Requires the granter to hold a global perm nobody in this test holds → any
# attempt to assign PROTECTED_PERM is denied.
PROTECTION_MAP = {PROTECTED_PERM: "sys.itest_never_held_by_anyone"}


@th.django_unit_setup()
def setup_invite_protection(opts):
    from mojo.apps.account.models import User, Group, GroupMember
    from mojo.apps.account.models.setting import Setting

    # Clean slate on the long-lived DB.
    Setting.remove("MEMBER_PERMS_PROTECTION")

    suffix = _uuid.uuid4().hex[:8]
    # Group admin: holds manage_group at the MEMBER level only (enough to pass
    # the invite endpoint's own gate), NOT global manage_groups/manage_users
    # (which would bypass can_change_permission entirely).
    admin_email = f"gp_inviteadmin_{suffix}@globalperms.test"
    admin_pw = "Gp##invite99"
    admin = User.objects.create_user(username=admin_email, email=admin_email, password=admin_pw)
    admin.is_active = True
    admin.is_email_verified = True
    admin.requires_mfa = False
    admin.save()
    group = Group.objects.create(name=f"gp_invite_group_{suffix}", kind="organization")
    m, _ = GroupMember.objects.get_or_create(user=admin, group=group)
    m.permissions = {"manage_group": True}
    m.save()

    opts.admin_email = admin_email
    opts.admin_pw = admin_pw
    opts.group = group
    opts.group_id = group.pk
    opts.invitee_protected = f"gp_invitee_prot_{suffix}@globalperms.test"
    opts.invitee_plain = f"gp_invitee_plain_{suffix}@globalperms.test"


def _login(opts):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1", key="login")
    assert opts.client.login(opts.admin_email, opts.admin_pw), \
        f"admin login failed: {opts.client.last_response.body}"


@th.django_unit_test("invite: protected perm cannot be granted on the invite path")
def test_invite_enforces_member_perms_protection(opts):
    from mojo.apps.account.models import User, GroupMember
    from mojo.apps.account.models.setting import Setting

    Setting.set("MEMBER_PERMS_PROTECTION", PROTECTION_MAP)  # stored as JSON string
    _login(opts)
    try:
        # Inviting with the protected perm must be denied (group admin lacks the
        # required global perm). Pre-fix this returned 200 (bypass).
        resp = opts.client.post("/api/group/member/invite", {
            "group": opts.group_id,
            "email": opts.invitee_protected,
            "permissions": {PROTECTED_PERM: True},
        })
        assert resp.status_code == 403, \
            f"protected perm must be denied on invite, got {resp.status_code}: {opts.client.last_response.body}"

        # And the perm must NOT have landed on the (created) member.
        invitee = User.objects.filter(email=opts.invitee_protected).first()
        if invitee is not None:
            m = GroupMember.objects.filter(user=invitee, group=opts.group).first()
            if m is not None:
                assert not m.permissions.get(PROTECTED_PERM), \
                    "protected perm leaked onto the member despite the 403"

        # An UNLISTED perm is still grantable by a group admin (behavior
        # preserved for the common case).
        resp = opts.client.post("/api/group/member/invite", {
            "group": opts.group_id,
            "email": opts.invitee_plain,
            "permissions": {"some_group_perm": True},
        })
        assert resp.status_code == 200, \
            f"unlisted perm must still be grantable, got {resp.status_code}: {opts.client.last_response.body}"
        plain = User.objects.get(email=opts.invitee_plain)
        m = GroupMember.objects.get(user=plain, group=opts.group)
        assert m.permissions.get("some_group_perm") is True, \
            f"unlisted perm must land, got {m.permissions}"
    finally:
        Setting.remove("MEMBER_PERMS_PROTECTION")
        opts.client.logout()
        for email in (opts.invitee_protected, opts.invitee_plain):
            u = User.objects.filter(email=email).first()
            if u is not None:
                GroupMember.objects.filter(user=u).delete()
                u.delete()
