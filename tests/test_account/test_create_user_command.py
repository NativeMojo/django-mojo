"""Tests for the `create_user` management command."""
from io import StringIO
from testit import helpers as th

EMAIL_SUPERUSER = "cucmd_super@test.com"
PHONE_ONLY = "+15555550100"
EMAIL_STAFF = "cucmd_scoped@test.com"
EMAIL_DUP = "cucmd_dup@test.com"
EMAIL_WEAK = "cucmd_weak@test.com"


@th.django_unit_setup()
def setup_create_user_command(opts):
    from mojo.apps.account.models import User

    # Clean up any leftover test data so the suite is repeatable.
    User.objects.filter(email__in=[
        EMAIL_SUPERUSER, EMAIL_STAFF, EMAIL_DUP, EMAIL_WEAK,
    ]).delete()
    User.objects.filter(phone_number=User.normalize_phone(PHONE_ONLY)).delete()


@th.django_unit_test()
def test_create_email_superuser(opts):
    from django.core.management import call_command
    from mojo.apps.account.models import User

    out = StringIO()
    call_command('create_user', '--email', EMAIL_SUPERUSER, '--password', 'Str0ng!Passw0rd',
                  '--superuser', stdout=out)

    user = User.objects.get(email=EMAIL_SUPERUSER)
    assert user.is_staff, "superuser creation should also set is_staff"
    assert user.is_superuser, "expected --superuser to set is_superuser"
    assert user.check_password('Str0ng!Passw0rd'), "password should verify after creation"


@th.django_unit_test()
def test_create_phone_only_user(opts):
    from django.core.management import call_command
    from mojo.apps.account.models import User

    out = StringIO()
    call_command('create_user', '--phone', PHONE_ONLY, '--first-name', 'Ada', '--last-name', 'Lovelace',
                  '--password', 'Str0ng!Passw0rd', stdout=out)

    user = User.objects.get(phone_number=User.normalize_phone(PHONE_ONLY))
    assert user.email is None, f"phone-only user should have no email, got {user.email!r}"
    assert user.username, "a username should have been auto-generated"


@th.django_unit_test()
def test_create_staff_with_scoped_permission(opts):
    from django.core.management import call_command
    from mojo.apps.account.models import User

    out = StringIO()
    call_command('create_user', '--email', EMAIL_STAFF, '--password', 'Str0ng!Passw0rd',
                  '--staff', '--permission', 'manage_users', stdout=out)

    user = User.objects.get(email=EMAIL_STAFF)
    assert user.is_staff, "expected --staff to set is_staff"
    assert not user.is_superuser, "should not be superuser without --superuser"
    assert user.has_permission("manage_users"), "expected manage_users permission to be granted"
    assert not user.has_permission("view_logs"), \
        "granting one permission must not leak access to unrelated permissions"


@th.django_unit_test()
def test_create_user_duplicate_email_rejected(opts):
    from django.core.management import call_command, CommandError
    from mojo.apps.account.models import User

    User.objects.filter(email=EMAIL_DUP).delete()
    User.objects.create_user(username=EMAIL_DUP, email=EMAIL_DUP, password="Str0ng!Passw0rd")
    before_count = User.objects.filter(email=EMAIL_DUP).count()

    out = StringIO()
    raised = False
    try:
        call_command('create_user', '--email', EMAIL_DUP, '--password', 'AnotherStr0ng!Pw', stdout=out)
    except CommandError:
        raised = True
    assert raised, "creating a user with a duplicate email should raise CommandError"
    assert User.objects.filter(email=EMAIL_DUP).count() == before_count, \
        "duplicate rejection must not create an extra row"


@th.django_unit_test()
def test_create_user_weak_password_rejected(opts):
    from django.core.management import call_command, CommandError
    from mojo.apps.account.models import User

    User.objects.filter(email=EMAIL_WEAK).delete()

    out = StringIO()
    raised = False
    try:
        call_command('create_user', '--email', EMAIL_WEAK, '--password', 'weak', stdout=out)
    except CommandError:
        raised = True
    assert raised, "a weak password should raise CommandError"
    assert not User.objects.filter(email=EMAIL_WEAK).exists(), \
        "no user row should be created when password strength validation fails"
