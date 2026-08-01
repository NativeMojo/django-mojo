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

# Token validation failures a legitimate user produces without doing anything
# wrong: the 15-minute TTL running out while they read the page, a
# double-submitted form (the nonce is single-use), or a cellular/CGNAT handoff
# that changed their egress IP mid-session. Reported for visibility, never at a
# level a blocking ruleset acts on.
BENIGN_TOKEN_ERRORS = frozenset({'expired', 'nonce_consumed', 'ip_mismatch'})

# Below every default block rule — recorded, never enforced on.
TOKEN_INVALID_BENIGN_LEVEL = 4
# The token was forged, malformed, or replayed outside its scope.
TOKEN_INVALID_SUSPICIOUS_LEVEL = 7


def requires_bouncer_token(page_type='login'):
    """
    Validate the bouncer token attached to an API request.

    Reads bouncer_token from request.DATA. Validates signature, expiry, IP binding,
    duid binding, and single-use nonce.

    BOUNCER_REQUIRE_TOKEN=False (default): invalid/missing token is logged but
    the request proceeds, and every token_invalid event is capped at level 4 so
    no blocking ruleset can act on a deployment that has not enabled
    enforcement. Safe for gradual rollout.
    BOUNCER_REQUIRE_TOKEN=True: invalid/missing token returns 403. Tampering
    (bad signature, malformed, wrong page_type, wrong duid) reports at level 7;
    benign lifecycle failures stay at 4 — see _token_invalid_level.

    Test-mode override: when the test-mode gate passes (see
    mojo.helpers.test_mode: env var + loopback + no proxy chain), the
    X-Mojo-Test-Bouncer-Require-Token header ("0"/"1") overrides the setting
    per-request. Production traffic never satisfies the gate.
    """
    def decorator(func):
        @wraps(func)
        def wrapper(request, *args, **kwargs):
            from mojo.helpers import test_mode as _tm
            token_str = request.DATA.get('bouncer_token', '')
            require = settings.get_static('BOUNCER_REQUIRE_TOKEN', False)
            # Test-mode header override (gated)
            if _tm.is_test_request(request):
                hdr = request.META.get('HTTP_X_MOJO_TEST_BOUNCER_REQUIRE_TOKEN')
                if hdr is not None:
                    require = hdr not in ('0', 'false', 'False', '')

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
                    f"Bouncer token invalid ({error})",
                    level=_token_invalid_level(error, require), error=error,
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


def _token_invalid_level(error, require):
    """
    Severity to report a security:bouncer:token_invalid event at.

    ``error`` is the ValueError string raised by TokenManager (invalid_format,
    invalid_signature, expired, ip_mismatch, duid_mismatch, nonce_consumed) or
    page_type_mismatch from the scope check above.

    ``require`` False means this deployment is still in log-only mode and has
    not enabled enforcement — so nothing observed here may be strong enough to
    get an address firewalled, whatever the cause.
    """
    if not require:
        return TOKEN_INVALID_BENIGN_LEVEL
    if error in BENIGN_TOKEN_ERRORS:
        return TOKEN_INVALID_BENIGN_LEVEL
    return TOKEN_INVALID_SUSPICIOUS_LEVEL


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
