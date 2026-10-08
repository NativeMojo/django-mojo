"""Maestro item #7149 — a Setting's group is fixed when it is created.

Bug: `Setting.RestMeta` leaves `group` writable and the generic REST save
checks an update only against the group the row is LEAVING. A member holding
`manage_settings` in one group could create a setting there and then clear its
group (a platform-wide row, which overrides the deployment's own configuration
for every tenant) or point it at a group they can only view. A member holding
only `manage_members` could grant themselves `manage_settings` first.

Fix: `Setting` refuses any change of `group` on an existing row, for every
writer, and a REST create authorized through a group can only produce a row in
that group.

Isolation-scanner rules this file follows: every create posts to the literal
path `/api/settings` with an inline dict carrying a literal `TESTIT_` key;
updates use `f"/api/settings/{pk}"`; every row is removed by its literal key in
`finally`.
"""
import uuid as _uuid
from testit import helpers as th


IP = "127.0.0.1"
PASSWORD = "Scope##set99"
ADMIN_USER = "setscope_admin"


def _mk_user(email, username=None, superuser=False):
    from mojo.apps.account.models import User
    user = User.objects.create_user(
        username=username or email, email=email, password=PASSWORD)
    user.is_active = True
    user.is_email_verified = True
    user.requires_mfa = False
    user.is_superuser = superuser
    user.save()
    return user


def _login(opts, username):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip=IP, key="login")
    muid = opts.client.session.cookies.get("_muid")
    if muid:
        clear_rate_limits(key="login", muid=muid)
    ok = opts.client.login(username, PASSWORD)
    assert ok, f"login failed for {username}: {opts.client.last_response.body}"


def _drop(rows):
    """Remove test-owned rows from Redis and the database."""
    for row in rows:
        row.remove_from_cache()
    rows.delete()


@th.django_unit_setup()
def setup_setting_scope(opts):
    """Two groups. In A: a `manage_members`-only member and a `manage_settings`
    member. The `manage_settings` member can also view B, nothing more."""
    from mojo.apps.account.models import User, Group, GroupMember, Setting

    # delete-before-create — tests run against a long-lived DB
    _drop(Setting.objects.filter(key__startswith="TESTIT_SCOPE_"))
    User.objects.filter(email__startswith="setscope_").delete()
    User.objects.filter(username=ADMIN_USER).delete()
    Group.objects.filter(name__startswith="setscope_grp_").delete()

    tag = _uuid.uuid4().hex[:8]
    grp_a = Group.objects.create(
        name=f"setscope_grp_a_{tag}", kind="organization", is_active=True)
    grp_b = Group.objects.create(
        name=f"setscope_grp_b_{tag}", kind="organization", is_active=True)
    opts.grp_a = grp_a.pk
    # Group.uuid is null until asked for; get_uuid() allocates and saves one.
    opts.grp_a_uuid = grp_a.get_uuid()
    assert opts.grp_a_uuid and opts.grp_a_uuid != "None", \
        f"group A needs a real uuid for the group_uuid tests, got {opts.grp_a_uuid!r}"
    opts.grp_b = grp_b.pk

    opts.members_email = f"setscope_members_{tag}@account.test"
    members_only = _mk_user(opts.members_email)
    grp_a.add_member(members_only)
    mm = GroupMember.objects.get(group=grp_a, user=members_only)
    mm.add_permission("manage_members")   # member-level grant only
    mm.save()

    opts.settings_email = f"setscope_settings_{tag}@account.test"
    settings_mgr = _mk_user(opts.settings_email)
    grp_a.add_member(settings_mgr)
    ms = GroupMember.objects.get(group=grp_a, user=settings_mgr)
    ms.add_permission("manage_settings")  # member-level grant only
    ms.save()
    grp_b.add_member(settings_mgr)
    viewer = GroupMember.objects.get(group=grp_b, user=settings_mgr)
    viewer.add_permission("view_groups")
    viewer.save()

    _mk_user(f"setscope_admin_{tag}@account.test", username=ADMIN_USER,
             superuser=True)


