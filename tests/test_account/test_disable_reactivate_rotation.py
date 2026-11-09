"""Turning an account off or back on replaces its signing key (maestro #9533).

Session tokens and emailed links are signed with User.auth_key, and
validate_jwt refuses only while the account is inactive. Before this item,
re-enabling an account kept the key, so every token signed with it worked
again: ones minted during the disable, and — when the account was turned off
by a direct `is_active` write — ones from before it too.

Every path that flips `is_active` now writes a new key and clears the cached
invite: the disable and reactivate service, and the admin REST save of
`is_active`. Tokens "minted during the disable" are made in process with the
live key, so these tests do not depend on what the sign-in flows do for a
disabled account (#9463).

Test-owned users only, created delete-then-create. No settings writes, no
patches. The package's default tier, not `core`: one core tag holds all of
test_account to the strict scan, which its existing protected-setting tests
fail (testit/isolation.py scan_package).
"""
from testit import helpers as th

ADMIN_EMAIL = "rot9533_admin@rotation.test"
ADMIN_PASSWORD = "Rot9533##admin"
TARGET_PASSWORD = "Rot9533##target"


def _null_reporter(*args, **kwargs):
    return None


def _make_user(email, password):
    from mojo.apps.account.models import User
    User.objects.filter(email=email).delete()
    user = User.objects.create_user(username=email, email=email, password=password)
    user.is_active = True
    user.is_email_verified = True
    user.requires_mfa = False
    user.save()
    return user


def _mint(user_pk):
    """A session token signed with the account's live key. validate_jwt
    returns (user, error) for a bad signature, so a refusal is `error`."""
    from mojo.apps.account.models import User
    from mojo.apps.account.utils.jwtoken import JWToken
    user = User.objects.get(pk=user_pk)
    return JWToken(user.get_auth_key()).create(uid=user.pk).access_token


def _admin_save(opts, user_pk, data):
    resp = opts.client.post(f"/api/user/{user_pk}", data)
    assert resp.status_code == 200, (
        f"admin save {data} on user {user_pk} should succeed, got "
        f"{resp.status_code}: {opts.client.last_response.body}")
    return resp


@th.django_unit_setup()
def setup_rotation(opts):
    admin = _make_user(ADMIN_EMAIL, ADMIN_PASSWORD)
    admin.add_permission("manage_users")
    opts.admin_id = admin.pk


@th.django_unit_test()
def test_admin_off_then_on_kills_token_from_before(opts):
    from mojo.apps.account.models import User

    target = _make_user("rot9533_t1@rotation.test", TARGET_PASSWORD)
    token = _mint(target.pk)
    validated, error = User.validate_jwt(token)
    assert error is None, f"sanity: token must validate while active, got {error!r}"

    assert opts.client.login(ADMIN_EMAIL, ADMIN_PASSWORD), "admin login failed"
    _admin_save(opts, target.pk, {"is_active": False})
    _admin_save(opts, target.pk, {"is_active": True})
    opts.client.logout()

    assert User.objects.get(pk=target.pk).is_active is True, "admin save should re-enable the account"
    validated, error = User.validate_jwt(token)
    assert error is not None, (
        "a token issued before an admin is_active=false write must stay dead after "
        "the account is re-enabled")


