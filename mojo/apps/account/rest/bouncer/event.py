import uuid

from mojo import decorators as md
from mojo.helpers import logit
from mojo.helpers.response import JsonResponse
from mojo.apps.account.rest.bouncer.assess import _geolocate, _report_bouncer_event

logger = logit.get_logger('bouncer', 'bouncer.log')


@md.POST('account/bouncer/event')
@md.public_endpoint("Bouncer client event reporting — public, rate-limited")
@md.rate_limit('bouncer_event', ip_limit=60)
def on_bouncer_event(request):
    """
    Client-side event reporting (uncaught exceptions, custom signals).
    Logs to BouncerSignal and fires incident for high-risk events.
    """
    from mojo.apps.account.models.bouncer_device import BouncerDevice
    from mojo.apps.account.models.bouncer_signal import BouncerSignal

    muid = request.muid or ''
    duid = request.DATA.get('duid') or request.duid or ''
    msid = request.msid or ''
    mtab = request.mtab or ''
    page_type = request.DATA.get('page_type', 'login')
    event_type = request.DATA.get('event_type', 'client_error')
    session_id = request.DATA.get('session_id') or uuid.uuid4().hex
    event_data = request.DATA.get('data') or {}

    device = None
    risk_tier = 'unknown'
    if muid:
        device = BouncerDevice.objects.filter(muid=muid).first()
        if device:
            risk_tier = device.risk_tier

    geo_ip = _geolocate(request.ip)

    risk_action = _risk_action(risk_tier, event_type)

    try:
        BouncerSignal.objects.create(
            device=device,
            muid=muid,
            duid=duid,
            msid=msid,
            mtab=mtab,
            session_id=session_id,
            stage='event',
            ip_address=request.ip,
            page_type=page_type,
            raw_signals={'event_type': event_type, 'data': event_data},
            server_signals={},
            risk_score=0,
            decision='log',
            triggered_signals=[event_type],
            geo_ip=geo_ip,
        )
    except Exception:
        logger.exception('bouncer: failed to log event signal')

    if risk_action in ('flag', 'monitor'):
        level = 7 if risk_action == 'flag' else 5
        _report_bouncer_event(
            'security:bouncer:event',
            f"Bouncer client event: muid={muid} event={event_type} tier={risk_tier}",
            level=level, request=request,
            muid=muid, duid=duid, event_type=event_type,
            risk_tier=risk_tier, risk_action=risk_action,
        )

    return JsonResponse({'status': True, 'data': {'risk_action': risk_action}})


def _risk_action(risk_tier, event_type):
    if risk_tier == 'blocked':
        return 'flag'
    if risk_tier == 'high':
        return 'monitor'
    return 'log'
