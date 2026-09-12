"""Hosted-page recovery policy and protocol. Legacy assess callers do not enter here."""
import re

from mojo.helpers import logit
from mojo.helpers.redis import get_bounded_connection
from mojo.helpers.response import JsonResponse
from mojo.helpers.settings import settings
from mojo.apps.account.services.bouncer.hosted_challenge import ChallengeStore
from mojo.apps.account.services.bouncer.scoring import RiskScorer, ScoringContext

logger = logit.get_logger('bouncer', 'bouncer.log')
_IDENTITY = re.compile(r'^[a-zA-Z0-9_-]{1,64}$')
_CAPABILITY = re.compile(r'^[a-zA-Z0-9_-]{32}$')
_REQUEST_ID = re.compile(r'^[a-zA-Z0-9_-]{8,64}$')
PURPOSES = ('login', 'registration', 'public_message')


def identity(request, *, returning=False):
    value = request.COOKIES.get('_muid', '')
    if not returning:
        value = value or getattr(request, 'muid', '')
    return value if isinstance(value, str) and _IDENTITY.fullmatch(value) else ''


def route(result, page_type, *, matched=False, blocked=False, frozen=False):
    if matched:
        return 'decoy'
    metadata = result.metadata
    if metadata.get('analysis_failed'):
        return 'recovery'
    remaining = metadata['uncapped_score'] - metadata['recovery_credit']
    current = remaining - metadata['historical_block']
    if RiskScorer.decide(current, page_type) == 'block':
        return 'decoy'
    if blocked or frozen or RiskScorer.decide(remaining, page_type) == 'block':
        return 'recovery'
    return 'check' if result.decision == 'allow' else 'slider'


def screen(request, purpose, redis, signals=None):
    from mojo.apps.account.models import BouncerDevice
    from mojo.apps.account.services.bouncer.environment import EnvironmentService
    from mojo.apps.account.services.bouncer.learner import check_signature_cache
    from mojo.apps.account.rest.bouncer.assess import _geolocate

    muid = identity(request)
    device = BouncerDevice.objects.filter(muid=muid).first() if muid else None
    fingerprint = device.fingerprint_id if device else ''
    matched, _, _ = check_signature_cache(
        request.ip, request.user_agent, fingerprint, redis=redis, strict=True)
    raw_risk = redis.get(f'bouncer:session_risk:{muid}') if muid else None
    bands = settings.get_static('BOUNCER_SESSION_BANDS') or {}
    frozen = bool(raw_risk and int(raw_risk) >= bands.get('freeze', 90))
    server_signals = EnvironmentService.analyze_request(request, _geolocate(request.ip))
    result = RiskScorer.score(ScoringContext(
        client_signals=signals or {}, server_signals=server_signals,
        device_session=device, page_type=purpose,
        request=request if signals is not None else None))
    action = route(result, purpose, matched=matched,
                   blocked=bool(device and device.risk_tier == 'blocked'), frozen=frozen)
    return action, result, device, server_signals


def descriptor(request, purpose, group=None, *, action='check', form=False, redis=None):
    muid = identity(request)
    if not muid:
        return {'next_action': 'recovery', 'reason': 'cookies'}
    owned = redis is None
    try:
        if owned:
            redis = get_bounded_connection(timeout=1, read_from_replicas=False)
        store = ChallengeStore(redis, request.get_host().lower(), muid)
        return store.issue(purpose, getattr(group, 'uuid', '') or '', action=action, form=form)
    except Exception:
        logger.warning('bouncer: hosted challenge state unavailable')
        return {'next_action': 'error', 'reason': 'unavailable'}
    finally:
        if owned and redis is not None:
            redis.close()


def page_check(request, purpose, group=None):
    """Return (action, descriptor); a returning pass still honors current restrictions."""
    from mojo.apps.account.rest.bouncer.assess import verify_pass_cookie

    redis = None
    try:
        redis = get_bounded_connection(timeout=1, read_from_replicas=False)
        action, _, _, _ = screen(request, purpose, redis)
        if action in ('decoy', 'recovery'):
            return action, {'next_action': action, 'reason': 'operator'}
        muid = identity(request, returning=True)
        pass_muid = verify_pass_cookie(request.COOKIES.get('mbp', ''), request.ip)
        if muid and pass_muid == muid:
            return 'allow', descriptor(request, purpose, group, form=True, redis=redis)
        return action, descriptor(request, purpose, group, action=action, redis=redis)
    except Exception:
        logger.warning('bouncer: hosted pre-screen unavailable')
        return 'error', {'next_action': 'error', 'reason': 'unavailable'}
    finally:
        if redis is not None:
            redis.close()


