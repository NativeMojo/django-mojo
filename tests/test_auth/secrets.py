from testit import helpers as th
from testit import faker

# Own, uniquely-named fixture user so this module stands alone (#2792):
# it no longer borrows accounts.py's "auth_user", whose setup deletes and
# recreates that username and could race a concurrent read here.
TEST_USER = "auth_secrets_user"
TEST_PWORD = "secrets##mojo99"


@th.django_unit_setup()
def setup_secrets(opts):
    from mojo.apps.account.models import User
    # Clean up before creating — the DB is long-lived (see testing.md).
    User.objects.filter(username=TEST_USER).delete()
    user = User(username=TEST_USER, display_name=TEST_USER,
                email=f"{TEST_USER}@example.com")
    user.save()
    user.is_email_verified = True
    user.save_password(TEST_PWORD)


@th.django_unit_test()
def test_secrets_basic(opts):
    from mojo.apps.account.models import User
    user = User.objects.filter(username=TEST_USER).last()
    user.clear_secrets()
    pword = user._get_secrets_password()
    user.set_secret("test_secret", "test_value")
    user.save()

    user = User.objects.filter(username=TEST_USER).last()
    assert pword == user._get_secrets_password(), "Password does not match"
    assert user.get_secret("test_secret") == "test_value", "Secret value does not match"


@th.django_unit_test()
def test_secrets_complex(opts):
    from mojo.apps.account.models import User
    user = User.objects.filter(username=TEST_USER).last()
    user.clear_secrets()
    pword = user._get_secrets_password()
    user.set_secret("test_secret", "test_value")
    user.set_secrets({"test_secret2": "test_value2"})
    user.save()

    user = User.objects.filter(username=TEST_USER).last()
    assert pword == user._get_secrets_password(), "Password does not match"
    assert user.get_secret("test_secret") == "test_value", "Secret value does not match"
    assert user.get_secret("test_secret2") == "test_value2", "Secret value2 does not match"
    user.set_secrets({"test_secret": "test_value3"})
    user.save()

    user = User.objects.filter(username=TEST_USER).last()
    assert pword == user._get_secrets_password(), "Password does not match"
    assert user.get_secret("test_secret") == "test_value3", "Secret value does not match"
    assert user.get_secret("test_secret2") == "test_value2", "Secret value2 does not match"

