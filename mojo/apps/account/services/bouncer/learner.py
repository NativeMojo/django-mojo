"""
BotLearner — background job that registers bot signatures after confirmed blocks.

Published by on_bouncer_assess when risk_score >= BOUNCER_LEARN_MIN_SCORE.
Counts reports per /24 and per signal set, records an event when a /24
reaches its threshold, writes a `signal_set` BotSignature for a campaign and
updates the Redis signature cache used by pre-screen.

The learner never blocks a network: every report it counts is sent by the
caller, so a `subnet_24` signature written from them lets five requests lock
out everyone behind a shared address. A `subnet_24` row an older release
learned is no longer enforced either. It also never changes a signature a
person made or switched off.

The learner never writes, and the cache never enforces, an automatic
`user_agent` or `fingerprint` signature: the caller chooses both values, so a
learned one lets any caller block everyone who shares them. Only an operator's
(source='manual') signature of those two types is enforced.
"""
import hashlib
import json
from datetime import timedelta

from mojo.helpers import dates, logit
from mojo.helpers.redis import get_connection
from mojo.helpers.settings import settings

logger = logit.get_logger('bouncer', 'bouncer.log')

_SUBNET_PREFIX = 'bouncer:learn:subnet24:'
_CAMPAIGN_PREFIX = 'bouncer:learn:campaign:'
# v2: the cache no longer holds automatic user_agent/fingerprint signatures.
# v3: nor a learned subnet_24 one. A job worker still running older code
# rebuilds an older key, which is not read.
SIG_CACHE_KEY = 'bouncer:sigs:active:v3'
# Signature types whose value the caller chooses: never learned, and a row of
# either type with source='auto' is never enforced.
CALLER_CHOSEN_SIG_TYPES = ('user_agent', 'fingerprint')
# Distinct /24 networks that may record a threshold event in one hour.
SUBNET_EVENT_BUDGET = 20


def learn_from_block(job):
    """
    Background job: register bot signatures after a confirmed high-confidence block.

    Payload keys: duid, ip, fingerprint_id, risk_score, triggered_signals, user_agent

    The payload's user_agent and fingerprint_id are not learned from: both are
    chosen by the caller (see CALLER_CHOSEN_SIG_TYPES).
    """
    if not settings.get_static('BOUNCER_LEARN_ENABLED', True):
        return

    p = job.payload
    muid = p.get('muid', '')
    duid = p.get('duid', '')
    ip = p.get('ip', '')
    risk_score = p.get('risk_score', 0)
    triggered_signals = p.get('triggered_signals', [])

    min_score = settings.get_static('BOUNCER_LEARN_MIN_SCORE', 80)
    if risk_score < min_score:
        return

    # 1. Mark device as blocked
    if muid:
        from mojo.apps.account.models.bouncer_device import BouncerDevice
        BouncerDevice.objects.filter(muid=muid).update(risk_tier='blocked')

    redis = get_connection()
    window = 3600  # 1-hour rolling window for subnet escalation

    # 2. Subnet /24 escalation
    if ip:
        _check_subnet(ip, redis, window)

    # 3. Campaign (signal_set) detection
    if triggered_signals:
        _check_campaign(triggered_signals, redis)

    # 4. Refresh Redis signature cache
    refresh_sig_cache()


def _subnet24(ip):
    parts = ip.split('.')
    if len(parts) == 4:
        return '.'.join(parts[:3]) + '.0/24'
    return None


def _check_subnet(ip, redis, window):
    subnet = _subnet24(ip)
    if not subnet:
        return
    threshold = settings.get_static('BOUNCER_LEARN_SUBNET_THRESHOLD', 5)
    key = f"{_SUBNET_PREFIX}{subnet}"
    count = redis.incr(key)
    if count == 1:
        redis.expire(key, window)
    # No signature: the reports are the caller's own word. A person decides.
    if count == threshold:
        _report_subnet(subnet, count, window)


def _report_subnet(subnet, count, window):
    """Record that a /24 reached the report threshold, for an operator to list.

    Level 5, below the level that opens an incident, and with no address: no
    rule blocks on it and no automatic triage runs on it. Suppressed per
    subnet and capped per hour, failing closed, since a caller who can choose
    the address the server sees could otherwise file one per network.
    """
    from mojo.apps.incident.reporter import report_event_suppressed
    report_event_suppressed(
        f"Bouncer: {count} high-score reports from {subnet} within an hour. "
        "No block was added.",
        subnet,
        category='security:bouncer:subnet',
        scope='account',
        level=5,
        window=window,
        budget=SUBNET_EVENT_BUDGET,
        fail_open=False,
        subnet=subnet,
        report_count=count,
    )


