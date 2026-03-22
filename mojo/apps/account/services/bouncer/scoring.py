from mojo.helpers import logit
from mojo.helpers.settings import settings

logger = logit.get_logger('bouncer', 'bouncer.log')


class ScoringResult:
    def __init__(self, score, decision, triggered_signals, signal_scores, metadata=None):
        self.score = score
        self.decision = decision
        self.triggered_signals = triggered_signals
        self.signal_scores = signal_scores
        self.metadata = metadata or {}


class ScoringContext:
    def __init__(self, client_signals, server_signals, device_session, page_type, request=None):
        self.client_signals = client_signals
        self.server_signals = server_signals
        self.device_session = device_session
        self.page_type = page_type
        self.request = request


# ---------------------------------------------------------------------------
# Pluggable analyzer registry
# ---------------------------------------------------------------------------

_ANALYZER_REGISTRY = []


def register_analyzer(cls):
    """Class decorator — registers an analyzer in the global scoring pipeline."""
    _ANALYZER_REGISTRY.append(cls)
    return cls


class BaseSignalAnalyzer:
    """
    Base for pluggable signal analyzers.

    Subclass, set `name`, implement `analyze(context)` returning
    (score_contribution, list_of_triggered_signal_names), then decorate
    with @register_analyzer.
    """
    name = 'base'

    @classmethod
    def analyze(cls, context):
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------

_DEFAULT_WEIGHTS = {
    'webdriver_flag': 25,
    'playwright_artifacts': 30,
    'puppeteer_artifacts': 30,
    'outer_size_zero': 20,
    'headless_ua': 20,
    'languages_empty': 15,
    'screen_zero': 20,
    'chrome_runtime_missing': 20,
    'document_focus_never': 15,
    'no_interaction': 20,
    'first_interaction_too_fast': 15,
    'rapid_click': 20,
    'mouse_straightness': 15,
    'geo_vpn': 10,
    'geo_tor': 35,
    'geo_proxy': 15,
    'geo_datacenter': 15,
    'geo_known_attacker': 40,
    'geo_known_abuser': 30,
    'header_missing_accept': 10,
    'header_missing_accept_language': 10,
    'header_headless_ua': 20,
    'signal_contradiction': 20,
    'history_blocked_device': 60,
    'history_high_risk_device': 30,
    'history_high_event_count': 10,
    'gate_honeypot_filled': 50,
    'gate_click_too_fast': 20,
    'gate_no_interaction_desktop': 25,
    'gate_excessive_attempts': 15,
    'form_instant_fill': 30,
    'form_no_focus': 20,
    'muid_missing': 10,
    'muid_duid_mismatch': 15,
    'muid_duid_changed': 10,
    'msid_missing': 5,
    'concurrent_mtabs': 20,
    'mtab_missing': 10,
    'muid_multi_user': 25,
    'muid_ip_drift': 15,
    'msid_too_long': 10,
}


def _weight(signal_name):
    weights = settings.get_static('BOUNCER_SCORE_WEIGHTS') or _DEFAULT_WEIGHTS
    return weights.get(signal_name, 0)


def _gate_threshold(key, default):
    thresholds = settings.get_static('BOUNCER_GATE_CHALLENGE') or {}
    return thresholds.get(key, default)


# ---------------------------------------------------------------------------
# Built-in analyzers — ported from mojo-verify bouncer
# ---------------------------------------------------------------------------

@register_analyzer
class EnvironmentAnalyzer(BaseSignalAnalyzer):
    """Detects headless browsers, automation frameworks, spoofed APIs."""
    name = 'environment'

    BOOLEAN_SIGNALS = [
        'webdriver_flag',
        'phantom_globals',
        'nightmare_global',
        'selenium_artifacts',
        'chrome_runtime_missing',
        'languages_empty',
        'screen_zero',
        'webgl_missing',
        'plugins_zero',
        'mobile_touch_mismatch',
        'notification_missing',
        'eval_modified',
        'native_fn_spoofed',
        'document_focus_never',
        'playwright_artifacts',
        'puppeteer_artifacts',
        'outer_size_zero',
        'connection_missing',
        'device_memory_missing',
    ]

    @classmethod
    def analyze(cls, context):
        if not context.client_signals:
            return 0, []
        env = context.client_signals.get('environment', {})
        score = 0
        triggered = []
        for sig in cls.BOOLEAN_SIGNALS:
            if env.get(sig):
                w = _weight(sig)
                score += w
                if w > 0:
                    triggered.append(sig)
        return score, triggered


