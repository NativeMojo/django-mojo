"""
BotLearner — background job that registers bot signatures after confirmed blocks.

Published by on_bouncer_assess when risk_score >= BOUNCER_LEARN_MIN_SCORE.
Checks subnet/UA/fingerprint/campaign escalation thresholds and writes
BotSignature entries + updates the Redis signature cache used by pre-screen.
"""
import hashlib
import json
from datetime import timedelta

from mojo.helpers import dates, logit
from mojo.helpers.redis import get_connection
from mojo.helpers.settings import settings

logger = logit.get_logger('bouncer', 'bouncer.log')

_SUBNET_PREFIX = 'bouncer:learn:subnet24:'
_UA_PREFIX = 'bouncer:learn:ua:'
_FP_PREFIX = 'bouncer:learn:fp:'
_CAMPAIGN_PREFIX = 'bouncer:learn:campaign:'
SIG_CACHE_KEY = 'bouncer:sigs:active'


def learn_from_block(job):
    """
    Background job: register bot signatures after a confirmed high-confidence block.

    Payload keys: duid, ip, fingerprint_id, risk_score, triggered_signals, user_agent
    """
    if not settings.get_static('BOUNCER_LEARN_ENABLED', True):
        return

    p = job.payload
    muid = p.get('muid', '')
    duid = p.get('duid', '')
    ip = p.get('ip', '')
    fingerprint_id = p.get('fingerprint_id', '')
    risk_score = p.get('risk_score', 0)
    triggered_signals = p.get('triggered_signals', [])
    user_agent = p.get('user_agent', '')

    min_score = settings.get_static('BOUNCER_LEARN_MIN_SCORE', 80)
    if risk_score < min_score:
        return

    # 1. Mark device as blocked
    if muid:
        from mojo.apps.account.models.bouncer_device import BouncerDevice
        BouncerDevice.objects.filter(muid=muid).update(risk_tier='blocked')

    redis = get_connection()
    window = 3600  # 1-hour rolling window for subnet/UA escalation

    # 2. Subnet /24 escalation
    if ip:
        _check_subnet(ip, redis, window)

    # 3. UA escalation
    if user_agent:
        _check_user_agent(user_agent, redis, window)

    # 4. Fingerprint escalation
    if fingerprint_id:
        _check_fingerprint(fingerprint_id, redis)

    # 5. Campaign (signal_set) detection
    if triggered_signals:
        _check_campaign(triggered_signals, redis)

    # 6. Refresh Redis signature cache
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
    ttl = settings.get_static('BOUNCER_LEARN_SUBNET_TTL', 86400)
    key = f"{_SUBNET_PREFIX}{subnet}"
    count = redis.incr(key)
    if count == 1:
        redis.expire(key, window)
    if count >= threshold:
        _upsert_signature('subnet_24', subnet, 'auto', min(count * 10, 90), ttl)


def _check_user_agent(ua, redis, window):
    threshold = settings.get_static('BOUNCER_LEARN_UA_THRESHOLD', 5)
    ttl = settings.get_static('BOUNCER_LEARN_UA_TTL', 604800)
    ua_hash = hashlib.md5(ua.encode()).hexdigest()
    key = f"{_UA_PREFIX}{ua_hash}"
    count = redis.incr(key)
    if count == 1:
        redis.expire(key, window)
    if count >= threshold:
        _upsert_signature('user_agent', ua[:512], 'auto', min(count * 10, 90), ttl)


def _check_fingerprint(fingerprint_id, redis):
    threshold = settings.get_static('BOUNCER_LEARN_FP_THRESHOLD', 3)
    ttl = settings.get_static('BOUNCER_LEARN_UA_TTL', 604800)
    key = f"{_FP_PREFIX}{fingerprint_id}"
    count = redis.incr(key)
    redis.expire(key, 86400 * 30)
    if count >= threshold:
        _upsert_signature('fingerprint', fingerprint_id, 'auto', min(count * 15, 95), ttl)


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
        sig.block_count += 1
        sig.confidence = max(sig.confidence, confidence)
        sig.expires_at = expires_at  # extend TTL on repeated blocks
        sig.is_active = True
        sig.save(update_fields=['block_count', 'confidence', 'expires_at', 'is_active', 'modified'])


def _fire_campaign_incident(sig_hash, count):
    from mojo.apps import incident
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
    Called after every signature upsert. Also safe to call on a schedule.
    """
    from mojo.apps.account.models.bot_signature import BotSignature
    now = dates.utcnow()
    active = BotSignature.objects.filter(is_active=True).exclude(
        expires_at__lt=now
    ).values('sig_type', 'value')

    sigs_by_type = {}
    for sig in active:
        sigs_by_type.setdefault(sig['sig_type'], []).append(sig['value'])

    redis = get_connection()
    redis.set(SIG_CACHE_KEY, json.dumps(sigs_by_type), ex=3600)


def check_signature_cache(request_ip, user_agent='', fingerprint_id=''):
    """
    Check Redis signature cache for pre-screen blocks.
    Returns (matched, sig_type, value) or (False, None, None).
    Fast path — O(1) lookup before any scoring runs.
    """
    redis = get_connection()
    try:
        raw = redis.get(SIG_CACHE_KEY)
        if not raw:
            return False, None, None
        sigs = json.loads(raw)
    except Exception:
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
