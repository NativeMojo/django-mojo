"""Strict-posture /api/settings validation moved from
tests/test_geofence/strict_posture.py — it POSTs the protected
GEOFENCE_STRICT_POSTURE key to the generic /api/settings REST path on the
live server, which is unsafe under the parallel default tier (maestro item
#1839). Runs opt-in (`extended`) and serial.

NOTE: only "false" is ever persisted here — a global strict=true row would
deny unheadered requests from other modules if a finally were missed.
"""
import uuid as _uuid
from testit import helpers as th

IP = "127.0.0.1"


def _cleanup_strict_setting():
    from mojo.apps.account.models.setting import Setting
    from mojo.apps.account.services.geofence import cache as gf_cache
    Setting.remove("GEOFENCE_STRICT_POSTURE")
    gf_cache.invalidate_all()


def _make_group(name_prefix, metadata=None):
    from mojo.apps.account.models.group import Group
    grp = Group.objects.create(
        name=f"{name_prefix} {_uuid.uuid4().hex[:8]}",
        is_active=True, metadata=metadata or {})
    grp.get_uuid()
    return grp


@th.django_unit_setup()
def setup_strict_posture_serial(opts):
    from mojo.apps.account.models import User

    _cleanup_strict_setting()

    suffix = _uuid.uuid4().hex[:8]
    opts.admin_email = f"geofence_serstradm_{suffix}@geofence.test"
    opts.admin_password = "Geo##stradm99"
    admin = User.objects.create_user(
        username=opts.admin_email, email=opts.admin_email, password=opts.admin_password)
    admin.is_email_verified = True
    admin.requires_mfa = False
    admin.add_permission(
        ["manage_geofence", "manage_groups", "manage_settings"])
    admin.save()


def _admin_login(opts):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip=IP, key="login")
    ok = opts.client.login(opts.admin_email, opts.admin_password)
    assert ok, f"admin login failed: {opts.client.last_response.body}"


@th.django_unit_test("strict: /api/settings write path validates GEOFENCE_STRICT_POSTURE")
def test_setting_write_validation(opts):
    from mojo.apps.account.models.setting import Setting
    _admin_login(opts)
    try:
        # NOTE: only "false" is ever persisted here — a global strict=true row
        # would deny unheadered requests from parallel test modules.
        resp = opts.client.post(
            "/api/settings", {"key": "GEOFENCE_STRICT_POSTURE", "value": "maybe"})
        assert resp.status_code == 400, \
            f"non-JSON value must 400, got {resp.status_code}"
        resp = opts.client.post(
            "/api/settings", {"key": "GEOFENCE_STRICT_POSTURE", "value": "1"})
        assert resp.status_code == 400, \
            f"non-boolean JSON must 400 (kind=bool coerces garbage truthy), got {resp.status_code}"
        assert Setting.objects.filter(
            key="GEOFENCE_STRICT_POSTURE", group=None).first() is None, \
            "rejected writes must not persist"

        resp = opts.client.post(
            "/api/settings", {"key": "GEOFENCE_STRICT_POSTURE", "value": "false"})
        assert resp.status_code == 200, \
            f"boolean value must save, got {resp.status_code}: {opts.client.last_response.body}"

        # group-scoped rows are dead config for this key — reject loudly
        grp = _make_group("GF StrictScope")
        try:
            resp = opts.client.post(
                "/api/settings",
                {"key": "GEOFENCE_STRICT_POSTURE", "value": "false", "group": grp.pk})
            assert resp.status_code == 400, \
                f"group-scoped strict setting must 400, got {resp.status_code}"
            assert "global-only" in str(opts.client.last_response.body), \
                f"rejection must explain why: {opts.client.last_response.body}"
        finally:
            grp.delete()
    finally:
        _cleanup_strict_setting()
        opts.client.logout()