@register_analyzer
class BehaviorAnalyzer(BaseSignalAnalyzer):
    """Scores mouse/scroll/keystroke behavior during the gate window."""
    name = 'behavior'

    @classmethod
    def analyze(cls, context):
        if not context.client_signals:
            return 0, []
        behavior = context.client_signals.get('behavior', {})
        mouse = context.client_signals.get('mouse', {})
        score = 0
        triggered = []

        total_events = (
            behavior.get('mouse_move_count', 0)
            + behavior.get('scroll_event_count', 0)
            + behavior.get('keystroke_count', 0)
            + behavior.get('touch_event_count', 0)
        )
        if total_events == 0:
            w = _weight('no_interaction')
            score += w
            if w > 0:
                triggered.append('no_interaction')

        first = behavior.get('first_interaction_ms')
        if first is not None and first < 100:
            w = _weight('first_interaction_too_fast')
            score += w
            if w > 0:
                triggered.append('first_interaction_too_fast')

        if behavior.get('rapid_click'):
            w = _weight('rapid_click')
            score += w
            if w > 0:
                triggered.append('rapid_click')

        if behavior.get('page_hidden_on_load'):
            w = _weight('page_hidden_on_load')
            score += w
            if w > 0:
                triggered.append('page_hidden_on_load')

        straightness = mouse.get('straightness_score')
        if straightness is not None and straightness > 0.85:
            w = _weight('mouse_straightness')
            score += w
            if w > 0:
                triggered.append('mouse_straightness')

        accel_var = mouse.get('acceleration_variance')
        if accel_var is not None and accel_var < 0.05:
            w = _weight('mouse_low_acceleration_variance')
            score += w
            if w > 0:
                triggered.append('mouse_low_acceleration_variance')

        return score, triggered


@register_analyzer
class GeoAnalyzer(BaseSignalAnalyzer):
    """Scores GeoIP threat signals."""
    name = 'geo'

    SIGNALS = [
        ('is_vpn', 'geo_vpn'),
        ('is_tor', 'geo_tor'),
        ('is_proxy', 'geo_proxy'),
        ('is_datacenter', 'geo_datacenter'),
        ('is_known_attacker', 'geo_known_attacker'),
        ('is_known_abuser', 'geo_known_abuser'),
    ]

    @classmethod
    def analyze(cls, context):
        geo = context.server_signals.get('geo', {})
        score = 0
        triggered = []
        for geo_key, weight_key in cls.SIGNALS:
            if geo.get(geo_key):
                w = _weight(weight_key)
                score += w
                if w > 0:
                    triggered.append(weight_key)
        return score, triggered


@register_analyzer
class HeaderAnalyzer(BaseSignalAnalyzer):
    """Scores suspicious or missing HTTP headers."""
    name = 'headers'

    @classmethod
    def analyze(cls, context):
        headers = context.server_signals.get('headers', {})
        score = 0
        triggered = []

        checks = [
            ('missing_accept', 'header_missing_accept'),
            ('missing_accept_language', 'header_missing_accept_language'),
            ('headless_ua', 'header_headless_ua'),
            ('signal_contradiction', 'signal_contradiction'),
        ]
        for field, weight_key in checks:
            if headers.get(field):
                w = _weight(weight_key)
                score += w
                if w > 0:
                    triggered.append(weight_key)
        return score, triggered


