"""
Streaming bouncer scorer.

Continuous in-session bot detection. Parallel to RiskScorer (one-shot gate)
in scoring.py; this scorer walks a sliding window of BouncerSignal rows
keyed on muid and accumulates risk via registered stream analyzers.

Plugin pattern mirrors register_analyzer / BaseSignalAnalyzer:

    @register_stream_analyzer
    class MyAnalyzer(BaseStreamAnalyzer):
        name = 'my_signal'

        @classmethod
        def analyze(cls, muid, signal_window, device):
            return (score_delta, ['my_signal']) if triggered else (0, [])

Score model: monotonic high-water within a TTL window. The Redis key
`bouncer:session_risk:{muid}` only goes up while it lives; on TTL expiry
the score resets and the next scorer run starts fresh. Avoids flapping.
"""
from datetime import timedelta

from mojo.helpers import dates, logit
from mojo.helpers.redis import get_connection
from mojo.helpers.settings import settings

logger = logit.get_logger('bouncer', 'bouncer.log')

_STREAM_REGISTRY = []

_SCORE_KEY_PREFIX = 'bouncer:session_risk:'


def register_stream_analyzer(cls):
    """Class decorator — registers a stream analyzer in the global registry."""
    _STREAM_REGISTRY.append(cls)
    return cls


class BaseStreamAnalyzer:
    """
    Base for pluggable stream analyzers.

    Subclass, set `name`, implement `analyze(muid, signal_window, device)`
    returning (score_delta, list_of_triggered_signal_names), then decorate
    with @register_stream_analyzer.

    Analyzers receive the same signal_window list — don't query the DB,
    work from the rows provided. Apps that need extra context (game state,
    wallet) can read it inside their own analyzers, but the framework's
    universal analyzers must stay self-contained.
    """
    name = 'base'

    @classmethod
    def analyze(cls, muid, signal_window, device):
        raise NotImplementedError


def _window_signals(muid, window_seconds):
    """Read the most recent BouncerSignal rows for this muid within window."""
    from mojo.apps.account.models.bouncer_signal import BouncerSignal
    cutoff = dates.utcnow() - timedelta(seconds=window_seconds)
    return list(
        BouncerSignal.objects.filter(muid=muid, created__gte=cutoff)
        .order_by('-created')[:1000]
    )


def _read_score(redis, muid):
    try:
        raw = redis.get(f"{_SCORE_KEY_PREFIX}{muid}")
        return int(raw) if raw else 0
    except Exception:
        return 0


def _write_score(redis, muid, score, ttl):
    try:
        redis.set(f"{_SCORE_KEY_PREFIX}{muid}", int(score), ex=ttl)
    except Exception:
        logger.exception("bouncer: failed to write session_risk for muid=%s", muid)


def _resolve_user(muid):
    """Look up the post-auth user linked to this muid via UserDevice."""
    try:
        from mojo.apps.account.models.device import UserDevice
        ud = UserDevice.objects.filter(muid=muid).select_related('user').first()
        return ud.user if ud else None
    except Exception:
        return None


def score_session(muid, window_seconds=3600, user=None):
    """
    Score the current session window for `muid`.

    1. Load the last ~1k BouncerSignal rows within `window_seconds`
    2. Run every registered stream analyzer, sum score_delta
    3. Read current Redis high-water; new = min(max(current, current + delta), 100)
    4. Write back with `BOUNCER_SESSION_RISK_TTL` (default 86400)
    5. Apply gradient enforcement against the new score
    6. Return (score, triggered) — None for no muid

    Inline by design — no jobs queue. Cheap: indexed query + a few in-memory
    analyzers. Wrap caller in try/except — scoring failures must not break
    writer paths.

    Pass `user` if the caller already has it (authenticated request handler).
    Otherwise UserDevice is consulted to find the post-auth user for the muid.
    Anonymous pre-auth sessions still update device.risk_tier.
    """
    if not muid:
        return None

    signal_window = _window_signals(muid, window_seconds)

    from mojo.apps.account.models.bouncer_device import BouncerDevice
    device = BouncerDevice.objects.filter(muid=muid).first()

    total_delta = 0
    all_triggered = []
    for analyzer_cls in _STREAM_REGISTRY:
        try:
            delta, triggered = analyzer_cls.analyze(muid, signal_window, device)
            total_delta += delta
            if triggered:
                all_triggered.extend(triggered)
        except Exception:
            logger.exception(
                "bouncer: stream analyzer %s failed for muid=%s",
                analyzer_cls.name, muid,
            )

    redis = get_connection()
    current = _read_score(redis, muid)
    # Monotonic high-water: never decrease while the TTL window is live.
    new_score = min(max(current, current + total_delta), 100)
    ttl = settings.get_static('BOUNCER_SESSION_RISK_TTL', 86400)
    _write_score(redis, muid, new_score, ttl)

    # Enforcement is its own module so apps can read flags without pulling
    # the scorer's whole dependency graph.
    try:
        from mojo.apps.account.services.bouncer.enforcement import apply_session_response
        if user is None:
            user = _resolve_user(muid)
        apply_session_response(device, new_score, user=user, triggered=all_triggered)
    except Exception:
        logger.exception(
            "bouncer: enforcement failed for muid=%s score=%s", muid, new_score
        )

    return new_score, all_triggered
