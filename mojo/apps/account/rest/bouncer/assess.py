import uuid

from django.http import HttpResponse

from mojo import decorators as md
from mojo.helpers import logit
from mojo.helpers.crypto import sign as crypto_sign, verify as crypto_verify
from mojo.helpers.response import JsonResponse
from mojo.helpers.settings import settings
from mojo.apps import incident, jobs, metrics

from mojo.apps.account.services.bouncer.scoring import RiskScorer, ScoringContext
from mojo.apps.account.services.bouncer.environment import EnvironmentService
from mojo.apps.account.services.bouncer.token_manager import TokenManager

logger = logit.get_logger('bouncer', 'bouncer.log')

_bouncer_defaults_checked = False

def _ensure_bouncer_defaults():
    """Bootstrap default RuleSets on first request. Idempotent — ensure_bouncer_rules
    uses get_or_create. The guard checks the most recently added category so that
    existing deployments backfill new rule categories on the next assess hit."""
    global _bouncer_defaults_checked
    if not _bouncer_defaults_checked:
        try:
            from mojo.apps.incident.models import RuleSet
            # Use security:bouncer:session_freeze as the canary — it's the newest
            # category, so missing means rules need to be (re-)bootstrapped.
            has_session_rules = RuleSet.objects.filter(
                category="security:bouncer:session_freeze"
            ).exists()
            if not has_session_rules:
                RuleSet.ensure_bouncer_rules()
        except Exception:
            pass
        _bouncer_defaults_checked = True


def _geolocate(ip):
    """Shared GeoIP enrichment — fail-open, returns None on error."""
    try:
        from mojo.apps.account.models.geolocated_ip import GeoLocatedIP
        return GeoLocatedIP.geolocate(ip)
    except Exception:
        return None


def _report_bouncer_event(category, details, level, request, **kwargs):
    """Report a structured bouncer incident with full metadata for rule matching."""
    incident.report_event(
        details,
        category=category,
        scope='account',
        level=level,
        request=request,
        **kwargs,
    )