@register_analyzer
class HistoryAnalyzer(BaseSignalAnalyzer):
    """Scores device session history."""
    name = 'history'

    @classmethod
    def analyze(cls, context):
        ds = context.device_session
        if ds is None:
            return 0, []

        score = 0
        triggered = []

        if ds.risk_tier == 'blocked':
            w = _weight('history_blocked_device')
            score += w
            if w > 0:
                triggered.append('history_blocked_device')
        elif ds.risk_tier == 'high':
            w = _weight('history_high_risk_device')
            score += w
            if w > 0:
                triggered.append('history_high_risk_device')

        if ds.event_count > 50:
            w = _weight('history_high_event_count')
            score += w
            if w > 0:
                triggered.append('history_high_event_count')

        return score, triggered


@register_analyzer
class FormSignalAnalyzer(BaseSignalAnalyzer):
    """Scores Stage 2 form-fill behavior signals."""
    name = 'form'

    @classmethod
    def analyze(cls, context):
        form = context.client_signals.get('form_signals', {})
        if not form:
            return 0, []

        score = 0
        triggered = []

        fill_time = form.get('form_fill_total_ms')
        if fill_time is not None and fill_time < 500:
            w = _weight('form_instant_fill')
            score += w
            if w > 0:
                triggered.append('form_instant_fill')

        focus_time = form.get('time_to_first_focus_ms')
        if focus_time is not None and focus_time == 0:
            w = _weight('form_no_focus')
            score += w
            if w > 0:
                triggered.append('form_no_focus')

        return score, triggered


@register_analyzer
class GateChallengeAnalyzer(BaseSignalAnalyzer):
    """Scores the interactive gate challenge signals."""
    name = 'gate_challenge'

    @classmethod
    def analyze(cls, context):
        challenge = context.client_signals.get('gate_challenge', {})
        if not challenge:
            return 0, []

        if challenge.get('session_cookie_skipped'):
            return 0, []

        score = 0
        triggered = []

        if challenge.get('honeypot_filled'):
            w = _weight('gate_honeypot_filled')
            score += w
            if w > 0:
                triggered.append('gate_honeypot_filled')

        time_to_click = challenge.get('time_to_click_ms')
        min_time = _gate_threshold('min_time_to_click_ms', 800)
        if time_to_click is not None and time_to_click < min_time:
            w = _weight('gate_click_too_fast')
            score += w
            if w > 0:
                triggered.append('gate_click_too_fast')

        is_touch = challenge.get('is_touch_device', False)
        if not is_touch:
            if not challenge.get('had_mouse_movement') and not challenge.get('had_touch_events'):
                w = _weight('gate_no_interaction_desktop')
                score += w
                if w > 0:
                    triggered.append('gate_no_interaction_desktop')

        attempt = challenge.get('attempt_number', 1)
        max_attempts = _gate_threshold('max_attempts_before_penalty', 3)
        if attempt > max_attempts:
            w = _weight('gate_excessive_attempts')
            score += w
            if w > 0:
                triggered.append('gate_excessive_attempts')

        return score, triggered


