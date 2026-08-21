"""Setting write-validation matrices moved from
tests/test_geofence/settings_validation.py — every test here POSTs protected
GEOFENCE_* keys to the generic /api/settings REST path on the live server,
which is unsafe under the parallel default tier (maestro item #1839). Runs
opt-in (`extended`) and serial.

Valid-write tests persist only DEFAULT-EQUAL values (zero behavioral impact if
a finally is missed); everything is restored in finally via Setting.remove +
decision-cache invalidation, and no strict=true row is ever persisted.
"""
import uuid as _uuid
from testit import helpers as th

# key -> (garbage payloads that must 400, default-equal valid payload)
GARBAGE = {
    "GEOFENCE_ENABLED": ["garbage"],                    # not JSON at all
    "GEOFENCE_FAIL_CLOSED": ["[1,2"],                   # truncated JSON
    "GEOFENCE_ALLOW_PRIVATE_IPS": ['"yes"'],            # JSON string, not boolean
    "GEOFENCE_CACHE_TTL": ["true", "-5"],               # bool-as-int trap; negative
    "GEOFENCE_FAIL_CLOSED_SCOPES": ['{"a": 1}', '["payments", ""]'],
}
VALID = {
    "GEOFENCE_ENABLED": "true",
    "GEOFENCE_FAIL_CLOSED": "false",
    "GEOFENCE_ALLOW_PRIVATE_IPS": "true",
    "GEOFENCE_CACHE_TTL": "300",
    "GEOFENCE_FAIL_CLOSED_SCOPES": '["item023-scope"]',
}


def _cleanup_settings():
    from mojo.apps.account.models.setting import Setting
    from mojo.apps.account.services.geofence import cache as gf_cache
    for key in VALID:
        Setting.remove(key)
    gf_cache.invalidate_all()


def _row(key):
    from mojo.apps.account.models.setting import Setting
    return Setting.objects.filter(key=key, group=None).first()


def _login(opts):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1", key="login")
    ok = opts.client.login(opts.admin_email, opts.admin_password)
    assert ok, f"admin login failed: {opts.client.last_response.body}"


@th.django_unit_setup()
def setup_settings_validation_serial(opts):
    from mojo.apps.account.models import User

    # Long-lived DB: clear anything a previous run left behind BEFORE creating.
    _cleanup_settings()

    suffix = _uuid.uuid4().hex[:8]
    opts.admin_email = f"item023_seradmin_{suffix}@geofence.test"
    opts.admin_password = "Item023##admin99"
    admin = User.objects.create_user(
        username=opts.admin_email, email=opts.admin_email, password=opts.admin_password)
    admin.is_email_verified = True
    admin.requires_mfa = False
    admin.add_permission("manage_settings")
    admin.save()


@th.django_unit_test("settings: /api/settings rejects garbage for every geofence posture key")
def test_backdoor_garbage_rejected(opts):
    _login(opts)
    try:
        for key, payloads in GARBAGE.items():
            for payload in payloads:
                resp = opts.client.post("/api/settings", {"key": key, "value": payload})
                assert resp.status_code == 400, (
                    f"{key}={payload!r} must be rejected at write time, "
                    f"got {resp.status_code}: {opts.client.last_response.body}"
                )
                assert _row(key) is None, \
                    f"rejected write of {key} must not persist a row"
    finally:
        _cleanup_settings()
        opts.client.logout()


@th.django_unit_test("settings: /api/settings accepts valid (default-equal) posture values")
def test_backdoor_valid_accepted(opts):
    _login(opts)
    try:
        for key, payload in VALID.items():
            resp = opts.client.post("/api/settings", {"key": key, "value": payload})
            assert resp.status_code == 200, (
                f"valid {key}={payload!r} must save, "
                f"got {resp.status_code}: {opts.client.last_response.body}"
            )
            assert _row(key) is not None, f"valid write of {key} must persist"
    finally:
        _cleanup_settings()
        opts.client.logout()


@th.django_unit_test("settings: group-scoped rows rejected for newly-validated posture keys")
def test_backdoor_group_scoped_rejected(opts):
    from mojo.apps.account.models.group import Group
    _login(opts)
    grp = Group.objects.create(
        name=f"Item023 SettingScope {_uuid.uuid4().hex[:8]}", is_active=True)
    try:
        resp = opts.client.post(
            "/api/settings",
            {"key": "GEOFENCE_ENABLED", "value": "true", "group": grp.pk})
        assert resp.status_code == 400, \
            f"group-scoped GEOFENCE_ENABLED must 400, got {resp.status_code}"
        assert "global-only" in str(opts.client.last_response.body), \
            f"rejection must explain why: {opts.client.last_response.body}"
    finally:
        grp.delete()
        _cleanup_settings()
        opts.client.logout()


@th.django_unit_test("settings: is_secret cannot bypass validation on registered keys")
def test_secret_flag_rejected_for_registered_keys(opts):
    from mojo import errors as merrors
    from mojo.apps.account.models.setting import Setting
    _login(opts)
    try:
        # The bypass vector: is_secret used to short-circuit validation, so
        # garbage could persist (masked as ****** to boot).
        resp = opts.client.post(
            "/api/settings",
            {"key": "GEOFENCE_ENABLED", "value": "garbage", "is_secret": True})
        assert resp.status_code == 400, (
            f"is_secret garbage write to a registered key must 400, "
            f"got {resp.status_code}: {opts.client.last_response.body}"
        )
        assert _row("GEOFENCE_ENABLED") is None, \
            "is_secret bypass write must not persist a row"
        # Even a VALID value can't be secret on a registered key.
        resp = opts.client.post(
            "/api/settings",
            {"key": "GEOFENCE_ENABLED", "value": "true", "is_secret": True})
        assert resp.status_code == 400, \
            f"registered keys must reject is_secret entirely, got {resp.status_code}"

        raised = False
        try:
            Setting.set("GEOFENCE_FAIL_CLOSED", False, is_secret=True)
        except merrors.ValueException:
            raised = True
        assert raised, "Setting.set(is_secret=True) on a registered key must raise"
    finally:
        _cleanup_settings()
        opts.client.logout()