def _response(action, *, status=200, **fields):
    return JsonResponse({'status': status < 400, 'data': {
        'decision': 'allow' if action in ('check_cookie', 'allow', 'token') else 'block',
        'next_action': action, **fields}}, status=status)


def _audit(request, record, action, result, device, server_signals):
    """No event endpoint, incident promotion, learner or client-controlled event data."""
    from mojo.apps.account.models import BouncerSignal
    from mojo.apps import metrics
    try:
        metrics.record(f'bouncer:hosted:{action}', category='bouncer')
        BouncerSignal.objects.create(
            device=device, muid=identity(request, returning=True),
            page_type=record['purpose'], stage='assess', ip_address=request.ip,
            raw_signals={}, server_signals={**server_signals, 'hosted_gate': {'action': action}},
            risk_score=result.score, decision='log', triggered_signals=result.triggered_signals)
    except Exception:
        logger.warning('bouncer: hosted outcome audit unavailable')


def assess(request):
    from mojo.apps.account.rest.bouncer.assess import _set_pass_cookie, verify_pass_cookie
    from mojo.apps.account.services.bouncer.token_manager import TokenManager
    from mojo.apps.account.models import Group

    data = request.DATA.get('hosted_gate')
    if not isinstance(data, dict) or data.get('version') != 1:
        return _response('error', status=400, reason='invalid')
    operation = data.get('operation')
    capability = data.get('descriptor')
    request_id = data.get('request_id')
    if (operation not in ('check', 'submit', 'confirm', 'token')
            or not isinstance(capability, str) or not _CAPABILITY.fullmatch(capability)
            or not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id)):
        return _response('error', status=400, reason='invalid')
    muid = identity(request, returning=True)
    if not muid:
        return _response('recovery', status=409, reason='cookies')
    signals = request.DATA.get('signals', {})
    if not isinstance(signals, dict) or any(not isinstance(v, dict) for v in signals.values()):
        return _response('error', status=400, reason='invalid')
    redis = None
    try:
        redis = get_bounded_connection(timeout=1, read_from_replicas=False)
        store = ChallengeStore(redis, request.get_host().lower(), muid)
        record = store.read(capability)
        if record is None:
            return _response('recovery', status=409, reason='expired')
        purpose = record['purpose']
        if purpose not in PURPOSES:
            return _response('error', status=400, reason='invalid')
        if record['group_uuid']:
            group = Group.objects.filter(uuid=record['group_uuid'], is_active=True).first()
            if group is None or not group.is_effectively_active():
                return _response('recovery', status=403, reason='operator')
            request.group = group
        # Client group/purpose values cannot replace the render-issued scope.
        action, result, device, server_signals = screen(request, purpose, redis, signals)
        if operation in ('confirm', 'token'):
            if action in ('decoy', 'recovery') or record['stage'] in ('decoy', 'recovery'):
                return _response(action if action in ('decoy', 'recovery') else record['stage'], reason='operator')
            cookie = request.COOKIES.get('mbp', '')
            if verify_pass_cookie(cookie, request.ip) != muid:
                return _response('recovery', status=409, reason='cookies')
            if operation == 'confirm':
                if record['stage'] != 'granted' or cookie.split(':')[1] != str(record['issued']):
                    return _response('recovery', status=409, reason='restart')
                return _response('allow')
            if not record['form']:
                return _response('recovery', status=403, reason='restart')
            token = TokenManager.issue(duid='', fingerprint_id='', ip=request.ip,
                                       risk_score=result.score, page_type=purpose)
            return _response('token', token=token)
        outcome = store.complete(capability, operation, request_id,
                                 policy=action, answer=data.get('answer'))
        action = outcome.pop('next_action')
        issued = outcome.pop('issued', None)
        _audit(request, record, action, result, device, server_signals)
        response = _response(action, **outcome)
        if action == 'check_cookie' and issued is not None:
            _set_pass_cookie(response, muid, request.ip, issued_at=issued)
        return response
    except Exception:
        logger.warning('bouncer: hosted assessment unavailable')
        return _response('error', status=503, reason='unavailable')
    finally:
        if redis is not None:
            redis.close()
