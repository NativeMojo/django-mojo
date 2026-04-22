"""
Public message REST endpoints.

Two surfaces:
- Public submit endpoint (bouncer-gated, rate-limited) — writes PublicMessage.
- Admin RestMeta endpoint for list/detail/status updates — gated by support perms.
"""
from mojo import decorators as md
from mojo import errors as merrors
from mojo.helpers import logit
from mojo.helpers.response import JsonResponse
from mojo.apps import incident, metrics
from mojo.apps.account.models import PublicMessage
from mojo.apps.account.rest.bouncer.views import _resolve_group
from mojo.apps.account.services import public_message as svc


logger = logit.get_logger('bouncer', 'bouncer.log')


@md.POST('account/bouncer/message')
@md.public_endpoint("Bouncer public message intake (contact/support)")
@md.strict_rate_limit('public_message_submit', ip_limit=5, ip_window=300)
@md.requires_bouncer_token('public_message')
def on_submit_public_message(request):
    """
    Public submission endpoint for contact/support messages.

    Expects request.DATA: kind, name, email, message + kind-specific fields
    (company for contact_us; category+severity for support), plus bouncer_token.
    """
    raw_kind = request.DATA.get('kind') or svc.DEFAULT_KIND
    if svc.get_kind(raw_kind) is None:
        raise merrors.ValueException('kind:invalid')

    try:
        common, metadata = svc.validate_submission(raw_kind, request.DATA)
    except ValueError as err:
        raise merrors.ValueException(str(err))

    group = _resolve_group(request)

    msg = PublicMessage.objects.create(
        group=group,
        kind=raw_kind,
        name=common.get('name', ''),
        email=common.get('email', ''),
        subject=common.get('subject', ''),
        message=common.get('message', ''),
        metadata=metadata,
        status='open',
        ip_address=request.ip or None,
        user_agent=(request.META.get('HTTP_USER_AGENT') or '')[:512],
    )

    try:
        metrics.record(f"bouncer:public_messages:{raw_kind}", category="bouncer")
    except Exception:
        pass

    try:
        incident.report_event(
            f"Public message received: kind={raw_kind} email={common.get('email', '')} "
            f"group={getattr(group, 'id', None)}",
            category='security:bouncer:public_message',
            scope='account',
            level=3,
            request=request,
            kind=raw_kind,
            message_id=msg.id,
            group_id=getattr(group, 'id', None),
        )
    except Exception as err:
        logger.warning(f"public_message: incident.report_event failed: {err}")

    try:
        svc.notify_admins(msg)
    except Exception as err:
        logger.warning(f"public_message: notify_admins failed: {err}")

    return JsonResponse({'status': True, 'data': {'id': msg.id}})


@md.URL('account/public_message')
@md.URL('account/public_message/<int:pk>')
@md.uses_model_security(PublicMessage)
def on_public_message(request, pk=None):
    """Admin list/detail/update endpoint for PublicMessage."""
    return PublicMessage.on_rest_request(request, pk)