@th.django_unit_test("#7149: a manage_members-only member cannot make their group's setting platform-wide")
def test_members_only_cannot_globalize(opts):
    """The regression. Before the fix the last request returned 200 and the row
    became platform-wide."""
    from mojo.apps.account.models import Setting

    _login(opts, opts.members_email)
    try:
        granted = opts.client.post("/api/group/member/invite", {
            "group": opts.grp_a,
            "email": opts.members_email,
            "permissions": {"manage_settings": True},
        })
        assert granted.status_code == 200, (
            f"the self-grant is documented behaviour and must still work here, "
            f"got {granted.status_code}: {opts.client.last_response.body}")

        created = opts.client.post("/api/settings", {
            "group": opts.grp_a, "key": "TESTIT_SCOPE_REGRESSION", "value": "mine"})
        assert created.status_code == 200, (
            f"a group setting create must work, got {created.status_code}: "
            f"{opts.client.last_response.body}")
        row = Setting.objects.get(key="TESTIT_SCOPE_REGRESSION")
        assert row.group_id == opts.grp_a, f"created in the wrong scope: {row.group_id}"

        moved = opts.client.post(f"/api/settings/{row.pk}", {"group": None})
        row.refresh_from_db()
        assert row.group_id == opts.grp_a, (
            f"SECURITY: a group member turned their group's setting into a "
            f"platform-wide setting (group_id={row.group_id}, "
            f"status {moved.status_code})")
        assert moved.status_code == 400, (
            f"clearing a setting's group must be refused with 400, got "
            f"{moved.status_code}: {opts.client.last_response.body}")
    finally:
        opts.client.logout()
        _drop(Setting.objects.filter(key="TESTIT_SCOPE_REGRESSION"))


@th.django_unit_test("#7149: no spelling of group moves an existing setting")
def test_update_cannot_move(opts):
    from mojo.apps.account.models import Group, Setting

    row = Setting.objects.create(
        key="TESTIT_SCOPE_MOVE", group=Group.objects.get(pk=opts.grp_a), value="v")
    _login(opts, opts.settings_email)
    try:
        attempts = [
            {"group_id": None},
            {"group": None},
            {"group": ""},
            {"group": 0},
            {"group": opts.grp_b},
            {"group_id": opts.grp_b},
        ]
        for body in attempts:
            resp = opts.client.post(f"/api/settings/{row.pk}", body)
            row.refresh_from_db()
            assert row.group_id == opts.grp_a, (
                f"SECURITY: {body} moved the setting to group_id={row.group_id} "
                f"(status {resp.status_code})")
            assert resp.status_code == 400, (
                f"{body} must be refused with 400, got {resp.status_code}: "
                f"{opts.client.last_response.body}")
        assert not Setting.objects.filter(
            key="TESTIT_SCOPE_MOVE").exclude(group_id=opts.grp_a).exists(), \
            "a refused move left a row outside the member's group"
    finally:
        opts.client.logout()
        _drop(Setting.objects.filter(key="TESTIT_SCOPE_MOVE"))


@th.django_unit_test("#7149: a create authorized by group id cannot land in another group")
def test_create_by_id_cannot_land_elsewhere(opts):
    from mojo.apps.account.models import Setting

    _login(opts, opts.settings_email)
    try:
        resp = opts.client.post("/api/settings", {
            "group": opts.grp_a, "group_id": opts.grp_b,
            "key": "TESTIT_SCOPE_GRAFT_ID", "value": "v"})
        stored = list(Setting.objects.filter(
            key="TESTIT_SCOPE_GRAFT_ID").values_list("group_id", flat=True))
        assert not stored, (
            f"SECURITY: a create authorized in group A stored a row in "
            f"{stored} (status {resp.status_code})")
        assert resp.status_code == 403, (
            f"group + group_id of another group must be refused with 403, got "
            f"{resp.status_code}: {opts.client.last_response.body}")
    finally:
        opts.client.logout()
        _drop(Setting.objects.filter(key="TESTIT_SCOPE_GRAFT_ID"))