@register_analyzer
class IdentityAnalyzer(BaseSignalAnalyzer):
    """
    Scores identity correlation signals across the four identity layers:
    muid (server cookie), duid (client localStorage), msid (session cookie),
    mtab (tab sessionStorage).

    Detects: cookie-blocking bots, identity rotation, multi-tab automation,
    multi-account credential stuffing, never-closing bots.
    """
    name = 'identity'

    @classmethod
    def analyze(cls, context):
        request = context.request
        if not request:
            return 0, []

        score = 0
        triggered = []

        muid = getattr(request, 'muid', '') or ''
        duid = getattr(request, 'duid', '') or ''
        msid = getattr(request, 'msid', '') or ''
        mtab = getattr(request, 'mtab', '') or ''
        # Also check duid from client signals payload
        client_duid = ''
        if context.client_signals:
            client_duid = context.client_signals.get('duid', '') or ''
        if not duid and client_duid:
            duid = client_duid

        # muid_missing: no server cookie — first visit or cookie-blocking bot
        if not muid:
            w = _weight('muid_missing')
            score += w
            if w > 0:
                triggered.append('muid_missing')

        # msid_missing: no session cookie
        if not msid:
            w = _weight('msid_missing')
            score += w
            if w > 0:
                triggered.append('msid_missing')

        # mtab_missing: no tab session — non-JS client or blocking
        if not mtab:
            w = _weight('mtab_missing')
            score += w
            if w > 0:
                triggered.append('mtab_missing')

        # muid_duid_mismatch: check if this muid has a known duid pairing
        if muid and duid and context.device_session:
            known_duid = context.device_session.duid
            if known_duid and known_duid != duid:
                w = _weight('muid_duid_changed')
                score += w
                if w > 0:
                    triggered.append('muid_duid_changed')

        # concurrent_mtabs: too many active tab sessions per muid
        if muid and mtab:
            try:
                concurrent = cls._count_recent_mtabs(muid)
                threshold = settings.get_static('BOUNCER_CONCURRENT_MTAB_LIMIT', 4)
                if concurrent > threshold:
                    w = _weight('concurrent_mtabs')
                    score += w
                    if w > 0:
                        triggered.append('concurrent_mtabs')
            except Exception:
                pass

        # msid_too_long: same session cookie active too long (never-closing bot)
        if muid and msid:
            try:
                session_age = cls._session_age_hours(muid, msid)
                if session_age and session_age > 24:
                    w = _weight('msid_too_long')
                    score += w
                    if w > 0:
                        triggered.append('msid_too_long')
            except Exception:
                pass

        return score, triggered

    @classmethod
    def _count_recent_mtabs(cls, muid):
        """Count distinct mtab values for this muid in the last 5 minutes."""
        from mojo.apps.account.models.bouncer_signal import BouncerSignal
        from mojo.helpers import dates
        from datetime import timedelta
        cutoff = dates.utcnow() - timedelta(minutes=5)
        return BouncerSignal.objects.filter(
            muid=muid, created__gte=cutoff
        ).exclude(mtab='').values('mtab').distinct().count()

    @classmethod
    def _session_age_hours(cls, muid, msid):
        """Return hours since the first signal with this msid for this muid."""
        from mojo.apps.account.models.bouncer_signal import BouncerSignal
        from mojo.helpers import dates
        first = BouncerSignal.objects.filter(
            muid=muid, msid=msid
        ).order_by('created').values_list('created', flat=True).first()
        if not first:
            return None
        delta = dates.utcnow() - first
        return delta.total_seconds() / 3600


# ---------------------------------------------------------------------------
# Risk scorer
# ---------------------------------------------------------------------------

class RiskScorer:
    """
    Composites all registered analyzers into a single 0–100 risk score.

    Weights: BOUNCER_SCORE_WEIGHTS (per-signal point values)
    Thresholds: BOUNCER_THRESHOLDS (default block/monitor cutoffs)
    Per-page overrides: BOUNCER_THRESHOLDS_OVERRIDES
    """

    @classmethod
    def score(cls, context):
        total = 0
        all_triggered = []
        signal_scores = {}

        for analyzer_cls in _ANALYZER_REGISTRY:
            try:
                contribution, triggered = analyzer_cls.analyze(context)
                total += contribution
                all_triggered.extend(triggered)
                signal_scores[analyzer_cls.name] = contribution
            except Exception:
                logger.exception(f"bouncer: analyzer {analyzer_cls.name} failed")

        total = min(total, 100)
        decision = cls.decide(total, context.page_type)
        return ScoringResult(
            score=total,
            decision=decision,
            triggered_signals=all_triggered,
            signal_scores=signal_scores,
        )

    @classmethod
    def decide(cls, score, page_type):
        thresholds = settings.get_static('BOUNCER_THRESHOLDS') or {'block': 60, 'monitor': 40}
        overrides = settings.get_static('BOUNCER_THRESHOLDS_OVERRIDES') or {}
        if page_type in overrides:
            thresholds = {**thresholds, **overrides[page_type]}
        if score >= thresholds.get('block', 60):
            return 'block'
        if score >= thresholds.get('monitor', 40):
            return 'monitor'
        return 'allow'
