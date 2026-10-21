"""
#9109: a push token already registered to another user, or to the same user
under another device_id, must move to the newest registration without a 500,
and the previous owner must stop holding it.
"""
import uuid

from testit import helpers as th

OWNER_A = "push_tok_owner_a"
OWNER_B = "push_tok_owner_b"
PWORD = "push##tok99"


@th.django_unit_setup()
def setup_token_owners(opts):
    from mojo.apps.account.models import User, RegisteredDevice

    RegisteredDevice.objects.filter(user__username__in=[OWNER_A, OWNER_B]).delete()
    User.objects.filter(username__in=[OWNER_A, OWNER_B]).delete()
    for name in (OWNER_A, OWNER_B):
        user = User(username=name, email=f"{name}@test.com")
        user.save()
        user.is_active = True
        user.is_email_verified = True
        user.save_password(PWORD)
        user.save()


def _token():
    return f"tok9109-{uuid.uuid4().hex}"


def _assert_moved(token, platform, new_user, new_device_id):
    from mojo.apps.account.models import RegisteredDevice

    rows = list(RegisteredDevice.objects.filter(device_token=token, platform=platform))
    assert len(rows) == 1, f"expected one row for the token, got {len(rows)}"
    row = rows[0]
    assert row.user.username == new_user, f"token still owned by {row.user.username}, expected {new_user}"
    assert row.device_id == new_device_id, f"token on device_id {row.device_id}, expected {new_device_id}"
    assert row.is_active and row.push_enabled, "new registration should be active"


@th.django_unit_test()
def test_register_token_owned_by_other_user(opts):
    from mojo.apps.account.models import User, RegisteredDevice

    token = _token()
    owner_a = User.objects.get(username=OWNER_A)
    RegisteredDevice.objects.create(user=owner_a, device_token=token,
                                    device_id=f"a-{uuid.uuid4().hex[:8]}", platform="android")

    opts.client.login(OWNER_B, PWORD)
    device_id = f"b-{uuid.uuid4().hex[:8]}"
    resp = opts.client.post("/api/account/devices/push/register", json={
        "device_token": token, "device_id": device_id, "platform": "android"})
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}"
    _assert_moved(token, "android", OWNER_B, device_id)
    assert not owner_a.registered_devices.filter(device_token=token).exists(), \
        "previous owner still holds the token"


@th.django_unit_test()
def test_register_token_same_user_other_device_id(opts):
    from mojo.apps.account.models import User, RegisteredDevice

    token = _token()
    owner_a = User.objects.get(username=OWNER_A)
    RegisteredDevice.objects.create(user=owner_a, device_token=token,
                                    device_id=f"old-{uuid.uuid4().hex[:8]}", platform="ios")

    opts.client.login(OWNER_A, PWORD)
    device_id = f"new-{uuid.uuid4().hex[:8]}"
    resp = opts.client.post("/api/account/devices/push/register", json={
        "device_token": token, "device_id": device_id, "platform": "ios"})
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}"
    _assert_moved(token, "ios", OWNER_A, device_id)


@th.django_unit_test()
def test_legacy_register_token_owned_by_other_user(opts):
    from mojo.apps.account.models import RegisteredDevice

    token = _token()
    opts.client.login(OWNER_A, PWORD)
    resp = opts.client.post("/api/account/member/device/register", json={
        "cmf_token": token, "device_id": f"a-{uuid.uuid4().hex[:8]}"})
    assert resp.status_code == 200, f"seed registration failed: {resp.status_code}"
    platform = RegisteredDevice.objects.get(device_token=token).platform

    opts.client.login(OWNER_B, PWORD)
    device_id = f"b-{uuid.uuid4().hex[:8]}"
    resp = opts.client.post("/api/account/member/device/register", json={
        "cmf_token": token, "device_id": device_id})
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}"
    _assert_moved(token, platform, OWNER_B, device_id)


@th.django_unit_test()
def test_legacy_register_token_same_user_other_device_id(opts):
    from mojo.apps.account.models import RegisteredDevice

    token = _token()
    opts.client.login(OWNER_A, PWORD)
    resp = opts.client.post("/api/account/member/device/register", json={
        "cmf_token": token, "device_id": f"old-{uuid.uuid4().hex[:8]}"})
    assert resp.status_code == 200, f"seed registration failed: {resp.status_code}"
    platform = RegisteredDevice.objects.get(device_token=token).platform

    device_id = f"new-{uuid.uuid4().hex[:8]}"
    resp = opts.client.post("/api/account/member/device/register", json={
        "cmf_token": token, "device_id": device_id})
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}"
    _assert_moved(token, platform, OWNER_A, device_id)