@th.django_unit_test("#7149: a create authorized by group_uuid lands in that group and nowhere else")
def test_create_by_uuid_cannot_land_elsewhere(opts):
    from mojo.apps.account.models import Setting

    _login(opts, opts.settings_email)
    try:
        # The uuid must really authorize: without this a bad uuid would be
        # refused before the scope rule and the test below would prove nothing.
        own = opts.client.post("/api/settings", {
            "group_uuid": opts.grp_a_uuid,
            "key": "TESTIT_SCOPE_UUID_OWN", "value": "v"})
        assert own.status_code == 200, (
            f"a create authorized by the member's own group_uuid must work, "
            f"got {own.status_code}: {opts.client.last_response.body}")
        landed = list(Setting.objects.filter(
            key="TESTIT_SCOPE_UUID_OWN").values_list("group_id", flat=True))
        assert landed == [opts.grp_a], \
            f"a group_uuid create must land in that group, got {landed}"

        resp = opts.client.post("/api/settings", {
            "group_uuid": opts.grp_a_uuid, "group_id": opts.grp_b,
            "key": "TESTIT_SCOPE_GRAFT_UUID", "value": "v"})
        stored = list(Setting.objects.filter(
            key="TESTIT_SCOPE_GRAFT_UUID").values_list("group_id", flat=True))
        assert not stored, (
            f"SECURITY: a create authorized by group_uuid of A stored a row in "
            f"{stored} (status {resp.status_code})")
        assert resp.status_code == 403, (
            f"group_uuid + group_id of another group must be refused with 403, "
            f"got {resp.status_code}: {opts.client.last_response.body}")
    finally:
        opts.client.logout()
        _drop(Setting.objects.filter(key="TESTIT_SCOPE_UUID_OWN"))
        _drop(Setting.objects.filter(key="TESTIT_SCOPE_GRAFT_UUID"))


@th.django_unit_test("#7149: a member still creates and edits settings in their own group")
def test_member_own_group_still_works(opts):
    from mojo.apps.account.models import Setting

    _login(opts, opts.settings_email)
    try:
        no_group = opts.client.post("/api/settings", {
            "key": "TESTIT_SCOPE_OWN", "value": "v"})
        assert no_group.status_code == 403, (
            f"a member create with no group must stay refused, got "
            f"{no_group.status_code}: {opts.client.last_response.body}")
        assert not Setting.objects.filter(key="TESTIT_SCOPE_OWN").exists(), \
            "the refused platform-wide create stored a row"

        created = opts.client.post("/api/settings", {
            "group": opts.grp_a, "key": "TESTIT_SCOPE_OWN", "value": "v"})
        assert created.status_code == 200, (
            f"a member create in their own group must work, got "
            f"{created.status_code}: {opts.client.last_response.body}")
        row = Setting.objects.get(key="TESTIT_SCOPE_OWN")
        assert row.group_id == opts.grp_a, f"created in the wrong scope: {row.group_id}"

        edited = opts.client.post(f"/api/settings/{row.pk}", {"value": "v2"})
        assert edited.status_code == 200, (
            f"a value-only edit must work, got {edited.status_code}: "
            f"{opts.client.last_response.body}")
        row.refresh_from_db()
        assert row.value == "v2" and row.group_id == opts.grp_a, \
            f"value edit went wrong: value={row.value!r} group_id={row.group_id}"

        same = opts.client.post(
            f"/api/settings/{row.pk}", {"group": opts.grp_a, "value": "v3"})
        assert same.status_code == 200, (
            f"an edit that repeats the row's own group must work, got "
            f"{same.status_code}: {opts.client.last_response.body}")

        # REST DELETE is off for Setting (no CAN_DELETE) before and after this
        # change; the in-process removal is the delete that exists.
        assert Setting.remove("TESTIT_SCOPE_OWN", group=row.group), \
            "removing the member's group row in process must still work"
        assert not Setting.objects.filter(key="TESTIT_SCOPE_OWN").exists(), \
            "the removed row is still stored"
    finally:
        opts.client.logout()
        _drop(Setting.objects.filter(key="TESTIT_SCOPE_OWN"))