@th.django_unit_test()
def test_reactivate_action_kills_token_minted_during_disable(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.services import disable as disable_service

    target = _make_user("rot9533_t2@rotation.test", TARGET_PASSWORD)
    disable_service.disable_entity(target, reason="admin", reporter=_null_reporter)
    token = _mint(target.pk)
    key_during = User.objects.get(pk=target.pk).auth_key

    assert opts.client.login(ADMIN_EMAIL, ADMIN_PASSWORD), "admin login failed"
    _admin_save(opts, target.pk, {"reactivate": {"note": "9533"}})
    opts.client.logout()

    fresh = User.objects.get(pk=target.pk)
    assert fresh.is_active is True, "reactivate action should re-enable the account"
    assert fresh.auth_key != key_during, "reactivate must replace the signing key"
    validated, error = User.validate_jwt(token)
    assert error is not None, "a token minted during the disable must stay dead after reactivate"


@th.django_unit_test()
def test_admin_on_kills_token_minted_during_disable(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.services import disable as disable_service

    target = _make_user("rot9533_t3@rotation.test", TARGET_PASSWORD)
    disable_service.disable_entity(target, reason="admin", reporter=_null_reporter)
    token = _mint(target.pk)

    assert opts.client.login(ADMIN_EMAIL, ADMIN_PASSWORD), "admin login failed"
    _admin_save(opts, target.pk, {"is_active": True})
    opts.client.logout()

    assert User.objects.get(pk=target.pk).is_active is True, "admin save should re-enable the account"
    validated, error = User.validate_jwt(token)
    assert error is not None, (
        "a token minted during the disable must stay dead after an admin is_active=true write")


@th.django_unit_test()
def test_admin_off_kills_magic_link_from_before(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens

    target = _make_user("rot9533_t4@rotation.test", TARGET_PASSWORD)
    link = tokens.generate_magic_login_token(User.objects.get(pk=target.pk))

    assert opts.client.login(ADMIN_EMAIL, ADMIN_PASSWORD), "admin login failed"
    _admin_save(opts, target.pk, {"is_active": False})
    opts.client.logout()

    try:
        tokens.verify_magic_login_token(link)
        verified = True
    except Exception:
        verified = False
    assert not verified, "a magic link issued before an admin is_active=false write must not verify"


@th.django_unit_test()
def test_reactivate_mints_a_working_invite(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.services import disable as disable_service
    from mojo.apps.account.utils import tokens

    target = _make_user("rot9533_t5@rotation.test", TARGET_PASSWORD)
    disable_service.disable_entity(target, reason="admin", reporter=_null_reporter)
    cached = tokens.get_or_generate_invite_token(User.objects.get(pk=target.pk))

    disable_service.reactivate_entity(
        User.objects.get(pk=target.pk), note="9533", reporter=_null_reporter)

    fresh = User.objects.get(pk=target.pk)
    invite = tokens.get_or_generate_invite_token(fresh)
    assert invite != cached, "after reactivate the cached invite must be gone and a new one minted"
    verified = tokens.verify_invite_token(invite, consume=False)
    assert verified.pk == target.pk, "the invite minted after reactivate must verify"


@th.django_unit_test()
def test_active_account_key_survives_unrelated_saves(opts):
    from mojo.apps.account.models import User

    target = _make_user("rot9533_t6@rotation.test", TARGET_PASSWORD)
    token = _mint(target.pk)
    key_before = User.objects.get(pk=target.pk).auth_key

    assert opts.client.login(ADMIN_EMAIL, ADMIN_PASSWORD), "admin login failed"
    _admin_save(opts, target.pk, {"display_name": "Rotation Six"})
    _admin_save(opts, target.pk, {"is_active": True})
    opts.client.logout()

    assert opts.client.login("rot9533_t6@rotation.test", TARGET_PASSWORD), "target login failed"
    _admin_save(opts, target.pk, {"display_name": "Rotation Six Self"})
    opts.client.logout()

    assert User.objects.get(pk=target.pk).auth_key == key_before, (
        "an unrelated save, a self save and is_active=true on an active account "
        "must keep the signing key")
    validated, error = User.validate_jwt(token)
    assert error is None, f"an active account's token must survive those saves, got {error!r}"


def _stale_save_racing_disable(opts, target_pk, data):
    """A REST save of a copy loaded while the account was active, paused after
    its pre-save hook while disable_entity commits on a second database
    connection, then resumed. Returns (key_before, token_before)."""
    import threading
    from django.db import connection
    from objict import objict
    from mojo.apps.account.models import User
    from mojo.apps.account.services import disable as disable_service
    from mojo.models.rest import ACTIVE_REQUEST

    key_before = User.objects.get(pk=target_pk).get_auth_key()
    token_before = _mint(target_pk)
    admin = User.objects.get(pk=opts.admin_id)
    stale = User.objects.get(pk=target_pk)
    request = objict(user=admin, group=None, DATA=objict(data), META={}, ip="127.0.0.1",
                     method="POST", path=f"/api/user/{target_pk}")
    errors = []

    def disable_elsewhere():
        try:
            disable_service.disable_entity(
                User.objects.get(pk=target_pk), reason="admin", reporter=_null_reporter)
        except Exception as err:
            errors.append(repr(err))
        finally:
            connection.close()

    hook = stale.on_rest_pre_save

    def pause_after_pre_save(changed_fields, created):
        hook(changed_fields, created)
        worker = threading.Thread(target=disable_elsewhere)
        worker.start()
        worker.join(15)
        assert not worker.is_alive(), "disable on the second connection timed out"
        assert not errors, f"disable on the second connection failed: {errors}"

    stale.on_rest_pre_save = pause_after_pre_save
    token = ACTIVE_REQUEST.set(request)
    try:
        stale.on_rest_save(request, request.DATA)
    finally:
        ACTIVE_REQUEST.reset(token)
    return key_before, token_before


@th.django_unit_test()
def test_stale_is_active_true_save_cannot_undo_a_disable(opts):
    from mojo.apps.account.models import User

    target = _make_user("rot9533_t7@rotation.test", TARGET_PASSWORD)
    key_before, token_before = _stale_save_racing_disable(opts, target.pk, {"is_active": True})

    fresh = User.objects.get(pk=target.pk)
    assert fresh.is_active is False, (
        "a REST {is_active: true} save loaded before a disable must not turn the account back on")
    assert fresh.auth_key != key_before, "a stale REST save must not write back the pre-disable key"
    validated, error = User.validate_jwt(token_before)
    assert error is not None, "a token from before the disable must stay dead after the stale save"


@th.django_unit_test()
def test_stale_unrelated_save_cannot_undo_a_disable(opts):
    from mojo.apps.account.models import User

    target = _make_user("rot9533_t8@rotation.test", TARGET_PASSWORD)
    key_before, token_before = _stale_save_racing_disable(
        opts, target.pk, {"display_name": "Rotation Eight"})

    fresh = User.objects.get(pk=target.pk)
    assert fresh.display_name == "Rotation Eight", "the stale save's own change should still land"
    assert fresh.is_active is False, "a stale REST save of another field must not turn the account back on"
    assert fresh.auth_key != key_before, "a stale REST save must not write back the pre-disable key"
    validated, error = User.validate_jwt(token_before)
    assert error is not None, "a token from before the disable must stay dead after the stale save"