@md.POST('account/bouncer/assess')
@md.public_endpoint("Bouncer signal assessment — public, rate-limited, no user auth required")
@md.rate_limit('bouncer_assess', ip_limit=60)
def on_bouncer_assess(request):
    """
    Stage 1: receive client-side signals, score them, issue token if human.
    Called by mojo-bouncer.js after the challenge window completes.

    On allow/monitor: returns a signed bouncer token + sets HttpOnly pass cookie.
    On block: returns decision only, no token. Fires incident + learning job.
    """
    from mojo.apps.account.models.bouncer_device import BouncerDevice
    from mojo.apps.account.models.bouncer_signal import BouncerSignal

    _ensure_bouncer_defaults()

    muid = request.muid or ''
    duid = request.DATA.get('duid') or request.duid or ''
    msid = request.msid or ''
    mtab = request.mtab or ''
    fingerprint_id = request.DATA.get('fingerprint_id', '')
    page_type = request.DATA.get('page_type', 'login')
    session_id = request.DATA.get('session_id') or uuid.uuid4().hex
    client_signals = request.DATA.get('signals') or {}

    # Geo enrichment (fail-open)
    geo_ip = _geolocate(request.ip)

    # Server-side signal analysis
    server_signals = EnvironmentService.analyze_request(request, geo_ip)

    # Get or create device record (keyed on muid — server-controlled)
    device = None
    if muid:
        device, _ = BouncerDevice.get_or_create_for_muid(muid, duid=duid, ip=request.ip)
        updates = {'event_count': device.event_count + 1}
        if msid:
            updates['msid'] = msid
        if fingerprint_id and device.fingerprint_id != fingerprint_id:
            updates['fingerprint_id'] = fingerprint_id
        for k, v in updates.items():
            setattr(device, k, v)
        device.save(update_fields=list(updates.keys()) + ['last_seen'])

        # Stitch other devices sharing this fingerprint
        if fingerprint_id:
            _stitch_fingerprint(device, fingerprint_id)

    # Score
    context = ScoringContext(
        client_signals=client_signals,
        server_signals=server_signals,
        device_session=device,
        page_type=page_type,
        request=request,
    )
    result = RiskScorer.score(context)

    # Record metrics
    try:
        metrics.record("bouncer:assessments", category="bouncer")
    except Exception:
        pass

    # Issue token + set pass cookie on allow/monitor
    token = None
    response_data = {
        'decision': result.decision,
        'risk_score': result.score,
        'session_id': session_id,
    }

    if result.decision in ('allow', 'monitor'):
        token = TokenManager.issue(
            duid=duid,
            fingerprint_id=fingerprint_id,
            ip=request.ip,
            risk_score=result.score,
            page_type=page_type,
        )
        response_data['token'] = token

    # Update device tier on block
    if result.decision == 'block' and device:
        new_tier = 'blocked' if result.score >= 80 else 'high'
        if device.risk_tier != new_tier:
            device.risk_tier = new_tier
            device.block_count += 1
            device.save(update_fields=['risk_tier', 'block_count'])

    # Log signal event (fail-open)
    try:
        BouncerSignal.objects.create(
            device=device,
            muid=muid,
            duid=duid,
            msid=msid,
            mtab=mtab,
            session_id=session_id,
            stage='assess',
            ip_address=request.ip,
            page_type=page_type,
            raw_signals=_safe_signals(client_signals),
            server_signals=server_signals,
            risk_score=result.score,
            decision=result.decision,
            triggered_signals=result.triggered_signals,
            geo_ip=geo_ip,
        )
    except Exception:
        logger.exception('bouncer: failed to log BouncerSignal')

    # Incident + learning for blocks
    if result.decision == 'block':
        try:
            metrics.record("bouncer:blocks", category="bouncer")
            country_code = getattr(geo_ip, 'country_code', None) if geo_ip else None
            if country_code:
                metrics.record(f"bouncer:blocks:country:{country_code}", category="bouncer")
        except Exception:
            pass
        _report_bouncer_event(
            'security:bouncer:block',
            f"Bouncer block: muid={muid} duid={duid} ip={request.ip} score={result.score}",
            level=8, request=request,
            muid=muid, duid=duid, risk_score=result.score,
            triggered_signals=result.triggered_signals, page_type=page_type,
            decision='block',
        )
        min_score = settings.get_static('BOUNCER_LEARN_MIN_SCORE', 80)
        if result.score >= min_score:
            jobs.publish(
                'mojo.apps.account.services.bouncer.learner.learn_from_block',
                {
                    'muid': muid,
                    'duid': duid,
                    'ip': request.ip,
                    'fingerprint_id': fingerprint_id,
                    'risk_score': result.score,
                    'triggered_signals': result.triggered_signals,
                    'user_agent': request.user_agent,
                },
            )
    elif result.decision == 'monitor':
        try:
            metrics.record("bouncer:monitors", category="bouncer")
        except Exception:
            pass
        _report_bouncer_event(
            'security:bouncer:monitor',
            f"Bouncer monitor: muid={muid} duid={duid} ip={request.ip} score={result.score}",
            level=5, request=request,
            muid=muid, duid=duid, risk_score=result.score,
            triggered_signals=result.triggered_signals, page_type=page_type,
            decision='monitor',
        )

    resp = JsonResponse({'status': True, 'data': response_data})

    # Set HttpOnly pass cookie when human (allows skipping challenge next visit)
    if result.decision in ('allow', 'monitor') and muid:
        _set_pass_cookie(resp, muid, request.ip)

    return resp


def _stitch_fingerprint(device, fingerprint_id):
    from mojo.apps.account.models.bouncer_device import BouncerDevice
    others = BouncerDevice.objects.filter(
        fingerprint_id=fingerprint_id
    ).exclude(pk=device.pk)[:10]
    for other in others:
        device.link_muid(other.muid)
        other.link_muid(device.muid)


