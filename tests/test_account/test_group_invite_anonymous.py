"""Regression (ITEM-028): POST /api/group/member/invite must fail closed with a
clean 403, never a raw HTTP 500.

Unauthenticated: the handler called request.group.user_has_permission(
ANONYMOUS_USER, perms); ANONYMOUS_USER.has_permission was a zero-arg lambda, so
calling it with the perms argument raised TypeError -> a raw 500 leaking the
interpreter message. It must instead return a clean 403 (the @md.requires_auth()
gate) with no membership side effect.

Authenticated caller + inactive/unknown group: the dispatcher resolves such a
group id to None (Group.get_active), so request.group is None and the handler
used to raise AttributeError -> 500. It must now return a clean 403 (the None
guard), still with no side effect.
"""
from testit import helpers as th

INVITEE_EMAIL = "item028_invitee@account.test"
ADMIN_EMAIL = "item028_admin@account.test"
ADMIN_PW = "Item028##Pw99"
ACTIVE_GROUP = "item028_active_group"
INACTIVE_GROUP = "item028_inactive_group"


@th.django_unit_setup()
def setup_group_invite_anon(opts):
    from mojo.apps.account.models import User, Group, GroupMember

    # Clean slate on the long-lived DB (delete before create); GroupMember rows
    # cascade with their user/group.
    User.objects.filter(email__in=[INVITEE_EMAIL, ADMIN_EMAIL]).delete()
    Group.objects.filter(name__in=[ACTIVE_GROUP, INACTIVE_GROUP]).delete()

    active = Group.objects.create(name=ACTIVE_GROUP, is_active=True)
    opts.group_id = active.pk

    # Inactive group -> dispatcher's Group.get_active() returns None -> request.group None.
    inactive = Group.objects.create(name=INACTIVE_GROUP, is_active=False)
    opts.inactive_group_id = inactive.pk

    # Authenticated user with a member-level manage_group grant on the active group
    # (passes the endpoint's own permission gate) for the None-guard case.
    admin = User.objects.create_user(username=ADMIN_EMAIL, email=ADMIN_EMAIL, password=ADMIN_PW)
    admin.is_active = True
    admin.is_email_verified = True
    admin.requires_mfa = False
    admin.save()
    m, _ = GroupMember.objects.get_or_create(user=admin, group=active)
    m.permissions = {"manage_group": True}
    m.save()


def _cleanup_invitee():
    from mojo.apps.account.models import User, GroupMember
    u = User.objects.filter(email=INVITEE_EMAIL).first()
    if u is not None:
        GroupMember.objects.filter(user=u).delete()
        u.delete()


@th.django_unit_test("invite: anonymous POST is a clean 403, not a 500")
def test_invite_anonymous_is_403_not_500(opts):
    from mojo.apps.account.models import User, GroupMember

    before = GroupMember.objects.filter(group_id=opts.group_id).count()

    # Genuinely anonymous: logout drops the Authorization header, so the server
    # sees request.user = ANONYMOUS_USER.
    opts.client.logout()
    resp = opts.client.post("/api/group/member/invite", {
        "group": opts.group_id,
        "email": INVITEE_EMAIL,
    })
    try:
        assert resp.status_code == 403, \
            f"anonymous invite must be a clean 403, got {resp.status_code}: {opts.client.last_response.body}"
        body = resp.response
        assert body.get("error") == "Permission Denied", \
            f"expected the standard permission-denied body, got: {body!r}"
        assert body.get("code") == 403, f"expected code 403 in body, got: {body!r}"
        assert body.get("status") is False, f"expected status false in body, got: {body!r}"

        assert GroupMember.objects.filter(group_id=opts.group_id).count() == before, \
            "anonymous invite must not create a GroupMember"
        assert not User.objects.filter(email=INVITEE_EMAIL).exists(), \
            "anonymous invite must not create the invitee User"
    finally:
        _cleanup_invitee()


@th.django_unit_test("invite: authed caller + inactive/unknown group is a clean 403, not a 500")
def test_invite_authed_unknown_group_is_403_not_500(opts):
    from mojo.decorators.limits import clear_rate_limits
    from mojo.apps.account.models import User

    clear_rate_limits(ip="127.0.0.1", key="login")
    assert opts.client.login(ADMIN_EMAIL, ADMIN_PW), \
        f"admin login failed: {opts.client.last_response.body}"
    try:
        # Inactive group -> dispatcher resolves request.group to None -> the None
        # guard must produce a clean 403 (pre-fix: AttributeError -> 500).
        resp = opts.client.post("/api/group/member/invite", {
            "group": opts.inactive_group_id,
            "email": INVITEE_EMAIL,
        })
        assert resp.status_code == 403, \
            f"authed invite to an inactive group must be a clean 403, got {resp.status_code}: {opts.client.last_response.body}"
        assert not User.objects.filter(email=INVITEE_EMAIL).exists(), \
            "invite to an unresolved group must not create the invitee User"
    finally:
        opts.client.logout()
        _cleanup_invitee()
