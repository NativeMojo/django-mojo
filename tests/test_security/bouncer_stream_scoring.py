"""
Tests for stream_scoring.score_session() and the 5 universal analyzers.

Verifies:
  - ExtendedSessionNoIdleAnalyzer fires at 4h / 8h / 12h thresholds
  - TabNeverHiddenAnalyzer fires when long session has 0 visibility transitions
  - CoordinateQuantizationAnalyzer fires on >100 clicks in <5 buckets
  - ActionIntervalRegularAnalyzer fires on lag-1 autocorrelation > 0.9
  - PasteIntoSensitiveFieldAnalyzer fires when paste lands in a password input
  - Human-pattern signal window scores 0 (no analyzer triggers)
  - Monotonic high-water: score never decreases on a quiet rerun
  - Score capped at 100
"""
from datetime import timedelta

from testit import helpers as th
from testit.helpers import assert_true, assert_eq


TEST_MUID = 'stream-score-muid-001'


def _seed_signal(muid, event_type, data, created=None):
    from mojo.apps.account.models import BouncerSignal
    from mojo.helpers import dates
    sig = BouncerSignal.objects.create(
        muid=muid,
        duid='',
        stage='event',
        ip_address='127.0.0.1',
        page_type='gameplay',
        raw_signals={'event_type': event_type, 'data': data},
        server_signals={},
        risk_score=0,
        decision='log',
        triggered_signals=[event_type],
    )
    if created is not None:
        # Direct timestamp override since auto_now_add fixes this on create.
        from mojo.apps.account.models import BouncerSignal as BS
        BS.objects.filter(pk=sig.pk).update(created=created)


def _clear_score(muid):
    from mojo.helpers.redis import get_connection
    try:
        get_connection().delete(f"bouncer:session_risk:{muid}")
    except Exception:
        pass


@th.django_unit_setup()
def setup(opts):
    from mojo.apps.account.models import BouncerSignal, BouncerDevice
    BouncerSignal.objects.filter(muid__startswith='stream-score-').delete()
    BouncerDevice.objects.filter(muid__startswith='stream-score-').delete()
    _clear_score(TEST_MUID)


def _fresh_muid(suffix):
    muid = f'stream-score-{suffix}'
    _clear_score(muid)
    from mojo.apps.account.models import BouncerSignal, BouncerDevice
    BouncerSignal.objects.filter(muid=muid).delete()
    BouncerDevice.objects.filter(muid=muid).delete()
    return muid


@th.django_unit_test()
def test_extended_session_no_idle_triggers_at_thresholds(opts):
    """Long no-idle session triggers with severity scaling on hours."""
    from mojo.apps.account.services.bouncer.stream_scoring import score_session

    # 4h with zero idle gaps → +15
    muid_4h = _fresh_muid('endurance-4h')
    _seed_signal(muid_4h, 'sentinel_snapshot', {
        'page_lifetime_ms': 4 * 3_600_000 + 1000,
        'idle_gaps_count': 0,
        'visibility_transitions': 2,
    })
    result_4h = score_session(muid_4h)
    assert_true(result_4h is not None, "expected score result, got None")
    score_4h, triggered_4h = result_4h
    assert_true(score_4h >= 15, f"expected >=15 at 4h no-idle, got {score_4h}")
    assert_true(any('extended_session_no_idle' in t for t in triggered_4h),
                f"expected extended_session_no_idle trigger, got {triggered_4h}")

    # 8h tier
    muid_8h = _fresh_muid('endurance-8h')
    _seed_signal(muid_8h, 'sentinel_snapshot', {
        'page_lifetime_ms': 8 * 3_600_000 + 1000,
        'idle_gaps_count': 0,
        'visibility_transitions': 5,
    })
    score_8h, _ = score_session(muid_8h)
    assert_true(score_8h >= 20, f"expected >=20 at 8h no-idle, got {score_8h}")

    # 12h tier
    muid_12h = _fresh_muid('endurance-12h')
    _seed_signal(muid_12h, 'sentinel_snapshot', {
        'page_lifetime_ms': 12 * 3_600_000 + 1000,
        'idle_gaps_count': 0,
        'visibility_transitions': 10,
    })
    score_12h, _ = score_session(muid_12h)
    assert_true(score_12h >= 25, f"expected >=25 at 12h no-idle, got {score_12h}")


@th.django_unit_test()
def test_extended_session_no_idle_does_not_trigger_with_idle(opts):
    """Same long lifetime but with idle gaps observed → analyzer does NOT fire."""
    from mojo.apps.account.services.bouncer.stream_scoring import score_session
    muid = _fresh_muid('endurance-with-idle')
    _seed_signal(muid, 'sentinel_snapshot', {
        'page_lifetime_ms': 12 * 3_600_000,
        'idle_gaps_count': 5,
        'visibility_transitions': 10,
    })
    score, triggered = score_session(muid)
    assert_true(not any('extended_session_no_idle' in t for t in triggered),
                f"endurance must not trigger with idle gaps present, got {triggered}")


