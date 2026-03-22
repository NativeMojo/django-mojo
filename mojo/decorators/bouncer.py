"""
Bouncer decorators for django-mojo endpoints and views.

@md.requires_bouncer_token(page_type)
    Guards an API endpoint. Validates the bouncer token from request.DATA.bouncer_token.
    Behaviour is controlled by BOUNCER_REQUIRE_TOKEN (default False = log-only mode).
    When False: logs missing/invalid tokens but allows the request through.
    When True (or group-level opt-in): rejects with 403.

Usage on the login endpoint:
    @md.requires_bouncer_token('login')
    def on_user_login(request):
        ...
"""
from functools import wraps
import mojo.errors
from mojo.helpers import logit
from mojo.helpers.settings import settings

logger = logit.get_logger('bouncer', 'bouncer.log')

__all__ = ['requires_bouncer_token']


def requires_bouncer_token(page_type='login'):
    """
    Validate the bouncer token attached to an API request.

    Reads bouncer_token from request.DATA. Validates signature, expiry, IP binding,
    duid binding, and single-use nonce.

    BOUNCER_REQUIRE_TOKEN=False (default): invalid/missing token is logged but
    the request proceeds. Safe for gradual rollout.
    BOUNCER_REQUIRE_TOKEN=True: invalid/missing token returns 403.
    """
    def decorator(func):
        @wraps(func)
        def wrapper(request, *args, **kwargs):
            token_str = request.DATA.get('bouncer_token', '')
            require = settings.get_static('BOUNCER_REQUIRE_TOKEN', False)

            # Check group-level opt-in override
            if not require and request.group:
                require = bool(
                    getattr(request.group, 'metadata', {}).get('require_bouncer_token', False)
                )

            if not token_str:
                if require:
                    _report_token_event(
                        request, page_type, 'security:bouncer:token_missing',
                        'Bouncer token missing', level=6,
                    )
                    raise mojo.errors.PermissionDeniedException(
                        'Bouncer token required', 403, 403
                    )
                logger.info(
                    f"bouncer: missing token page_type={page_type} ip={request.ip} "
                    f"muid={request.muid} (log-only mode)"
                )
                return func(request, *args, **kwargs)

            try:
                from mojo.apps.account.services.bouncer.token_manager import TokenManager
                payload = TokenManager.validate_and_consume(
                    token_str,
                    request_ip=request.ip,
                    request_duid=request.duid or '',
                )
                # Scope check
                if payload.get('page_type') != page_type:
                    raise ValueError('page_type_mismatch')
                request.bouncer_payload = payload
            except ValueError as exc:
                error = str(exc)
                _report_token_event(
                    request, page_type, 'security:bouncer:token_invalid',
                    f"Bouncer token invalid ({error})", level=7, error=error,
                )
                if require:
                    raise mojo.errors.PermissionDeniedException(
                        'Invalid bouncer token', 403, 403
                    )
                logger.warning(
                    f"bouncer: invalid token error={error} page_type={page_type} "
                    f"ip={request.ip} (log-only mode)"
                )

            return func(request, *args, **kwargs)
        return wrapper
    return decorator


def _report_token_event(request, page_type, category, details, level, **kwargs):
    from mojo.apps import incident
    incident.report_event(
        f"{details} on {page_type} endpoint ip={request.ip} muid={request.muid}",
        category=category,
        scope='account',
        level=level,
        request=request,
        muid=getattr(request, 'muid', ''),
        duid=getattr(request, 'duid', ''),
        page_type=page_type,
        **kwargs,
    )
