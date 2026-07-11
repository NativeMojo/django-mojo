"""ITEM-031 regression — GEOFENCE_TEST_OVERRIDE and MOJO_TEST_MODE are file-only.

Both keys are test/local-dev plumbing and must be read from the Django settings
FILE (settings.get_static), never from the DB/Redis settings plane. Before the
fix they were read via settings.get (DB/Redis-first), so a single global Setting
row (writable via the generic /api/settings REST or a direct Redis HSET) reached
into every geofence decision with no validator, no global-only enforcement, no
decision-cache invalidation, and no evidence event:

  - GEOFENCE_TEST_OVERRIDE: Setting.value is text, and _resolve_geo reads it with
    no kind=, so dict("<json string>") RAISES ValueError -> HTTP 400 on every
    geofenced request (an unaudited availability break).
  - MOJO_TEST_MODE: read with kind="bool", a DB row coerces cleanly to True and
    arms the entire X-Mojo-Test-* header plane process-wide, gated then only by
    loopback + no-proxy.

These tests write REAL global Setting rows; per the config_plane hygiene rules
they clean up in finally (Setting.remove + gf_cache.invalidate_all), never touch
127.0.0.1 / the DB allowlist, and never leave MOJO_TEST_MODE in a surprising
state. django.conf overrides use direct setattr + try/finally (NOT
th.server_settings — these are in-process reads).
"""
from testit import helpers as th

TEST_IP = "203.0.113.7"  # TEST-NET-3 — never a real client in this suite
OVERRIDE = {
    "country_code": "XX", "region_code": "ZZ",
    "is_tor": False, "is_vpn": False, "is_proxy": False, "is_datacenter": False,
}
_SENTINEL = object()


def _save_conf(name):
    from django.conf import settings as dj_settings
    return getattr(dj_settings, name, _SENTINEL)


def _restore_conf(name, orig):
    from django.conf import settings as dj_settings
    if orig is _SENTINEL:
        if hasattr(dj_settings, name):
            delattr(dj_settings, name)
    else:
        setattr(dj_settings, name, orig)


@th.django_unit_setup()
def setup_override_file_only(opts):
    """Clear any leftover rows so the DB/file distinction is unambiguous."""
    from mojo.apps.account.models.setting import Setting
    from mojo.apps.account.services.geofence import cache as gf_cache
    Setting.remove("GEOFENCE_TEST_OVERRIDE")
    Setting.remove("MOJO_TEST_MODE")
    gf_cache.invalidate_all()


@th.django_unit_test("ITEM-031: a DB GEOFENCE_TEST_OVERRIDE row does NOT affect engine resolution")
def test_db_override_ignored(opts):
    """The core regression. Pre-fix, the DB row's JSON string reached
    dict(<str>) and raised ValueError; either way it must not steer resolution."""
    from mojo.apps.account.models.setting import Setting
    from mojo.apps.account.services.geofence import engine, cache as gf_cache

    # premise: no conf value, only a DB row
    conf_orig = _save_conf("GEOFENCE_TEST_OVERRIDE")
    _restore_conf("GEOFENCE_TEST_OVERRIDE", _SENTINEL)  # ensure absent
    Setting.set("GEOFENCE_TEST_OVERRIDE", OVERRIDE)
    try:
        raised = None
        result = None
        try:
            result = engine._resolve_geo(TEST_IP, request=None)
        except Exception as exc:  # pre-fix: ValueError from dict("<json str>")
            raised = exc
        assert raised is None, \
            f"a DB GEOFENCE_TEST_OVERRIDE row must not break resolution, but it raised: {raised!r}"
        assert result != OVERRIDE, \
            f"a DB GEOFENCE_TEST_OVERRIDE row must not be honored, but resolution returned it: {result!r}"
    finally:
        Setting.remove("GEOFENCE_TEST_OVERRIDE")
        gf_cache.invalidate_all()
        _restore_conf("GEOFENCE_TEST_OVERRIDE", conf_orig)


@th.django_unit_test("ITEM-031: a conf-file GEOFENCE_TEST_OVERRIDE is still honored (dev/staging knob)")
def test_conf_override_honored(opts):
    """No over-blocking: the documented conf-file override still substitutes the
    geo lookup, proving the read moved to get_static rather than being removed."""
    from mojo.apps.account.models.setting import Setting
    from mojo.apps.account.services.geofence import engine, cache as gf_cache

    Setting.remove("GEOFENCE_TEST_OVERRIDE")  # no DB row — file only
    gf_cache.invalidate_all()
    conf_orig = _save_conf("GEOFENCE_TEST_OVERRIDE")
    from django.conf import settings as dj_settings
    dj_settings.GEOFENCE_TEST_OVERRIDE = dict(OVERRIDE)
    try:
        result = engine._resolve_geo(TEST_IP, request=None)
        assert result == OVERRIDE, \
            f"conf-file GEOFENCE_TEST_OVERRIDE must still be honored, got: {result!r}"
    finally:
        _restore_conf("GEOFENCE_TEST_OVERRIDE", conf_orig)


@th.django_unit_test("ITEM-031: a DB MOJO_TEST_MODE row does NOT enable test mode when the conf says False")
def test_db_mojo_test_mode_ignored(opts):
    """MOJO_TEST_MODE is the master switch for the X-Mojo-Test-* header plane;
    it must be file-only so a DB/Redis row cannot arm it. Pre-fix, is_enabled()
    read the DB row (True); post-fix it reads the conf file (False)."""
    from mojo.apps.account.models.setting import Setting
    from mojo.helpers import test_mode

    conf_orig = _save_conf("MOJO_TEST_MODE")
    from django.conf import settings as dj_settings
    dj_settings.MOJO_TEST_MODE = False          # conf explicitly off
    Setting.set("MOJO_TEST_MODE", True)         # DB says on — must be ignored
    try:
        assert test_mode.is_enabled() is False, \
            "a DB MOJO_TEST_MODE row must not enable test mode when the conf file says False"
    finally:
        Setting.remove("MOJO_TEST_MODE")
        _restore_conf("MOJO_TEST_MODE", conf_orig)