@th.django_unit_test()
def test_tab_never_hidden_triggers(opts):
    """4h+ lifetime, 0 visibility transitions across window → fires."""
    from mojo.apps.account.services.bouncer.stream_scoring import score_session
    muid = _fresh_muid('tab-never-hidden')
    _seed_signal(muid, 'sentinel_snapshot', {
        'page_lifetime_ms': 5 * 3_600_000,
        'visibility_transitions': 0,
        'idle_gaps_count': 1,  # don't trigger the endurance analyzer too
    })
    score, triggered = score_session(muid)
    assert_true('tab_never_hidden' in triggered,
                f"expected tab_never_hidden trigger, got {triggered}")


@th.django_unit_test()
def test_coordinate_quantization_triggers(opts):
    """>100 clicks but <5 distinct buckets → macro signature."""
    from mojo.apps.account.services.bouncer.stream_scoring import score_session
    muid = _fresh_muid('coord-quant')
    _seed_signal(muid, 'sentinel_snapshot', {
        'click_count': 150,
        'click_coord_buckets': ['10,20', '30,40', '50,60'],
    })
    score, triggered = score_session(muid)
    assert_true('coordinate_quantization' in triggered,
                f"expected coordinate_quantization trigger, got {triggered}")


@th.django_unit_test()
def test_action_interval_regular_triggers(opts):
    """50+ intervals all the same → autocorrelation 1.0 → fires."""
    from mojo.apps.account.services.bouncer.stream_scoring import score_session
    muid = _fresh_muid('action-regular')
    # Constant intervals — zero variance, treated as max-suspicious.
    _seed_signal(muid, 'sentinel_snapshot', {
        'inter_action_interval_ms': [1000] * 60,
    })
    score, triggered = score_session(muid)
    assert_true('action_interval_regular' in triggered,
                f"expected action_interval_regular trigger, got {triggered}")


@th.django_unit_test()
def test_paste_into_password_triggers(opts):
    """Paste event into input[type=password] → fires."""
    from mojo.apps.account.services.bouncer.stream_scoring import score_session
    muid = _fresh_muid('paste-pwd')
    _seed_signal(muid, 'paste_event', {'target_tag': 'input[type=password]'})
    score, triggered = score_session(muid)
    assert_true('paste_into_sensitive_field' in triggered,
                f"expected paste_into_sensitive_field trigger, got {triggered}")


@th.django_unit_test()
def test_human_pattern_no_triggers(opts):
    """Varied human-like signals → no analyzer fires, score 0."""
    from mojo.apps.account.services.bouncer.stream_scoring import score_session
    muid = _fresh_muid('human-pattern')
    _seed_signal(muid, 'sentinel_snapshot', {
        'page_lifetime_ms': 30 * 60_000,  # 30 minutes
        'visibility_transitions': 5,
        'focus_blur_count': 3,
        'paste_events': 0,
        'click_count': 20,
        'click_coord_buckets': ['10,20', '50,80', '120,200', '300,400', '500,600', '700,800'],
        'inter_action_interval_ms': [543, 1200, 980, 1800, 320, 750, 2100, 450],
        'idle_gaps_count': 2,
    })
    score, triggered = score_session(muid)
    assert_eq(score, 0, f"human pattern should score 0, got {score} (triggered={triggered})")


@th.django_unit_test()
def test_high_water_monotonic(opts):
    """Score never decreases on a quiet rerun within the TTL window."""
    from mojo.apps.account.services.bouncer.stream_scoring import score_session
    muid = _fresh_muid('high-water')
    # First run: signals trigger something.
    _seed_signal(muid, 'sentinel_snapshot', {
        'page_lifetime_ms': 5 * 3_600_000,
        'visibility_transitions': 0,
        'idle_gaps_count': 1,
    })
    score1, _ = score_session(muid)
    assert_true(score1 > 0, f"first run should score > 0, got {score1}")

    # Second run: same window, no new signals. Score must not drop.
    score2, _ = score_session(muid)
    assert_true(score2 >= score1,
                f"score must not decrease: first={score1} second={score2}")


@th.django_unit_test()
def test_score_capped_at_100(opts):
    """Many analyzers firing simultaneously → score is capped at 100."""
    from mojo.apps.account.services.bouncer.stream_scoring import score_session
    muid = _fresh_muid('cap-100')
    # All analyzers firing in the same window
    _seed_signal(muid, 'sentinel_snapshot', {
        'page_lifetime_ms': 12 * 3_600_000 + 1000,
        'visibility_transitions': 0,
        'idle_gaps_count': 0,
        'click_count': 200,
        'click_coord_buckets': ['10,20', '30,40'],
        'inter_action_interval_ms': [500] * 80,
    })
    _seed_signal(muid, 'paste_event', {'target_tag': 'input[type=password]'})
    # Run twice so the high-water has a chance to climb
    score_session(muid)
    score, triggered = score_session(muid)
    assert_true(score <= 100, f"score must be capped at 100, got {score}")
    assert_true(score >= 90, f"all-fires window should be high, got {score} (triggered={triggered})")


@th.django_unit_test()
def test_score_session_returns_none_for_empty_muid(opts):
    """No muid → no-op, no Redis write, no exception."""
    from mojo.apps.account.services.bouncer.stream_scoring import score_session
    result = score_session('')
    assert_true(result is None, f"empty muid should return None, got {result}")
