"""FileManager role settings through the generic `secrets` / `settings` keys (#7586).

`set_assume_role_arn` and `set_external_id` refuse a non-superuser (see
14_test_manager_role_settings.py). The REST save dispatches `set_<key>` for
any posted key, and the model also has `set_secrets` and `set_settings`, which
merge a posted dict into the same store with no such check. These tests post
the role settings that way, over the real endpoint, and read the database.

Each test asserts the SAFE outcome, so a failure here is the fault confirmed;
the message carries the HTTP status and what was stored.
"""
from testit import helpers as th
from testit.helpers import assert_eq, assert_true


ADMIN = "fm_rolegen_admin"      # holds the global `files` permission
MEMBER = "fm_rolegen_member"    # holds `files` in one group only
SUPER = "fm_rolegen_super"
PASSWORD = "rolegen-pass-99!"

FIXTURE_ARN = "arn:aws:iam::210987654321:role/tenant-fileman"
POSTED_ARN = "arn:aws:iam::111111111111:role/x"
FIXTURE_EXTERNAL_ID = "fixture-external-id"
POSTED_EXTERNAL_ID = "posted-external-id"


@th.django_unit_setup()
@th.requires_app("mojo.apps.fileman")
def setup_role_generic_keys(opts):
    from mojo.apps.account.models import Group, GroupMember, User
    from mojo.apps.fileman.models import FileManager

    FileManager.objects.filter(name__startswith="fm_rolegen_").delete()
    GroupMember.objects.filter(group__name="fm_rolegen_group").delete()
    Group.objects.filter(name="fm_rolegen_group").delete()
    User.objects.filter(username__in=[ADMIN, MEMBER, SUPER]).delete()

    def make_user(username):
        user = User(username=username, email=f"{username}@example.com")
        user.save()
        user.is_email_verified = True
        user.save_password(PASSWORD)
        user.save()
        return user

    admin = make_user(ADMIN)
    admin.add_permission("files")
    admin.save()
    member_user = make_user(MEMBER)
    superuser = make_user(SUPER)
    superuser.is_superuser = True
    superuser.save()

    group = Group(name="fm_rolegen_group")
    group.save()
    member = group.add_member(member_user)
    member.add_permission("files")
    member.save()

    opts.group_id = group.id


def _login(opts, username):
    assert_true(opts.client.login(username, PASSWORD), f"{username} login must succeed")


def _new_manager(opts, name, group=False):
    """A fresh S3 manager with the fixture role, written by direct ORM use."""
    from mojo.apps.fileman.models import FileManager

    FileManager.objects.filter(name=f"fm_rolegen_{name}").delete()
    fm = FileManager(
        name=f"fm_rolegen_{name}",
        backend_type="s3",
        backend_url="s3://rolegen-bucket/fileman/prefix",
        is_active=True,
        group_id=opts.group_id if group else None,
    )
    fm.set_assume_role_arn(FIXTURE_ARN)
    fm.set_external_id(FIXTURE_EXTERNAL_ID)
    fm.save()
    return fm


def _stored(fm, key):
    from mojo.apps.fileman.models import FileManager

    return FileManager.objects.get(pk=fm.pk).get_secret(key)


def _post(opts, fm, payload):
    return opts.client.post(f"/api/fileman/manager/{fm.pk}", payload)


def _assert_not_written(opts, fm, payload, key, posted, fixture):
    resp = _post(opts, fm, payload)
    stored = _stored(fm, key)
    assert_true(
        stored != posted,
        f"a non-superuser changed {key} with {sorted(payload)}: "
        f"HTTP {resp.status_code}, stored {stored!r}")
    assert_eq(stored, fixture,
              f"{key} must be untouched after HTTP {resp.status_code}, got {stored!r}")


@th.django_unit_test("generic keys: files admin, secrets.assume_role_arn")
def test_files_admin_secrets_assume_role_arn(opts):
    fm = _new_manager(opts, "secrets_arn")
    _login(opts, ADMIN)
    _assert_not_written(opts, fm, {"secrets": {"assume_role_arn": POSTED_ARN}},
                        "assume_role_arn", POSTED_ARN, FIXTURE_ARN)


@th.django_unit_test("generic keys: files admin, settings.assume_role_arn")
def test_files_admin_settings_assume_role_arn(opts):
    fm = _new_manager(opts, "settings_arn")
    _login(opts, ADMIN)
    _assert_not_written(opts, fm, {"settings": {"assume_role_arn": POSTED_ARN}},
                        "assume_role_arn", POSTED_ARN, FIXTURE_ARN)


@th.django_unit_test("generic keys: files admin, secrets.external_id")
def test_files_admin_secrets_external_id(opts):
    fm = _new_manager(opts, "secrets_ext")
    _login(opts, ADMIN)
    _assert_not_written(opts, fm, {"secrets": {"external_id": POSTED_EXTERNAL_ID}},
                        "external_id", POSTED_EXTERNAL_ID, FIXTURE_EXTERNAL_ID)


@th.django_unit_test("generic keys: files admin, settings.external_id")
def test_files_admin_settings_external_id(opts):
    fm = _new_manager(opts, "settings_ext")
    _login(opts, ADMIN)
    _assert_not_written(opts, fm, {"settings": {"external_id": POSTED_EXTERNAL_ID}},
                        "external_id", POSTED_EXTERNAL_ID, FIXTURE_EXTERNAL_ID)


@th.django_unit_test("generic keys: group member with files, secrets.assume_role_arn on a group manager")
def test_group_member_secrets_assume_role_arn(opts):
    fm = _new_manager(opts, "group_secrets_arn", group=True)
    _login(opts, MEMBER)
    _assert_not_written(opts, fm, {"secrets": {"assume_role_arn": POSTED_ARN}},
                        "assume_role_arn", POSTED_ARN, FIXTURE_ARN)


@th.django_unit_test("control: files admin, direct assume_role_arn is refused")
def test_control_files_admin_direct_key_refused(opts):
    fm = _new_manager(opts, "direct_arn")
    _login(opts, ADMIN)
    resp = _post(opts, fm, {"assume_role_arn": POSTED_ARN})
    stored = _stored(fm, "assume_role_arn")
    assert_eq(resp.status_code, 403,
              f"the existing check must refuse the direct key, got HTTP {resp.status_code}")
    assert_eq(stored, FIXTURE_ARN, f"the refused write must store nothing, got {stored!r}")


@th.django_unit_test("control: superuser, secrets.assume_role_arn is stored")
def test_control_superuser_secrets_form_stored(opts):
    fm = _new_manager(opts, "super_secrets_arn")
    _login(opts, SUPER)
    resp = _post(opts, fm, {"secrets": {"assume_role_arn": POSTED_ARN}})
    stored = _stored(fm, "assume_role_arn")
    assert_eq(resp.status_code, 200,
              f"a superuser may configure the role, got HTTP {resp.status_code}: {resp.response}")
    assert_eq(stored, POSTED_ARN, f"the superuser's value must be stored, got {stored!r}")
