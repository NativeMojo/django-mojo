"""Reachable review intake and globally permissioned operator queue."""
from django.shortcuts import render
from mojo import decorators as md
from mojo.helpers.response import JsonResponse
from mojo.apps.account.services.bouncer import hosted_recovery as recovery


@md.POST('auth/bouncer/recovery')
@md.public_endpoint('Host-bound review intake; no Bouncer pass or cookies required')
@md.strict_rate_limit('bouncer_review', ip_limit=30, ip_window=300,
                      include_request_in_incident=False)
def on_review_request(request):
    status = 200
    try:
        data = recovery.submit(request)
        message = 'Your request is recorded for review. This does not grant access. Keep your reference for support.'
    except ValueError as error:
        status = 400
        data = {'reason': str(error)}
        message = ('Enter a valid email and a note of at most 500 characters.' if str(error) == 'contact'
                   else 'This request has expired or changed. Return to the original page and restart the check.')
    except Exception:
        status = 503
        data = {'reason': 'unavailable'}
        message = 'We could not record your request. Please try again later or use the site’s support channel.'
    if request.content_type == 'application/x-www-form-urlencoded':
        response = render(request, 'account/bouncer_review_result.html', {'message': message, **data}, status=status)
    else:
        response = JsonResponse({'status': status == 200, 'data': {**data, 'message': message}}, status=status)
    response['Cache-Control'] = 'no-store'
    response['Referrer-Policy'] = 'no-referrer'
    return response


@md.GET('auth/bouncer/recovery')
@md.requires_global_perms('view_security', 'manage_security', 'security')
def on_review_queue(request):
    try:
        before = request.DATA.get('before')
        if before is not None:
            before = int(before)
            if before < 1 or before > 9223372036854775807:
                raise ValueError('invalid')
        return {'status': True, 'data': recovery.queue(request.DATA.get('reference', ''), before)}
    except (ValueError, TypeError):
        return JsonResponse({'status': False, 'error': 'Invalid reference or cursor'}, status=400)


@md.POST('auth/bouncer/recovery/resolve')
@md.requires_global_perms('manage_security', 'security')
def on_review_resolve(request):
    try:
        return {'status': True, 'data': recovery.resolve(
            request.DATA.get('reference', ''), request.user, request.DATA.get('resolution', ''))}
    except ValueError:
        return JsonResponse({'status': False, 'error': 'Invalid review or resolution'}, status=400)
