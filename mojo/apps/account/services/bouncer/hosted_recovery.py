"""Hosted diagnostics and operator review. A review ticket never grants access."""
import hmac
import hashlib
import ipaddress
import re
import secrets
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction

from mojo.helpers import dates, logit
from mojo.helpers.crypto import sign, secret_keys
from mojo.helpers.redis import get_bounded_connection
from mojo.helpers.settings import settings

VERSION = 'mobile-1'
TICKET_TTL = 1800
REFERENCE = re.compile(r'([1-9][0-9]{0,18})-([a-f0-9]{24})\Z')
logger = logit.get_logger('bouncer', 'bouncer.log')
_BUDGET = """
for i = 1, 2 do
    if tonumber(redis.call('GET', KEYS[i]) or '0') >= tonumber(ARGV[i]) then
        return 0
    end
end
for i = 1, 2 do
    if redis.call('INCR', KEYS[i]) == 1 then
        redis.call('EXPIRE', KEYS[i], 300)
    end
end
return 1
"""


def _budget_keys(ip):
    digest = hashlib.sha256((ip or 'unknown').encode()).hexdigest()
    # Both keys must share a slot for atomic admission on Redis Cluster.
    return 'bouncer:{hosted-diagnostics}:global', 'bouncer:{hosted-diagnostics}:ip:' + digest


def _admit(ip):
    """Bound writes even when callers change cookies/hosts; failure skips storage."""
    def limit(name, default):
        value = settings.get_static(name, default)
        return value if type(value) is int and value > 0 else default

    with get_bounded_connection(timeout=1, read_from_replicas=False) as redis:
        # Reject before charging either budget or creating any new IP key.
        return bool(redis.eval(_BUDGET, 2, *_budget_keys(ip),
                               limit('BOUNCER_DIAGNOSTIC_GLOBAL_LIMIT', 3000),
                               limit('BOUNCER_DIAGNOSTIC_IP_LIMIT', 300)))


def _identity(request, name):
    value = getattr(request, name, '')
    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', value) else ''


def _ticket_data(host, reference):
    return f'bouncer-review:v1:{host.lower()}:{reference}'


def record(request, purpose, action, reason, *, result=None, device=None, group=None, signals=None):
    """Record server outcome and allowlisted counts; never persist the POST body."""
    from mojo.apps.account.models import BouncerSignal
    nonce = secrets.token_hex(12)
    try:
        try:
            ip = str(ipaddress.ip_address(getattr(request, 'ip', None)))
        except ValueError:
            ip = None
        if not _admit(ip):
            return {'reference': nonce}
        host = request.get_host().lower()
        behavior = (signals or {}).get('behavior', {})
        counts = {key: min(value, 10000) for key, value in behavior.items()
                  if key in ('mouse_move_count', 'touch_event_count', 'keystroke_count', 'activation_count')
                  and type(value) is int and value >= 0}
        details = {'version': VERSION, 'reference': nonce, 'host': host,
                   'group_uuid': getattr(group, 'uuid', '') or '', 'action': action, 'reason': reason,
                   'input_counts': counts, 'user_agent': str(getattr(request, 'user_agent', ''))[:512]}
        if result:
            details.update({key: result.metadata[key] for key in
                            ('uncapped_score', 'recovery_credit', 'historical_block', 'restriction')
                            if key in result.metadata})
        row = BouncerSignal.objects.create(
            device=device, muid=_identity(request, 'muid'), msid=_identity(request, 'msid'),
            page_type=purpose, stage='assess', ip_address=ip, decision='log',
            risk_score=result.score if result else 0,
            triggered_signals=result.triggered_signals[:50] if result else [],
            raw_signals={}, server_signals={'hosted_gate': details})
        reference = f'{row.pk}-{nonce}'
        return {'reference': reference, 'review_ticket': reference + '.' + sign(_ticket_data(host, reference))}
    except Exception:
        logger.warning(f'bouncer: diagnostic unavailable reference={nonce} reason={reason}')
        return {'reference': nonce}


