"""ITEM-027 regression — Group REST save must require SAVE_PERMS, not collapse
to the any-member view check.

Bug: MojoModel._evaluate_permission classified EVERY operation as a view
(VIEW_PERMS is present in every caller's permission_keys), so an instance that
defines check_view_permission had that hook decide writes too. Only Group defines
one (mojo/apps/account/models/group.py), and its fallthrough admits ANY active
member with a basic-graph downgrade — clearly a read affordance. Net effect: a
plain member with zero permissions could POST /api/group/<pk> to rename the
group, change kind/auth_domain/metadata, and reach POST_SAVE_ACTIONS.

Fix: writes (permission_keys containing CREATE/SAVE/DELETE_PERMS) skip the view
hook and use Group.check_edit_permission — global OR member-level SAVE_PERMS
grant (manage_groups/manage_group/groups); an ApiKey must be confined to its own
group AND hold the perm. GET keeps the member basic-graph downgrade.

Style mirrors tests/test_geofence/strict_posture.py and
tests/test_global_perms/apikey_groupless.py.
"""
import uuid as _uuid
from testit import helpers as th

IP = "127.0.0.1"
PASSWORD = "Grp##save99"


def _mk_user(email):
    from mojo.apps.account.models import User
    user = User.objects.create_user(username=email, email=email, password=PASSWORD)
    user.is_active = True
    user.is_email_verified = True
    user.requires_mfa = False
    user.save()
    return user


def _login(opts, email):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip=IP, key="login")
    muid = opts.client.session.cookies.get("_muid")
    if muid:
        clear_rate_limits(key="login", muid=muid)
    ok = opts.client.login(email, PASSWORD)
    assert ok, f"login failed for {email}: {opts.client.last_response.body}"


@th.django_unit_setup()
def setup_group_save_perms(opts):
    """One group with two active members: a plain member (no member perms) and a
    manager member (member-level manage_group, NO global perms). Non-empty
    metadata + auth_domain so the basic-graph downgrade is observable."""
    from mojo.apps.account.models import User, Group, GroupMember

    # delete-before-create — tests run against a long-lived DB
    User.objects.filter(email__startswith="grpsave_").delete()
    Group.objects.filter(name__startswith="grpsave_grp_").delete()

    tag = _uuid.uuid4().hex[:8]
    grp = Group.objects.create(
        name=f"grpsave_grp_{tag}",
        kind="organization",
        is_active=True,
        auth_domain=f"grpsave-{tag}.example.test",
        metadata={"motto": "original"},
    )
    opts.grp_id = grp.pk

    opts.plain_email = f"grpsave_plain_{tag}@account.test"
    grp.add_member(_mk_user(opts.plain_email))

    opts.mgr_email = f"grpsave_mgr_{tag}@account.test"
    mgr = _mk_user(opts.mgr_email)
    grp.add_member(mgr)
    mm = GroupMember.objects.get(group=grp, user=mgr)
    mm.add_permission("manage_group")   # member-level grant only — no global perm
    mm.save()


@th.django_unit_test("ITEM-027: plain member CANNOT save Group fields (403)")
def test_plain_member_cannot_save_group(opts):
    """The core regression. Pre-fix this returned 200 and renamed the group."""
    from mojo.apps.account.models import Group
    _login(opts, opts.plain_email)
    try:
        resp = opts.client.post(f"/api/group/{opts.grp_id}", {"name": "hacked-by-member"})
        assert resp.status_code in (401, 403), \
            f"plain member must NOT save Group, got {resp.status_code}: {opts.client.last_response.body}"
        grp = Group.objects.get(pk=opts.grp_id)
        assert grp.name != "hacked-by-member", \
            f"SECURITY: plain member renamed the group to {grp.name!r}"
    finally:
        opts.client.logout()


@th.django_unit_test("ITEM-027: plain member GET Group keeps the basic-graph downgrade (200)")
def test_plain_member_get_group_downgraded(opts):
    """Reads are unaffected: a plain member still GETs their group, but only the
    basic graph (no metadata / auth_domain)."""
    _login(opts, opts.plain_email)
    try:
        resp = opts.client.get(f"/api/group/{opts.grp_id}")
        assert resp.status_code == 200, \
            f"plain member must still GET their group, got {resp.status_code}: {opts.client.last_response.body}"
        data = resp.response.data
        assert data.id == opts.grp_id, f"GET returned the wrong/no group: {data}"
        # basic graph = id/uuid/name/created/modified/last_activity/is_active/kind;
        # metadata + auth_domain are default-graph-only, so their absence proves
        # the member downgrade fired.
        assert "metadata" not in data, \
            f"basic-graph downgrade leaked metadata to a plain member: {list(data.keys())}"
        assert "auth_domain" not in data, \
            f"basic-graph downgrade leaked auth_domain to a plain member: {list(data.keys())}"
    finally:
        opts.client.logout()


@th.django_unit_test("ITEM-027: member with manage_group CAN save Group fields (200)")
def test_manage_group_member_can_save_group(opts):
    """A member-level SAVE_PERMS grant authorizes the write — no global perm and
    no downgrade needed."""
    from mojo.apps.account.models import Group
    _login(opts, opts.mgr_email)
    try:
        resp = opts.client.post(f"/api/group/{opts.grp_id}", {"name": "renamed-by-admin"})
        assert resp.status_code == 200, \
            f"manage_group member must save Group, got {resp.status_code}: {opts.client.last_response.body}"
        grp = Group.objects.get(pk=opts.grp_id)
        assert grp.name == "renamed-by-admin", \
            f"manage_group save did not persist; group name is {grp.name!r}"
    finally:
        opts.client.logout()


@th.django_unit_test("ITEM-027: confined ApiKey holding manage_group CAN save its group (200)")
def test_confined_apikey_with_perm_can_save_group(opts):
    """Positive counterpart to test_global_perms/apikey_groupless.py's zero-perm
    denial: a key confined to its group AND holding the perm may write it."""
    from mojo.apps.account.models import Group, ApiKey
    grp = Group.objects.get(pk=opts.grp_id)
    _, token = ApiKey.create_for_group(
        group=grp, name=f"grpsave_key_{_uuid.uuid4().hex[:6]}",
        permissions={"manage_group": True})
    try:
        opts.client.logout()
        opts.client.bearer = "apikey"
        opts.client.access_token = token
        opts.client.is_authenticated = True
        resp = opts.client.post(f"/api/group/{opts.grp_id}", {"name": "renamed-by-key"})
        assert resp.status_code == 200, \
            f"confined key with manage_group must save its group, got {resp.status_code}: {opts.client.last_response.body}"
        grp.refresh_from_db()
        assert grp.name == "renamed-by-key", \
            f"key save did not persist; group name is {grp.name!r}"
    finally:
        opts.client.logout()
        ApiKey.objects.filter(name__startswith="grpsave_key_").delete()
