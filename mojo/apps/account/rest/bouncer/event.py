import uuid

from mojo import decorators as md
from mojo.helpers import logit
from mojo.helpers.response import JsonResponse
from mojo.apps.account.rest.bouncer.assess import _geolocate, _report_bouncer_event
from mojo.apps.account.services.bouncer.stream_scoring import score_session

logger = logit.get_logger('bouncer', 'bouncer.log')


@md.POST('account/bouncer/event')
@md.public_endpoint("Bouncer client event reporting — public, rate-limited")
@md.rate_limit('bouncer_event', ip_limit=60)
def on_bouncer_event(request):
    """
    Client-side event reporting from mojo-bouncer.js (single-event legacy
    format) and mojo-sentinel.js (batched `events: [...]` format).

    Legacy: `{event_type, data}` — one BouncerSignal row, risk_action returned.
    Batched: `{events: [{event_type, data, context}, ...]}` — N rows persisted
             via bulk_create, then score_session(muid) called inline.

    Detection is presence of the `events` key. Score_session failures must not
    break the endpoint — wrapped in try/except. Endpoint returns 200 either way
    so the client cannot infer scoring state.
    """
    from mojo.apps.account.models.bouncer_device import BouncerDevice
    from mojo.apps.account.models.bouncer_signal import BouncerSignal

    muid = request.muid or ''
    duid = request.DATA.get('duid') or request.duid or ''
    msid = request.msid or ''
    mtab = request.mtab or ''
    page_type = request.DATA.get('page_type', 'login')
    session_id = request.DATA.get('session_id') or uuid.uuid4().hex
    events = request.DATA.get('events')

    geo_ip = _geolocate(request.ip)

    device = None
    risk_tier = 'unknown'
    if muid:
        device = BouncerDevice.objects.filter(muid=muid).first()
        if device:
            risk_tier = device.risk_tier

    # ── Batched path (sentinel) ──────────────────────────────────
    if isinstance(events, list) and events:
        # Hard cap on batch size — sentinel's default flush_size is 25 and a
        # cooperative client tops out around 200. Reject larger payloads
        # silently (truncate, persist what fits) so a malicious client can't
        # force an unbounded bulk_create or scoring window.
        events = events[:200]
        rows = []
        for evt in events:
            if not isinstance(evt, dict):
                continue
            evt_type = evt.get('event_type') or evt.get('category') or 'client_event'
            evt_data = evt.get('data') if isinstance(evt.get('data'), dict) else {}
            context = evt.get('context', '')
            rows.append(BouncerSignal(
                device=device,
                muid=muid,
                duid=duid,
                msid=msid,
                mtab=mtab,
                session_id=session_id,
                stage='event',
                ip_address=request.ip,
                page_type=page_type,
                raw_signals={
                    'event_type': evt_type,
                    'data': evt_data,
                    'context': context,
                },
                server_signals={},
                risk_score=0,
                decision='log',
                triggered_signals=[evt_type],
                geo_ip=geo_ip,
            ))
        if rows:
            try:
                BouncerSignal.objects.bulk_create(rows)
            except Exception:
                logger.exception('bouncer: failed to bulk_create event signals')

        # Inline scoring — failures swallowed so the writer path stays clean.
        try:
            score_session(muid)
        except Exception:
            logger.exception('bouncer: score_session failed for muid=%s', muid)

        return JsonResponse({'status': True, 'data': {'count': len(rows)}})

    # ── Legacy single-event path (mojo-bouncer.js v1 + back-compat) ──
    event_type = request.DATA.get('event_type', 'client_error')
    event_data = request.DATA.get('data') or {}

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