def _row(reference, *, lock=False):
    from mojo.apps.account.models import BouncerSignal
    match = REFERENCE.fullmatch(reference) if isinstance(reference, str) else None
    if not match:
        raise ValueError('invalid')
    query = BouncerSignal.objects.select_for_update() if lock else BouncerSignal.objects
    row = query.filter(pk=int(match[1])).first()
    detail = row.server_signals.get('hosted_gate', {}) if row else {}
    if not row or not hmac.compare_digest(detail.get('reference', ''), match[2]):
        raise ValueError('invalid')
    return row, detail


def submit(request):
    ticket = request.DATA.get('review_ticket', '')
    if not isinstance(ticket, str) or len(ticket) > 128 or '.' not in ticket:
        raise ValueError('invalid')
    reference, signature = ticket.rsplit('.', 1)
    host = request.get_host().lower()
    if not any(hmac.compare_digest(sign(_ticket_data(host, reference), key), signature)
               for key in secret_keys()):
        raise ValueError('invalid')
    email, note = request.DATA.get('email', ''), request.DATA.get('note', '')
    if not isinstance(email, str) or len(email) > 254 or not isinstance(note, str) or len(note) > 500:
        raise ValueError('contact')
    try:
        validate_email(email)
    except ValidationError:
        raise ValueError('contact')
    with transaction.atomic():
        row, detail = _row(reference, lock=True)
        if detail['host'] != host or row.created < dates.utcnow() - timedelta(seconds=TICKET_TTL):
            raise ValueError('expired')
        if not detail.get('review'):
            detail['review'] = {'state': 'pending', 'email': email, 'note': note,
                                'requested_at': dates.utcnow().isoformat()}
            failure = request.DATA.get('reported_failure')
            if failure in ('cookies', 'expired', 'restart', 'operator', 'unavailable', 'invalid', 'limited', 'network'):
                detail['review']['client_reported_failure'] = failure
            row.server_signals = {**row.server_signals, 'hosted_gate': detail}
            row.save(update_fields=['server_signals'])
        return {'reference': reference, 'review_state': detail['review']['state']}


def queue(reference='', before=None):
    """Scan a bounded primary-key window, with a cursor even on empty pages."""
    from mojo.apps.account.models import BouncerSignal
    if reference:
        row, detail = _row(reference)
        return {'items': [_entry(row, detail)], 'before': None}
    rows = BouncerSignal.objects.order_by('-pk')
    if before is not None:
        rows = rows.filter(pk__lt=before)
    # Filter after the indexed range scan, not an unindexed JSON predicate.
    rows = list(rows.only('id', 'muid', 'msid', 'page_type', 'ip_address', 'risk_score',
                          'triggered_signals', 'server_signals', 'created')[:2000])
    items = []
    for row in rows:
        detail = row.server_signals.get('hosted_gate', {})
        if detail.get('review', {}).get('state') == 'pending':
            items.append(_entry(row, detail))
            if len(items) == 50:
                return {'items': items, 'before': row.pk}
    return {'items': items, 'before': rows[-1].pk if len(rows) == 2000 else None}


def _entry(row, detail):
    return {'reference': f'{row.pk}-{detail["reference"]}', 'signal_id': row.pk,
            'muid': row.muid, 'msid': row.msid, 'page_type': row.page_type,
            'ip': row.ip_address, 'risk_score': row.risk_score,
            'triggered_signals': row.triggered_signals, 'details': detail, 'created': row.created}


def resolve(reference, reviewer, note):
    """Record the operator's completed review. Restriction changes are explicit, separate actions."""
    if not isinstance(note, str) or not note.strip() or len(note) > 1000:
        raise ValueError('resolution')
    with transaction.atomic():
        row, detail = _row(reference, lock=True)
        review = detail.get('review')
        if not review:
            raise ValueError('invalid')
        if review['state'] == 'pending':
            review.update(state='reviewed', reviewed_by=reviewer.pk, resolution=note,
                          reviewed_at=dates.utcnow().isoformat())
            row.server_signals = {**row.server_signals, 'hosted_gate': detail}
            row.save(update_fields=['server_signals'])
        return {'reference': reference, 'review_state': review['state']}