def _check_campaign(triggered_signals, redis):
    threshold = settings.get_static('BOUNCER_LEARN_CAMPAIGN_THRESHOLD', 5)
    ttl = settings.get_static('BOUNCER_LEARN_SIGNAL_SET_TTL', 2592000)
    sig_hash = hashlib.sha256(
        json.dumps(sorted(triggered_signals)).encode()
    ).hexdigest()[:16]
    key = f"{_CAMPAIGN_PREFIX}{sig_hash}"
    count = redis.incr(key)
    if count == 1:
        redis.expire(key, 86400)
    if count >= threshold:
        _upsert_signature('signal_set', sig_hash, 'auto', min(count * 8, 85), ttl)
        if count == threshold:
            _fire_campaign_incident(sig_hash, count)


def _upsert_signature(sig_type, value, source, confidence, ttl_seconds):
    from mojo.apps.account.models.bot_signature import BotSignature
    expires_at = dates.utcnow() + timedelta(seconds=ttl_seconds)
    sig, created = BotSignature.objects.get_or_create(
        sig_type=sig_type,
        value=value,
        defaults={
            'source': source,
            'confidence': confidence,
            'expires_at': expires_at,
            'is_active': True,
            'block_count': 1,
        },
    )
    if not created:
        # A row a person made, or switched off, is theirs: left as it is.
        if sig.source != 'auto' or not sig.is_active:
            return
        sig.block_count += 1
        sig.confidence = max(sig.confidence, confidence)
        sig.expires_at = expires_at  # extend TTL on repeated blocks
        sig.save(update_fields=['block_count', 'confidence', 'expires_at', 'modified'])
    try:
        from mojo.apps import metrics
        metrics.record("bouncer:signatures_learned", category="bouncer")
    except Exception:
        pass


def _fire_campaign_incident(sig_hash, count):
    from mojo.apps import incident
    try:
        from mojo.apps import metrics
        metrics.record("bouncer:campaigns", category="bouncer")
    except Exception:
        pass
    incident.report_event(
        f"Coordinated bot campaign detected: signal_set={sig_hash} count={count}",
        category='security:bouncer:campaign',
        scope='account',
        level=10,
        campaign_hash=sig_hash,
        campaign_count=count,
    )


def refresh_sig_cache():
    """
    Rebuild the Redis cache of active signatures for fast pre-screen lookup.
    Called at the end of every learn job and every 15 minutes by the
    `refresh_bouncer_sig_cache` cron job, so a manual signature outlives the
    one-hour life of the key on a quiet site.

    Left out, so that one an older release learned is no longer enforced:
    automatic (source='auto') user_agent and fingerprint rows, and a
    subnet_24 row the learner wrote, which is source='auto' with an expiry.
    Everything else loads: every `ip` row (the learner never wrote one), and a
    subnet_24 row with another source or with no expiry, which only a person
    can have made.
    """
    from mojo.apps.account.models.bot_signature import BotSignature
    now = dates.utcnow()
    active = BotSignature.objects.filter(is_active=True).exclude(
        expires_at__lt=now
    ).values('sig_type', 'value', 'source', 'expires_at')

    sigs_by_type = {}
    for sig in active:
        if sig['sig_type'] in CALLER_CHOSEN_SIG_TYPES and sig['source'] == 'auto':
            continue
        if sig['sig_type'] == 'subnet_24' and sig['source'] == 'auto' \
                and sig['expires_at'] is not None:
            continue
        sigs_by_type.setdefault(sig['sig_type'], []).append(sig['value'])

    redis = get_connection()
    redis.set(SIG_CACHE_KEY, json.dumps(sigs_by_type), ex=3600)


def check_signature_cache(request_ip, user_agent='', fingerprint_id='', *, redis=None, strict=False):
    """
    Check Redis signature cache for pre-screen blocks.
    Returns (matched, sig_type, value) or (False, None, None).
    Fast path — O(1) lookup before any scoring runs.
    """
    redis = get_connection() if redis is None else redis
    try:
        raw = redis.get(SIG_CACHE_KEY)
        if not raw:
            return False, None, None
        sigs = json.loads(raw)
    except Exception:
        if strict:
            raise
        return False, None, None

    if request_ip and 'ip' in sigs:
        if request_ip in sigs['ip']:
            return True, 'ip', request_ip

    subnet = _subnet24(request_ip) if request_ip else None
    if subnet and 'subnet_24' in sigs:
        if subnet in sigs['subnet_24']:
            return True, 'subnet_24', subnet

    if user_agent and 'user_agent' in sigs:
        if user_agent in sigs['user_agent']:
            return True, 'user_agent', user_agent

    if fingerprint_id and 'fingerprint' in sigs:
        if fingerprint_id in sigs['fingerprint']:
            return True, 'fingerprint', fingerprint_id

    return False, None, None
