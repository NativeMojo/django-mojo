"""A member permission request refused partway saves nothing and writes no
audit record (maestro #10293).

GroupMember.set_permissions used to save after every toggle, so a request
whose third change was refused still committed the first two. It now applies
every toggle and saves once: a refusal leaves the row exactly as it was, and
GroupMember.save() writes no "member:permissions_changed" record.

The refusal needs a protected MEMBER_PERMS_PROTECTION row, which is legal
only in this serial, opt-in package.
"""
import uuid as _uuid
from testit import helpers as th

KEY = "MEMBER_PERMS_PROTECTION"
KIND = "member:permissions_changed"
PROT = "itest_mpa_protected"
# Requires a global perm nobody in this module holds → always refused.
NEVER = "sys.itest_mpa_never_held_by_anyone"
PWORD = "Mpa##refuse99"


def _clear_row():
    from mojo.apps.account.models.setting import Setting
    for row in Setting.objects.filter(key=KEY, group=None):
        row.remove_from_cache()
    Setting.objects.filter(key=KEY, group=None).delete()


def _make_user(email):
    from mojo.apps.account.models import User
    User.objects.filter(email=email).delete()
    user = User.objects.create_user(username=email, email=email, password=PWORD)
    user.is_active = True
    user.is_email_verified = True
    user.requires_mfa = False
    user.save()
    return user


@th.django_unit_test("member audit: a request refused partway saves nothing and writes no record")
def test_refused_request_saves_nothing(opts):
    from mojo.apps.account.models import Group, GroupMember, User
    from mojo.apps.account.models.setting import Setting
    from mojo.apps.logit.models import Log
    from mojo.decorators.limits import clear_rate_limits

    tag = _uuid.uuid4().hex[:8]
    manager = _make_user(f"mpa_ref_mgr_{tag}@audit.test")
    target = _make_user(f"mpa_ref_tgt_{tag}@audit.test")
    group = Group.objects.create(name=f"mpa_ref_{tag}", kind="organization")
    # Member-level authority only: enough for unprotected perms, never PROT.
    GroupMember.objects.create(user=manager, group=group, permissions={"manage_group": True})
    member = GroupMember.objects.create(user=target, group=group, permissions={"view_logs": True})
    # Drop the fixture's own insert record; only the request is counted.
    Log.objects.filter(kind=KIND, model_id=member.pk).delete()
    _clear_row()
    try:
        Setting.set(KEY, {PROT: NEVER})
        clear_rate_limits(ip="127.0.0.1", key="login")
        assert opts.client.login(manager.email, PWORD), \
            f"manager login failed: {opts.client.last_response.body}"

        # Two allowed toggles come first; the protected grant is refused.
        resp = opts.client.post(f"/api/group/member/{member.pk}", {
            "permissions": {"manage_members": True, "view_logs": False, PROT: True}})
        assert resp.status_code == 403, \
            f"granting a protected perm must be refused, got {resp.status_code}: {opts.client.last_response.body}"

        member.refresh_from_db()
        assert member.permissions == {"view_logs": True}, \
            f"a refused request must leave the row unchanged, earlier toggles included, got {member.permissions!r}"
        recs = list(Log.objects.filter(kind=KIND, model_id=member.pk).values_list("log", flat=True))
        assert recs == [], f"a refused request must write no audit record, got {recs}"
    finally:
        opts.client.logout()
        _clear_row()
        member_ids = list(GroupMember.objects.filter(group=group).values_list("id", flat=True))
        Log.objects.filter(kind=KIND, model_id__in=member_ids).delete()
        GroupMember.objects.filter(group=group).delete()
        group.delete()
        User.objects.filter(pk__in=[manager.pk, target.pk]).delete()
