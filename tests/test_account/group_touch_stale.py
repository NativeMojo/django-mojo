"""Maestro item #10297 — Group.touch() must write only last_activity.

touch() used to do a full save of the group copy the request loaded. A disable,
a token-epoch bump or any metadata write committed after that copy was loaded
was then written back with the old values: the group came back on, or revoked
group tokens worked again. Each test loads a stale copy, commits newer state
beside it through the real API, calls the stale copy's touch() and re-reads
the row, which reproduces the race deterministically.

Fixtures are made by ORM with a t10297_ prefix and setup resets its own.
"""
from testit import helpers as th
from testit.helpers import assert_true, assert_eq

TESTIT_TIER = "bug"

PREFIX = "t10297_"
PASSWORD = "T10297##touch"
MEMBER_EMAIL = f"{PREFIX}member@account.test"


def _null_reporter(*args, **kwargs):
    return None


def _mk_group(name):
    from mojo.apps.account.models import Group
    group = Group.objects.create(name=f"{PREFIX}{name}", kind="generic")
    Group.objects.filter(pk=group.pk).update(last_activity=None)
    return group.pk


def _stale(pk):
    """A copy loaded now, with last_activity cleared so touch() writes."""
    from mojo.apps.account.models import Group
    stale = Group.objects.get(pk=pk)
    stale.last_activity = None
    return stale


def _login(opts):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1", key="login")
    ok = opts.client.login(MEMBER_EMAIL, PASSWORD)
    assert ok, f"login failed for {MEMBER_EMAIL}: {opts.client.last_response.body}"


@th.django_unit_setup()
def setup_group_touch_stale(opts):
    from mojo.apps.account.models import User, Group, GroupMember

    GroupMember.objects.filter(group__name__startswith=PREFIX).delete()
    Group.objects.filter(name__startswith=PREFIX).delete()
    User.objects.filter(email__startswith=PREFIX).delete()

    member = User.objects.create_user(
        username=MEMBER_EMAIL, email=MEMBER_EMAIL, password=PASSWORD)
    member.is_active = True
    member.is_email_verified = True
    member.requires_mfa = False
    member.save()
    opts.t10297_member_id = member.pk

    opts.t10297_disable = _mk_group("disable")
    opts.t10297_epoch = _mk_group("epoch")
    opts.t10297_meta = _mk_group("meta")
    opts.t10297_one = _mk_group("one")
    opts.t10297_window = _mk_group("window")
    opts.t10297_param = _mk_group("param")
    opts.t10297_member_ep = _mk_group("member_ep")
    for pk in (opts.t10297_epoch, opts.t10297_param, opts.t10297_member_ep):
        GroupMember(user=member, group=Group.objects.get(pk=pk)).save()


@th.django_unit_test("T1 a disable made after the load survives a touch")
def test_disable_survives_touch(opts):
    from mojo.apps.account.models import Group
    from mojo.apps.account.services import disable as disable_service

    stale = _stale(opts.t10297_disable)
    disable_service.disable_entity(
        Group.objects.get(pk=opts.t10297_disable), reason="admin",
        reporter=_null_reporter)

    stale.touch()

    row = Group.objects.get(pk=opts.t10297_disable)
    assert_eq(row.is_active, False,
              "touch() on a copy loaded before the disable must not switch the group back on")
    assert_true(row.last_activity is not None,
                "touch() must still record last_activity")


@th.django_unit_test("T2 a token-epoch bump made after the load survives a touch")
def test_epoch_bump_survives_touch(opts):
    from mojo.apps.account.models import Group, User
    from mojo.apps.account.services import group_token

    member = User.objects.get(pk=opts.t10297_member_id)
    token = group_token.mint(member, Group.objects.get(pk=opts.t10297_epoch))
    stale = _stale(opts.t10297_epoch)
    before = stale.get_group_token_epoch()
    bumped = Group.objects.get(pk=opts.t10297_epoch).bump_group_token_epoch()
    assert_eq(bumped, before + 1, f"the bump must move the epoch from {before}")

    stale.touch()

    row = Group.objects.get(pk=opts.t10297_epoch)
    assert_eq(row.get_group_token_epoch(), before + 1,
              "touch() on a copy loaded before the bump must not put the old epoch back")
    resp = opts.client.get("/api/user/me", headers={"Authorization": f"grouptoken {token}"})
    assert_eq(resp.status_code, 401,
              f"a token minted before the bump must stay refused after a touch, "
              f"got {resp.status_code}: {resp.response}")


@th.django_unit_test("T3 a metadata write made after the load survives a touch")
def test_metadata_write_survives_touch(opts):
    from mojo.apps.account.models import Group

    stale = _stale(opts.t10297_meta)
    fresh = Group.objects.get(pk=opts.t10297_meta)
    fresh.metadata = dict(fresh.metadata or {}, t10297_key="kept")
    fresh.save(update_fields=["metadata"])

    stale.touch()

    row = Group.objects.get(pk=opts.t10297_meta)
    assert_eq((row.metadata or {}).get("t10297_key"), "kept",
              f"touch() must not drop a metadata key written after the load: {row.metadata}")


@th.django_unit_test("T4 touch writes last_activity and no other column")
def test_touch_writes_one_column(opts):
    from mojo.apps.account.models import Group

    before = Group.objects.get(pk=opts.t10297_one)
    stale = _stale(opts.t10297_one)
    stale.name = f"{PREFIX}renamed_in_memory"
    stale.metadata = {"t10297_in_memory": True}

    stale.touch()

    row = Group.objects.get(pk=opts.t10297_one)
    assert_true(row.last_activity is not None, "touch() must set last_activity")
    assert_eq(stale.last_activity, row.last_activity,
              "touch() must keep the instance's last_activity in step with the row")
    assert_eq(row.modified, before.modified, "touch() must not move modified")
    assert_eq(row.name, before.name, "touch() must not write the in-memory name")
    assert_eq(row.metadata, before.metadata, "touch() must not write in-memory metadata")


@th.django_unit_test("T5 both request paths stamp last_activity without moving modified")
def test_request_paths_touch_one_column(opts):
    from mojo.apps.account.models import Group

    _login(opts)
    for pk, path in (
            (opts.t10297_param, f"/api/user/me?group={opts.t10297_param}"),
            (opts.t10297_member_ep, f"/api/group/{opts.t10297_member_ep}/member")):
        Group.objects.filter(pk=pk).update(last_activity=None)
        before = Group.objects.get(pk=pk)
        resp = opts.client.get(path)
        assert_eq(resp.status_code, 200, f"GET {path} failed: {resp.status_code} {resp.response}")
        row = Group.objects.get(pk=pk)
        assert_true(row.last_activity is not None, f"GET {path} must stamp last_activity")
        assert_eq(row.modified, before.modified, f"GET {path} must not move modified")


@th.django_unit_test("T6 a second touch inside the window writes nothing")
def test_second_touch_inside_window(opts):
    from mojo.apps.account.models import Group

    stale = _stale(opts.t10297_window)
    stale.touch()
    first = Group.objects.get(pk=opts.t10297_window).last_activity
    assert_true(first is not None, "the first touch must set last_activity")

    stale.touch()

    row = Group.objects.get(pk=opts.t10297_window)
    assert_eq(row.last_activity, first,
              "a touch inside GROUP_LAST_ACTIVITY_FREQ must not write again")
