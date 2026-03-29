from testit import helpers as th
from testit.helpers import assert_true, assert_eq

TEST_USER = "pii_test_user"
TEST_PWORD = "pii##mojo99"


@th.django_unit_setup()
def setup_pii(opts):
    from mojo.apps.account.models import User, Group
    from mojo.apps.account.models.notification import Notification

    user = User.objects.filter(username=TEST_USER).last()
    if user is None:
        user = User(username=TEST_USER, email=f"{TEST_USER}@example.com")
        user.save()
    # clear phone from any user that has it (unique constraint)
    User.objects.exclude(pk=user.pk).filter(phone_number="+15550001234").update(phone_number=None)
    User.objects.filter(pk=user.pk).update(phone_number=None)
    user.refresh_from_db()
    user.display_name = "Real Name"
    user.first_name = "Real"
    user.last_name = "Name"
    user.phone_number = "+15550001234"
    user.metadata = {"ip": "1.2.3.4", "dob": "1990-01-01"}
    user.permissions = {"view_data": True}
    user.is_active = True
    user.is_email_verified = True
    user.is_staff = False
    user.is_superuser = False
    user.save_password(TEST_PWORD)
    user.save()
    opts.user_id = user.pk

    group, _ = Group.objects.get_or_create(name="pii_test_group", defaults={"kind": "organization"})
    group.add_member(user)
    opts.group_id = group.pk

    Notification.send("PII test notif", user=user, push=False, ws=False)


@th.django_unit_test("pii_anonymize: PII fields are cleared")
def test_pii_fields_cleared(opts):
    from mojo.apps.account.models import User

    user = User.objects.get(pk=opts.user_id)
    summary = user.pii_anonymize()

    user.refresh_from_db()
    assert_true(user.username.startswith("deleted-"), f"username not anonymized: {user.username}")
    assert_true(user.email.endswith("@deleted.local"), f"email not anonymized: {user.email}")
    assert_eq(user.phone_number, None, "phone_number should be None")
    assert_eq(user.display_name, None, "display_name should be None")
    assert_eq(user.first_name, "", "first_name should be empty")
    assert_eq(user.last_name, "", "last_name should be empty")
    assert_eq(user.metadata, {}, "metadata should be wiped")
    assert_eq(user.permissions, {}, "permissions should be wiped")
    assert_true(not user.is_active, "user should be deactivated")
    assert_true(not user.is_staff, "is_staff should be False")
    assert_true(not user.is_superuser, "is_superuser should be False")
    assert_true("user_id" in summary, "summary should include user_id")


@th.django_unit_test("pii_anonymize: auth_key rotated (sessions revoked)")
def test_pii_auth_key_rotated(opts):
    from mojo.apps.account.models import User

    user = User.objects.get(pk=opts.user_id)
    old_key = user.auth_key
    user.pii_anonymize()
    user.refresh_from_db()
    assert_true(user.auth_key != old_key, "auth_key should be rotated to revoke sessions")


@th.django_unit_test("pii_anonymize: notifications deleted")
def test_pii_notifications_deleted(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.models.notification import Notification

    user = User.objects.get(pk=opts.user_id)
    user.pii_anonymize()
    count = Notification.objects.filter(user=user).count()
    assert_eq(count, 0, "all notifications should be deleted after anonymization")


@th.django_unit_test("pii_anonymize: group memberships removed")
def test_pii_memberships_removed(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.models.member import GroupMember

    user = User.objects.get(pk=opts.user_id)
    user.pii_anonymize()
    count = GroupMember.objects.filter(user=user).count()
    assert_eq(count, 0, "group memberships should be removed after anonymization")


@th.django_unit_test("pii_anonymize: cannot login after anonymization")
def test_pii_cannot_login(opts):
    opts.client.logout()
    resp = opts.client.login(TEST_USER, TEST_PWORD)
    assert_true(not opts.client.is_authenticated, "anonymized user should not be able to login")


@th.django_unit_test("pii_anonymize: row still exists (FK integrity preserved)")
def test_pii_row_preserved(opts):
    from mojo.apps.account.models import User

    assert_true(User.objects.filter(pk=opts.user_id).exists(), "user row should still exist after anonymization")


@th.django_unit_setup()
def cleanup_pii(opts):
    from mojo.apps.account.models import User, Group

    User.objects.filter(pk=opts.user_id).delete()
    Group.objects.filter(pk=opts.group_id).delete()
