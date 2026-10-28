"""Stores derived from the system default manager (#7586, #7990).

These two tests read or replace the first system default (user=None,
group=None, is_default=True), which get_for_user/get_for_group derive from.
That row is shared by every test that provisions a store, in any package,
so they run in this serial package, after and apart from all the parallel
ones, and not in the parallel test_fileman_role / test_fileman_scope packages
they came from (maestro #7538, Brenda 155355; test_filevault, Owen 155465).
"""
from testit import helpers as th
from testit.helpers import assert_eq, assert_true


MEMBER = "fm_sysdef_member"
PASSWORD = "sysdef-pass-99!"
GROUP = "fm_sysdef_group"
FRESH_GROUP = "fm_sysdef_fresh"
FIXTURE_ARN = "arn:aws:iam::210987654321:role/tenant-fileman"
PLATFORM_KEY = "AKIASYSDEFPLATFORM"
PLATFORM_SECRET = "sysdef-platform-secret"
# get_for_group() takes the first system default; a negative id keeps the
# fixture first without touching another test's row (same device as
# 12_test_public_access_reconciliation.py).
SYSTEM_DEFAULT_PK = -7586


@th.django_unit_setup()
def setup_system_default_derivation(opts):
    from mojo.apps.account.models import Group, GroupMember, User
    from mojo.apps.fileman.models import FileManager

    FileManager.objects.filter(pk=SYSTEM_DEFAULT_PK).delete()
    FileManager.objects.filter(name__startswith="fm_sysdef_").delete()
    FileManager.objects.filter(group__name__in=[GROUP, FRESH_GROUP]).delete()
    GroupMember.objects.filter(group__name__in=[GROUP, FRESH_GROUP]).delete()
    Group.objects.filter(name__in=[GROUP, FRESH_GROUP]).delete()
    User.objects.filter(username=MEMBER).delete()

    member_user = User(username=MEMBER, email=f"{MEMBER}@example.com")
    member_user.save()
    member_user.is_email_verified = True
    member_user.save_password(PASSWORD)
    member_user.save()

    group = Group(name=GROUP)
    group.save()
    member = group.add_member(member_user)
    member.add_permission("files")
    member.save()

    # The group's storage folders (#7990), shaped as in
    # test_fileman_role/21_test_manager_role_generic_keys.py. Not defaults.
    system = FileManager(name="fm_sysdef_system", backend_type="file",
                         backend_url="file://", is_active=True, is_default=False)
    system.set_secrets({"aws_key": PLATFORM_KEY, "aws_secret": PLATFORM_SECRET})
    system.save()
    system_s3 = FileManager(name="fm_sysdef_system_s3", backend_type="s3",
                            backend_url="s3://sysdef-bucket", is_active=True,
                            is_default=False)
    system_s3.set_secrets({"aws_key": PLATFORM_KEY, "aws_secret": PLATFORM_SECRET})
    system_s3.save()
    for parent, url in ((system, f"file://{group.get_uuid()}"),
                        (system_s3, "s3://sysdef-bucket/fileman")):
        anchor = FileManager(name=f"fm_sysdef_anchor_{parent.backend_type}", group=group,
                             parent=parent, backend_type=parent.backend_type,
                             backend_url=url, is_active=True, is_public=False)
        anchor.set_secrets(dict(parent.secrets))
        anchor.save()

    opts.group_id = group.id
    opts.member_user_id = member_user.id


@th.django_unit_test("file admin: a group's automatic store is still derived, role and key included")
def test_automatic_group_store_still_derived(opts):
    from mojo.apps.account.models import Group, User
    from mojo.apps.fileman.models import FileManager
    from mojo.models.rest import ACTIVE_REQUEST

    FileManager.objects.filter(pk=SYSTEM_DEFAULT_PK).delete()
    system = FileManager(
        pk=SYSTEM_DEFAULT_PK,
        name="fm_sysdef_system_default",
        backend_type="file",
        backend_url="file://",
        is_active=True,
        is_default=True,
    )
    system.save()
    system.set_secrets({"aws_key": PLATFORM_KEY, "aws_secret": PLATFORM_SECRET,
                        "assume_role_arn": FIXTURE_ARN})
    system.save()

    class _Request:
        user = User.objects.get(pk=opts.member_user_id)

    token = ACTIVE_REQUEST.set(_Request())
    try:
        derived = FileManager.get_for_group(group=Group.objects.get(pk=opts.group_id), use="sysdef")
    finally:
        ACTIVE_REQUEST.reset(token)
        FileManager.objects.filter(parent_id=SYSTEM_DEFAULT_PK).update(parent=None)
        system.delete()

    assert_true(derived is not None, "a group's automatic store must still be created for a non-superuser")
    stored = dict(FileManager.objects.get(pk=derived.pk).secrets)
    assert_eq(stored.get("assume_role_arn"), FIXTURE_ARN, "the derived store must copy the system store's role")
    assert_eq(stored.get("aws_key"), PLATFORM_KEY, "the derived store must copy the system store's key")


# Core, as in 25_test_manager_storage_scope (file-level TESTIT_TIER = "core");
# the derived-store test above stays framework, as in 21.
@th.tier("core")
@th.django_unit_test("control: get_for_group still provisions for a fresh group")
def test_get_for_group_provisions(opts):
    from mojo.apps.account.models import Group
    from mojo.apps.fileman.models import FileManager

    fresh = Group(name=FRESH_GROUP)
    fresh.save()
    default = FileManager.objects.filter(user=None, group=None, is_default=True,
                                         is_active=True).first()
    got = FileManager.get_for_group(group=fresh)
    if default is None:
        assert_true(got is None, "with no system default nothing is provisioned, as before")
    else:
        assert_true(got is not None and got.parent_id == default.id,
                    f"a fresh group gets a manager under the system default: {got}")
