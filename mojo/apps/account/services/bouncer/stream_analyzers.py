"""
Universal stream analyzers shipped with django-mojo.

App-agnostic detections that work for any continuous-session bot pattern.
Each analyzer reads the signal_window passed by the scorer — never queries
the DB independently. Apps register domain-specific analyzers (gameplay,
financial, etc.) via the same `@register_stream_analyzer` decorator.

Score deltas are hardcoded — at 5 analyzers and tens of points each, putting
them behind a settings dict is more configuration surface than value. If
tuning becomes a real need, a parallel weight-config can be added later.
"""
import math

from mojo.apps.account.services.bouncer.stream_scoring import (
    BaseStreamAnalyzer, register_stream_analyzer,
)


def _events_iter(signal_window):
    """Yield (event_dict, raw_signals_dict) for each BouncerSignal row in window.

    Sentinel batches arrive as one BouncerSignal per event. raw_signals on the
    row carries {event_type, data, context, ...} for sentinel-pushed rows, or
    arbitrary keys for game-backend direct writes.
    """
    for sig in signal_window:
        raw = sig.raw_signals or {}
        yield sig, raw


def _sum_scalar(signal_window, key):
    total = 0
    for _sig, raw in _events_iter(signal_window):
        data = raw.get('data') if isinstance(raw.get('data'), dict) else raw
        v = data.get(key, 0)
        if isinstance(v, (int, float)):
            total += v
    return total


def _max_scalar(signal_window, key):
    best = 0
    for _sig, raw in _events_iter(signal_window):
        data = raw.get('data') if isinstance(raw.get('data'), dict) else raw
        v = data.get(key, 0)
        if isinstance(v, (int, float)) and v > best:
            best = v
    return best


@register_stream_analyzer
class ExtendedSessionNoIdleAnalyzer(BaseStreamAnalyzer):
    """Session running for hours without idle gaps — bot endurance signature."""
    name = 'extended_session_no_idle'

    @classmethod
    def analyze(cls, muid, signal_window, device):
        if not signal_window:
            return 0, []
        lifetime_ms = _max_scalar(signal_window, 'page_lifetime_ms')
        idle_gaps = _sum_scalar(signal_window, 'idle_gaps_count')
        hours = lifetime_ms / 3_600_000.0
        if idle_gaps > 0:
            return 0, []
        if hours >= 12:
            return 25, [cls.name + ':12h']
        if hours >= 8:
            return 20, [cls.name + ':8h']
        if hours >= 4:
            return 15, [cls.name + ':4h']
        return 0, []


@register_stream_analyzer
class TabNeverHiddenAnalyzer(BaseStreamAnalyzer):
    """Multi-hour session with zero tab-visibility transitions — bots don't tab away."""
    name = 'tab_never_hidden'

    @classmethod
    def analyze(cls, muid, signal_window, device):
        if not signal_window:
            return 0, []
        lifetime_ms = _max_scalar(signal_window, 'page_lifetime_ms')
        transitions = _sum_scalar(signal_window, 'visibility_transitions')
        if lifetime_ms < 4 * 3_600_000:
            return 0, []
        if transitions == 0:
            return 20, [cls.name]
        return 0, []


@register_stream_analyzer
class CoordinateQuantizationAnalyzer(BaseStreamAnalyzer):
    """Many clicks falling into very few coordinate buckets — macro signature."""
    name = 'coordinate_quantization'

    @classmethod
    def analyze(cls, muid, signal_window, device):
        if not signal_window:
            return 0, []
        # `click_count` is total click events; `click_coord_buckets` is the
        # set of distinct (x/8, y/8) buckets sentinel observed across the
        # window. A bot replaying canned coordinates shows many clicks in
        # a tiny bucket set.
        clicks = _sum_scalar(signal_window, 'click_count')
        # Bucket set is reported as a list per batch; union across batches.
        buckets = set()
        for _sig, raw in _events_iter(signal_window):
            data = raw.get('data') if isinstance(raw.get('data'), dict) else raw
            arr = data.get('click_coord_buckets') or []
            if isinstance(arr, list):
                for b in arr:
                    if isinstance(b, str):
                        buckets.add(b)
        if clicks > 100 and len(buckets) < 5:
            return 25, [cls.name]
        return 0, []


@register_stream_analyzer
class ActionIntervalRegularAnalyzer(BaseStreamAnalyzer):
    """Lag-1 autocorrelation > 0.9 on inter-action intervals — macro replay."""
    name = 'action_interval_regular'

    @classmethod
    def analyze(cls, muid, signal_window, device):
        if not signal_window:
            return 0, []
        intervals = []
        for _sig, raw in _events_iter(signal_window):
            data = raw.get('data') if isinstance(raw.get('data'), dict) else raw
            arr = data.get('inter_action_interval_ms') or []
            if isinstance(arr, list):
                for v in arr:
                    if isinstance(v, (int, float)) and v >= 0:
                        intervals.append(float(v))
        if len(intervals) < 50:
            return 0, []
        # Pearson lag-1 autocorrelation, simple implementation
        a = intervals[:-1]
        b = intervals[1:]
        n = len(a)
        mean_a = sum(a) / n
        mean_b = sum(b) / n
        num = sum((a[i] - mean_a) * (b[i] - mean_b) for i in range(n))
        var_a = sum((x - mean_a) ** 2 for x in a)
        var_b = sum((x - mean_b) ** 2 for x in b)
        denom = math.sqrt(var_a * var_b)
        if denom == 0:
            # Zero variance means perfectly regular intervals — strongest signal.
            return 25, [cls.name]
        corr = num / denom
        if corr > 0.9:
            return 25, [cls.name]
        return 0, []


@register_stream_analyzer
class PasteIntoSensitiveFieldAnalyzer(BaseStreamAnalyzer):
    """Paste event with target a password field — credential-stuffing signature."""
    name = 'paste_into_sensitive_field'

    SENSITIVE_TAGS = ('input[type=password]', 'password')

    @classmethod
    def analyze(cls, muid, signal_window, device):
        for _sig, raw in _events_iter(signal_window):
            event_type = raw.get('event_type', '')
            data = raw.get('data') if isinstance(raw.get('data'), dict) else raw
            if event_type == 'paste_event' or 'paste' in raw.get('category', ''):
                target = (data.get('target_tag') or '').lower()
                if target in cls.SENSITIVE_TAGS:
                    return 15, [cls.name]
        return 0, []
