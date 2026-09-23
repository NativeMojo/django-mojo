"""The user graph exposes password capability without making it writable."""

from testit import helpers as th


USERNAME = "password-capability@test.com"
PASSWORD = "password-capability-123"
PASSWORDLESS_USERNAME = "passwordless-capability@test.com"


@th.django_unit_setup()
def setup_user_password_capability(opts):
    from mojo.apps.account.models import User

    User.objects.filter(
        username__in=[USERNAME, PASSWORDLESS_USERNAME]).delete()
    password_user = User.objects.create_user(
        username=USERNAME, email=USERNAME, password=PASSWORD)
    password_user.is_active = True
    password_user.save(update_fields=["is_active"])

    passwordless_user = User.objects.create_user(
        username=PASSWORDLESS_USERNAME, email=PASSWORDLESS_USERNAME)
    passwordless_user.set_unusable_password()
    passwordless_user.is_active = True
    passwordless_user.save(update_fields=["password", "is_active"])

    opts.password_user_id = password_user.pk
    opts.passwordless_user_id = passwordless_user.pk


@th.django_unit_test()
def test_default_user_graph_reports_usable_password(opts):
    from mojo.apps.account.models import User

    user = User.objects.get(pk=opts.password_user_id)
    data = user.to_dict(graph="default")
    assert data.get("has_password") is True, \
        f"password user should serialize has_password=true, got {data.get('has_password')!r}"


@th.django_unit_test()
def test_default_user_graph_reports_unusable_password(opts):
    from mojo.apps.account.models import User

    user = User.objects.get(pk=opts.passwordless_user_id)
    data = user.to_dict(graph="default")
    assert data.get("has_password") is False, \
        f"passwordless user should serialize has_password=false, got {data.get('has_password')!r}"


@th.django_unit_test()
def test_user_me_returns_password_capability(opts):
    assert opts.client.login(USERNAME, PASSWORD), \
        "password user must authenticate before reading /api/user/me"
    response = opts.client.get("/api/user/me")
    opts.client.logout()

    assert response.status_code == 200, \
        f"GET /api/user/me should succeed, got {response.status_code}: {response.text}"
    assert "has_password" in response.response.data, \
        f"GET /api/user/me must return has_password, got {response.response.data}"
    assert response.response.data.has_password is True, \
        f"password user should read has_password=true, got {response.response.data.has_password!r}"


@th.django_unit_test()
def test_user_me_reports_passwordless_capability(opts):
    from mojo.apps.account.models import User

    user = User.objects.get(pk=opts.passwordless_user_id)
    package = user.generate_jwt()
    opts.client.logout()
    opts.client.access_token = package.access_token
    opts.client.is_authenticated = True
    response = opts.client.get("/api/user/me")
    opts.client.logout()

    assert response.status_code == 200, \
        f"passwordless GET /api/user/me should succeed, got {response.status_code}: {response.text}"
    assert "has_password" in response.response.data, \
        f"passwordless GET /api/user/me must return has_password, got {response.response.data}"
    assert response.response.data.has_password is False, \
        f"passwordless user should read has_password=false, got {response.response.data.has_password!r}"


@th.django_unit_test()
def test_password_capability_is_ignored_on_user_updates(opts):
    from mojo.apps.account.models import User

    user = User.objects.get(pk=opts.password_user_id)
    user.set_password(PASSWORD)
    user.save(update_fields=["password"])

    assert opts.client.login(USERNAME, PASSWORD), \
        "password user must authenticate before exercising self updates"
    for method in (opts.client.post, opts.client.put):
        response = method("/api/user/me", {"has_password": False})
        assert response.status_code == 200, \
            f"read-only has_password should be ignored, got {response.status_code}: {response.text}"
        user.refresh_from_db()
        assert user.has_usable_password() is True, \
            f"{method.__name__.upper()} must not change usable-password state"
        assert user.check_password(PASSWORD) is True, \
            f"{method.__name__.upper()} must not replace the stored password"
    opts.client.logout()
