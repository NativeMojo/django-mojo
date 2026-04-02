from testit import helpers as th
from testit import faker

TEST_USER = "testit"

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

# @th.django_unit_test()
# def test_user_secrets_base(opts):
#     from mojo.apps.account.models import User
#     user = User.objects.filter(username=TEST_USER).last()
#     user.set_secret("test_secret", "test_value")
#     user.save()

#     user = User.objects.filter(username=TEST_USER).last()
#     assert user.get_secret("test_secret") == "test_value", "Secret value does not match"