@th.django_unit_test("#7149: a platform administrator keeps full use of settings, minus the move")
def test_superuser_paths(opts):
    from mojo.apps.account.models import Setting

    _login(opts, ADMIN_USER)
    try:
        created = opts.client.post("/api/settings", {
            "key": "TESTIT_SCOPE_GLOBAL", "value": "v"})
        assert created.status_code == 200, (
            f"a platform-wide create must work for an administrator, got "
            f"{created.status_code}: {opts.client.last_response.body}")
        row = Setting.objects.get(key="TESTIT_SCOPE_GLOBAL")
        assert row.group_id is None, f"expected a platform-wide row, got {row.group_id}"

        edited = opts.client.post(f"/api/settings/{row.pk}", {"value": "v2"})
        assert edited.status_code == 200, (
            f"a platform-wide value edit must work, got {edited.status_code}: "
            f"{opts.client.last_response.body}")

        moved = opts.client.post(f"/api/settings/{row.pk}", {"group": opts.grp_a})
        row.refresh_from_db()
        assert moved.status_code == 400 and row.group_id is None, (
            f"moving a platform-wide row into a group must be refused with 400, "
            f"got {moved.status_code}, group_id={row.group_id}")

        assert Setting.remove("TESTIT_SCOPE_GLOBAL"), \
            "removing a platform-wide row in process must still work"
        assert not Setting.objects.filter(key="TESTIT_SCOPE_GLOBAL").exists(), \
            "the removed platform-wide row is still stored"

        in_group = opts.client.post("/api/settings", {
            "group": opts.grp_a, "key": "TESTIT_SCOPE_ADMIN_GROUP", "value": "v"})
        assert in_group.status_code == 200, (
            f"an administrator create in a group must work, got "
            f"{in_group.status_code}: {opts.client.last_response.body}")
        group_row = Setting.objects.get(key="TESTIT_SCOPE_ADMIN_GROUP")
        assert group_row.group_id == opts.grp_a, \
            f"created in the wrong scope: {group_row.group_id}"

        globalized = opts.client.post(
            f"/api/settings/{group_row.pk}", {"group": None})
        group_row.refresh_from_db()
        assert globalized.status_code == 400 and group_row.group_id == opts.grp_a, (
            f"moving a group row to platform-wide must be refused with 400 for "
            f"everyone, got {globalized.status_code}, group_id={group_row.group_id}")

        assert Setting.remove("TESTIT_SCOPE_ADMIN_GROUP", group=group_row.group), \
            "removing a group row in process must still work"
    finally:
        opts.client.logout()
        _drop(Setting.objects.filter(key="TESTIT_SCOPE_GLOBAL"))
        _drop(Setting.objects.filter(key="TESTIT_SCOPE_ADMIN_GROUP"))


@th.django_unit_test("#7149: in-process writers keep working and cannot move a row either")
def test_in_process_writers(opts):
    from mojo import errors as merrors
    from mojo.apps.account.models import Group, Setting

    grp_a = Group.objects.get(pk=opts.grp_a)
    grp_b = Group.objects.get(pk=opts.grp_b)
    try:
        Setting.set("TESTIT_SCOPE_INPROC", "one", group=grp_a)
        row = Setting.set("TESTIT_SCOPE_INPROC", "two", group=grp_a)
        assert row.group_id == opts.grp_a and row.get_value() == "two", \
            f"Setting.set on an existing group row broke: {row.group_id} {row.get_value()!r}"

        for target in (None, grp_b):
            row = Setting.objects.get(key="TESTIT_SCOPE_INPROC")
            row.group = target
            with th.assert_raises(merrors.ValueException):
                row.save()
            assert Setting.objects.get(key="TESTIT_SCOPE_INPROC").group_id == opts.grp_a, \
                f"an in-process save moved the row to {target}"

        # No ambient request: the create rule has nothing to compare with and
        # must not crash.
        made = Setting.create_from_dict(
            {"key": "TESTIT_SCOPE_FROM_DICT", "value": "v"})
        assert made.pk and made.group_id is None, \
            f"create_from_dict with no request went wrong: {made.pk} {made.group_id}"
    finally:
        _drop(Setting.objects.filter(key="TESTIT_SCOPE_INPROC"))
        _drop(Setting.objects.filter(key="TESTIT_SCOPE_FROM_DICT"))