def _safe_signals(signals):
    """Strip oversized values from signal dict before storing."""
    if not isinstance(signals, dict):
        return {}
    result = {}
    for k, v in signals.items():
        if isinstance(v, (str, int, float, bool, type(None))):
            result[k] = v
        elif isinstance(v, dict):
            result[k] = {sk: sv for sk, sv in list(v.items())[:20]
                         if isinstance(sv, (str, int, float, bool, type(None)))}
    return result


def _set_pass_cookie(response, muid, ip):
    """Set a signed HttpOnly pass cookie so the bouncer gate is skipped next visit."""
    import time
    issued = str(int(time.time()))
    ip_prefix = '.'.join(ip.split('.')[:3]) if ip else ''
    data = f"{muid}:{ip_prefix}:{issued}"
    sig = crypto_sign(data)[:16]
    value = f"{muid}:{issued}:{sig}"

    ttl = settings.get_static('BOUNCER_PASS_COOKIE_TTL', 86400)
    # BOUNCER_PASS_COOKIE_DOMAIN lets the mbp cookie be shared across subdomains
    # under a common parent (e.g. ".example.com"), enabling nginx auth_request
    # gating from app subdomains against the bouncer host. Unset → host-only.
    cookie_domain = settings.get_static('BOUNCER_PASS_COOKIE_DOMAIN', '') or None
    response.set_cookie(
        'mbp', value,
        max_age=ttl,
        httponly=True,
        secure=not settings.DEBUG,
        samesite='Lax',
        domain=cookie_domain,
    )


@md.GET('account/bouncer/verify_pass')
@md.public_endpoint("nginx auth_request — validates mbp pass cookie + signature cache")
@md.rate_limit('bouncer_verify_pass', ip_limit=600)
def on_verify_pass(request):
    """
    Lightweight endpoint for nginx `auth_request` directives. Returns 200 if
    the request carries a valid `mbp` pass cookie and is not matched by the
    Redis signature cache. 401 otherwise — nginx then redirects to the bouncer
    challenge.

    Body is intentionally empty (nginx auth_request discards it); diagnostic
    info lives in response headers (`X-Bouncer-Muid`, `X-Bouncer-Reason`).
    """
    from mojo.apps.account.services.bouncer.learner import check_signature_cache

    # 1. Signature cache pre-screen — known-bot IPs/UAs blocked at the edge
    #    without ever checking the cookie. The challenge page does the same
    #    check; doing it here means nginx-gated locations also benefit.
    matched, sig_type, sig_value = check_signature_cache(
        request.ip, request.user_agent, ''
    )
    if matched:
        resp = HttpResponse(status=401)
        resp['X-Bouncer-Reason'] = 'signature'
        return resp

    # 2. Pass cookie validation
    cookie_value = request.COOKIES.get('mbp', '')
    if cookie_value:
        muid = verify_pass_cookie(cookie_value, request.ip)
        if muid:
            resp = HttpResponse(status=200)
            resp['X-Bouncer-Muid'] = muid
            return resp

    resp = HttpResponse(status=401)
    resp['X-Bouncer-Reason'] = 'no_cookie' if not cookie_value else 'invalid_cookie'
    return resp


def verify_pass_cookie(cookie_value, ip):
    """Validate a pass cookie. Returns muid string on success, None on failure."""
    import time
    try:
        parts = cookie_value.split(':')
        if len(parts) != 3:
            return None
        muid, issued_str, provided_sig = parts
        issued = int(issued_str)
        ttl = settings.get_static('BOUNCER_PASS_COOKIE_TTL', 86400)
        if int(time.time()) - issued > ttl:
            return None
        ip_prefix = '.'.join(ip.split('.')[:3]) if ip else ''
        data = f"{muid}:{ip_prefix}:{issued_str}"
        expected_sig = crypto_sign(data)[:16]
        if provided_sig != expected_sig:
            return None
        return muid
    except Exception:
        return None
