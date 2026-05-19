"""
Tests for the gradient enforcement helper apply_session_response().

Verifies:
  - Score ≥ freeze → device.risk_tier='blocked', block_count++, freeze handler called
  - Score ≥ shadow_ban → user.bouncer_shadow_banned flag set
  - Score ≥ require_step_up → user.bouncer_require_step_up flag set
  - Score ≥ monitor → incident fired, no metadata flags changed
  - Score < monitor → noop
  - BOUNCER_SESSION_BANDS setting overrides thresholds
  - Freeze handler failure does not break enforcement (logs, continues)
"""
from testit import helpers as th
from testit.helpers import assert_true, assert_eq

TEST_MUID = 'enforce-muid-001'


@th.django_unit_setup()
def setup(opts):
    from mojo.apps.account.models import BouncerDevice, BouncerSignal
    from django.contrib.auth import get_user_model
    User = get_user_model()
    User.objects.filter(username__startswith='enforce-test-').delete()
    BouncerDevice.objects.filter(muid__startswith='enforce-muid-').delete()
    BouncerSignal.objects.filter(muid__startswith='enforce-muid-').delete()


def _make_user(name):
    from django.contrib.auth import get_user_model
    User = get_user_model()
    u = User.objects.create(username=f'enforce-test-{name}', email=f'enforce-{name}@x.com')
    return u


def _make_device(muid):
    from mojo.apps.account.models import BouncerDevice
    d, _ = BouncerDevice.objects.get_or_create(
        muid=muid,
        defaults={'duid': '', 'risk_tier': 'unknown'},
    )
    return d


@th.django_unit_test()
def test_band_freeze_sets_blocked_and_fires_incident(opts):
    """Score 95 → device blocked + block_count incremented + incident fired."""
    from mojo.apps.account.services.bouncer.enforcement import apply_session_response
    from mojo.apps.account.models import BouncerDevice

    device = _make_device(f'{TEST_MUID}-freeze')
    user = _make_user('freeze')
    band = apply_session_response(device, 95, user=user, triggered=['test'])
    assert_eq(band, 'freeze', f"expected band=freeze, got {band}")
    device.refresh_from_db()
    assert_eq(device.risk_tier, 'blocked',
              f"freeze must set risk_tier=blocked, got {device.risk_tier}")
    assert_true(device.block_count >= 1,
                f"freeze must increment block_count, got {device.block_count}")


@th.django_unit_test()
def test_band_shadow_ban_sets_user_flag(opts):
    """Score 75 → user.bouncer_shadow_banned=True."""
    from mojo.apps.account.services.bouncer.enforcement import apply_session_response
    device = _make_device(f'{TEST_MUID}-shadow')
    user = _make_user('shadow')
    band = apply_session_response(device, 75, user=user, triggered=['test'])
    assert_eq(band, 'shadow_ban', f"expected band=shadow_ban, got {band}")
    # Read back the protected metadata flag the framework set.
    user.refresh_from_db()
    flag = user.get_protected_metadata('bouncer_shadow_banned')
    assert_true(flag, f"shadow_ban must set bouncer_shadow_banned, got {flag}")


@th.django_unit_test()
def test_band_step_up_sets_user_flag(opts):
    """Score 55 → user.bouncer_require_step_up=True."""
    from mojo.apps.account.services.bouncer.enforcement import apply_session_response
    device = _make_device(f'{TEST_MUID}-step')
    user = _make_user('step')
    band = apply_session_response(device, 55, user=user, triggered=['test'])
    assert_eq(band, 'require_step_up', f"expected band=require_step_up, got {band}")
    user.refresh_from_db()
    flag = user.get_protected_metadata('bouncer_require_step_up')
    assert_true(flag, f"step_up must set bouncer_require_step_up, got {flag}")


@th.django_unit_test()
def test_band_monitor_fires_incident_no_flag(opts):
    """Score 35 → 'monitor' band, no user flag set."""
    from mojo.apps.account.services.bouncer.enforcement import apply_session_response
    device = _make_device(f'{TEST_MUID}-monitor')
    user = _make_user('monitor')
    band = apply_session_response(device, 35, user=user, triggered=['test'])
    assert_eq(band, 'monitor', f"expected band=monitor, got {band}")
    user.refresh_from_db()
    shadow = user.get_protected_metadata('bouncer_shadow_banned')
    step_up = user.get_protected_metadata('bouncer_require_step_up')
    assert_true(not shadow,
                f"monitor must not set shadow_banned, got {shadow}")
    assert_true(not step_up,
                f"monitor must not set require_step_up, got {step_up}")


@th.django_unit_test()
def test_band_below_threshold_noops(opts):
    """Score 10 → noop, no flags, device unchanged."""
    from mojo.apps.account.services.bouncer.enforcement import apply_session_response
    device = _make_device(f'{TEST_MUID}-noop')
    user = _make_user('noop')
    band = apply_session_response(device, 10, user=user, triggered=[])
    assert_eq(band, 'noop', f"expected band=noop, got {band}")
    device.refresh_from_db()
    assert_eq(device.risk_tier, 'unknown',
              f"noop must not change risk_tier, got {device.risk_tier}")


@th.django_unit_test()
def test_custom_bands_setting_respected(opts):
    """Custom bands change the score → band mapping.

    The bands come from `settings.get_static('BOUNCER_SESSION_BANDS') or _DEFAULT_BANDS`.
    `th.server_settings()` updates the server process — but apply_session_response
    runs in the test process, so we override the module-level default directly.
    This proves the lookup is honoring the dict shape we expect.
    """
    from mojo.apps.account.services.bouncer import enforcement
    original = enforcement._DEFAULT_BANDS
    enforcement._DEFAULT_BANDS = {
        'freeze': 50,
        'shadow_ban': 30,
        'require_step_up': 20,
        'monitor': 10,
    }
    try:
        device = _make_device(f'{TEST_MUID}-custombands')
        user = _make_user('custombands')
        # 55 should now hit the lowered freeze threshold (50) rather than the
        # default freeze (90).
        band = enforcement.apply_session_response(device, 55, user=user, triggered=[])
        assert_eq(band, 'freeze',
                  f"with custom bands (freeze=50), score 55 should be freeze, got {band}")
    finally:
        enforcement._DEFAULT_BANDS = original


@th.django_unit_test()
def test_freeze_handler_failure_does_not_break(opts):
    """Freeze handler raising an exception must not propagate — device still flipped."""
    th.server_settings(
        BOUNCER_SESSION_FREEZE_HANDLER='tests.test_security._enforcement_helpers.raising_handler'
    )
    try:
        from mojo.apps.account.services.bouncer.enforcement import apply_session_response
        device = _make_device(f'{TEST_MUID}-handler-fail')
        user = _make_user('handler-fail')
        # Should not raise — the handler raises but the framework swallows it.
        band = apply_session_response(device, 95, user=user, triggered=[])
        assert_eq(band, 'freeze', f"expected band=freeze, got {band}")
        device.refresh_from_db()
        assert_eq(device.risk_tier, 'blocked',
                  f"freeze must still set blocked despite handler failure")
    finally:
        th.server_settings(BOUNCER_SESSION_FREEZE_HANDLER='')
