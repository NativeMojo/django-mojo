"""Immediate, authenticated account closure."""
import time

from testit import helpers as th
from testit.helpers import assert_eq, assert_true


PASSWORD = "close##mojo99"
PREFIX = "account_close_test_"


def _make_user(label, password=PASSWORD):
    from mojo.apps.account.models import User

    username = f"{PREFIX}{label}"
    user = User.objects.create_user(
        username=username, email=f"{username}@example.com", password=password)
    user.is_active = True
    user.is_email_verified = True
    user.requires_mfa = False
    user.save()
    user.get_auth_key()
    return user


def _token(user, auth_time):
    from mojo.apps.account.utils.jwtoken import JWToken
    return JWToken(user.get_auth_key()).create_access_token(
        uid=user.pk, auth_time=auth_time)


def _use_bearer(opts, token):
    opts.client.logout()
    opts.client.bearer = "bearer"
    opts.client.access_token = token
    opts.client.is_authenticated = True


def _error(resp):
    return resp.json.get("error") or resp.json.get("message")


def _clear_limits():
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1", key="account_close")


@th.django_unit_setup()
def setup_account_close(opts):
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits

    User.objects.filter(username__startswith=PREFIX).delete()
    clear_rate_limits(ip="127.0.0.1")


@th.django_unit_test("account close: password proof closes account and records incident")
def test_account_close_password(opts):
    from mojo.apps.account.models import User
    from mojo.apps.incident.models.event import Event

    _clear_limits()
    user = _make_user("password")
    assert_true(opts.client.login(user.username, PASSWORD), "login must succeed")
    resp = opts.client.post("/api/account/close", {"current_password": PASSWORD})
    opts.client.logout()

    assert_eq(resp.status_code, 200, f"closure should succeed, got {resp.status_code}: {resp.json}")
    assert_eq(resp.json.get("message"), "Your account has been deleted.",
              f"closure should return the frozen success sentence, got {resp.json}")
    assert_true(not User.objects.get(pk=user.pk).is_active,
                "successful immediate closure must deactivate the account")
    assert_true(Event.objects.filter(uid=user.pk, category="account:deactivated").exists(),
                "closure must record account:deactivated before anonymizing")


@th.django_unit_test("account close: fresh passwordless session closes account")
def test_account_close_passwordless_fresh(opts):
    from mojo.apps.account.models import User

    _clear_limits()
    user = _make_user("passwordless")
    user.set_unusable_password()
    user.save(update_fields=["password", "modified"])
    _use_bearer(opts, _token(user, int(time.time())))
    resp = opts.client.post("/api/account/close", {})
    opts.client.logout()

    assert_eq(resp.status_code, 200,
              f"fresh passwordless session should close, got {resp.status_code}: {resp.json}")
    assert_true(not User.objects.get(pk=user.pk).is_active,
                "fresh passwordless closure must deactivate the account")


@th.django_unit_test("account close: stale passwordless session gets fixed reauth sentence")
def test_account_close_passwordless_stale(opts):
    _clear_limits()
    user = _make_user("stale")
    user.set_unusable_password()
    user.save(update_fields=["password", "modified"])
    for label, auth_time in (("legacy", None), ("stale", int(time.time()) - 601)):
        _use_bearer(opts, _token(user, auth_time))
        resp = opts.client.post("/api/account/close", {})
        assert_eq(resp.status_code, 400,
                  f"{label} passwordless session should get 400, got {resp.status_code}")
        assert_eq(_error(resp), "Sign in again to delete your account",
                  f"{label} passwordless session should get the frozen sentence, got {resp.json}")
    opts.client.logout()


@th.django_unit_test("account close: missing and wrong password share generic sentence")
def test_account_close_bad_password(opts):
    _clear_limits()
    user = _make_user("bad_password")
    assert_true(opts.client.login(user.username, PASSWORD), "login must succeed")
    for body in ({}, {"current_password": "wrong"}):
        resp = opts.client.post("/api/account/close", body)
        assert_eq(resp.status_code, 400,
                  f"bad password proof should get 400, got {resp.status_code}: {resp.json}")
        assert_eq(_error(resp), "Incorrect password",
                  f"missing and wrong password must share the fixed sentence, got {resp.json}")
    opts.client.logout()


@th.django_unit_test("account close: wrong guesses share login account throttle")
def test_account_close_wrong_password_throttled(opts):
    from mojo.apps.incident.models.event import Event
    from mojo.decorators.limits import clear_rate_limits

    _clear_limits()
    user = _make_user("throttle")
    assert_true(opts.client.login(user.username, PASSWORD), "login must succeed")
    for attempt in range(11):
        clear_rate_limits(ip="127.0.0.1", key="account_close")
        resp = opts.client.post("/api/account/close", {"current_password": f"wrong-{attempt}"})
    opts.client.logout()

    assert_eq(resp.status_code, 429,
              f"the eleventh account-scoped guess must be throttled, got {resp.status_code}: {resp.json}")
    assert_true(Event.objects.filter(uid=user.pk, category="invalid_password").exists(),
                "wrong password must report the login invalid_password incident")


@th.django_unit_test("account close: UserAPIKey bearer is forbidden")
def test_account_close_user_api_key_forbidden(opts):
    from mojo.apps.account.models import UserAPIKey

    _clear_limits()
    user = _make_user("user_key")
    package = UserAPIKey.create_for_user(user, label="closure refusal")
    _use_bearer(opts, package.token)
    resp = opts.client.post("/api/account/close", {"current_password": PASSWORD})
    opts.client.logout()
    assert_eq(resp.status_code, 403,
              f"UserAPIKey must not close an account, got {resp.status_code}: {resp.json}")


@th.django_unit_test("account close: group ApiKey bearer is forbidden")
def test_account_close_group_api_key_forbidden(opts):
    from mojo.apps.account.models import ApiKey, Group

    _clear_limits()
    user = _make_user("group_key")
    group = Group.objects.create(name=f"Closure group {user.pk}")
    group.add_member(user)
    _key, raw = ApiKey.create_for_group(
        group, "closure refusal", permissions={}, user=user, override_user=True)
    opts.client.logout()
    opts.client.bearer = "apikey"
    opts.client.access_token = raw
    opts.client.is_authenticated = True
    resp = opts.client.post("/api/account/close", {"current_password": PASSWORD})
    opts.client.logout()
    assert_eq(resp.status_code, 403,
              f"group ApiKey must not close an account, got {resp.status_code}: {resp.json}")


@th.django_unit_test("account close: unauthenticated request is refused")
def test_account_close_unauthenticated(opts):
    _clear_limits()
    opts.client.logout()
    resp = opts.client.post("/api/account/close", {})
    assert_true(resp.status_code in (401, 403),
                f"unauthenticated closure must be refused, got {resp.status_code}: {resp.json}")


@th.unit_test("account close: request body is always treated as sensitive")
def test_account_close_body_is_sensitive(opts):
    from mojo.helpers.request import sensitive_body_label

    request = th.get_mock_request(path="/api/account/close")
    request.method = "POST"
    assert_eq(sensitive_body_label(request), "account_auth",
              "account close carries current_password and must never be body-logged")
