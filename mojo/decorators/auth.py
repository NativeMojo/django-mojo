from functools import wraps
import mojo.errors
from mojo.helpers import logit
from mojo.helpers.settings import settings

logger = logit.get_logger("error", "error.log")

# Global security registry - stores security metadata for all decorated functions
SECURITY_REGISTRY = {}
REQUIRES_PERMS_IS_GROUP = settings.get_static('REQUIRES_PERMS_IS_GROUP', True)


def requires_perms(*required_perms):
    def decorator(func):
        # Add metadata for security detection
        func._mojo_requires_perms = True
        func._mojo_required_permissions = list(required_perms)
        func._mojo_security_type = "permissions"

        # Register in global security registry
        key = f"{func.__module__}.{func.__name__}"
        SECURITY_REGISTRY[key] = {
            'type': 'permissions',
            'permissions': list(required_perms),
            'function': func,
            'requires_auth': True
        }

        @wraps(func)
        def wrapper(request, *args, **kwargs):
            if not request.user.is_authenticated:
                raise mojo.errors.PermissionDeniedException()
            perms = set(required_perms)

            # A non-User identity (a group-scoped ApiKey) is trusted only within
            # an ACTIVE group context — validate_token strips request.group when
            # the key's group is deactivated, so this fails closed the moment a
            # tenant is suspended (ITEM-037). Without it, the has_permission
            # short-circuit below would trust the key's self-claimed perms with
            # no group consideration at all. Platform-global machine access uses
            # requires_global_perms (which ignores request.group) instead.
            if not hasattr(request.user, "is_request_user"):
                group = getattr(request, "group", None)
                if group is None or not group.is_active:
                    logger.error(f"{getattr(request.user, 'username', request.user)} has no active group context for {perms}")
                    raise mojo.errors.PermissionDeniedException()

            # First check user-based permissions
            if request.user.has_permission(perms):
                return func(request, *args, **kwargs)

            # If user doesn't have permissions, fallback to group-based checking
            if REQUIRES_PERMS_IS_GROUP:
                if "group" in request.DATA and not request.group:
                    from mojo.apps.account.models.group import Group
                    try:
                        # Active groups only — a member grant in a deactivated
                        # group must not authorize (mirrors the dispatcher).
                        request.group = Group.get_active(int(request.DATA.group))
                    except (TypeError, ValueError):
                        # Unusable group param -> no group context (fail closed)
                        request.group = None
                if not request.group or not request.group.user_has_permission(request.user, perms, True):
                    logger.error(f"{request.user.username} is missing {perms}")
                    raise mojo.errors.PermissionDeniedException()
            else:
                # No group checking allowed, user already failed permission check
                logger.error(f"{request.user.username} is missing {perms}")
                raise mojo.errors.PermissionDeniedException()

            return func(request, *args, **kwargs)
        return wrapper
    return decorator


def requires_group_perms(*required_perms):
    def decorator(func):
        # Add metadata for security detection
        func._mojo_requires_perms = True
        func._mojo_required_permissions = list(required_perms)
        func._mojo_security_type = "permissions"

        # Register in global security registry
        key = f"{func.__module__}.{func.__name__}"
        SECURITY_REGISTRY[key] = {
            'type': 'permissions',
            'permissions': list(required_perms),
            'function': func,
            'requires_auth': True
        }

        @wraps(func)
        def wrapper(request, *args, **kwargs):
            if not request.user.is_authenticated:
                raise mojo.errors.PermissionDeniedException()
            perms = set(required_perms)

            # Same ApiKey active-group-context gate as requires_perms (ITEM-037).
            if not hasattr(request.user, "is_request_user"):
                group = getattr(request, "group", None)
                if group is None or not group.is_active:
                    logger.error(f"{getattr(request.user, 'username', request.user)} has no active group context for {perms}")
                    raise mojo.errors.PermissionDeniedException()

            # First check user-based permissions
            if request.user.has_permission(perms):
                return func(request, *args, **kwargs)

            # If user doesn't have permissions, fallback to group-based checking
            if "group" in request.DATA and not request.group:
                from mojo.apps.account.models.group import Group
                try:
                    # Active groups only — a member grant in a deactivated
                    # group must not authorize (mirrors the dispatcher).
                    request.group = Group.get_active(int(request.DATA.group))
                except (TypeError, ValueError):
                    # Unusable group param -> no group context (fail closed)
                    request.group = None
            if not request.group or not request.group.user_has_permission(request.user, perms, True):
                logger.error(f"{request.user.username} is missing {perms}")
                raise mojo.errors.PermissionDeniedException()
            return func(request, *args, **kwargs)
        return wrapper
    return decorator


def requires_global_perms(*required_perms, allow_api_keys=False):
    """Like ``requires_perms`` but WITHOUT the group-permission fallback.

    ``requires_perms`` falls back to
    ``request.group.user_has_permission(request.user, perms)`` using a
    client-supplied ``group`` param. That is correct for endpoints whose effect
    is confined to that group, but a cross-tenant privilege escalation for
    endpoints whose effect is PLATFORM-WIDE (global settings, fleet operations,
    cross-tenant data): a GroupMember-scoped grant — which any group admin can
    assign, with arbitrary key names — would otherwise authorize a global
    action just by naming the caller's own group. Use this decorator for those
    endpoints; it authorizes on the caller's GLOBAL ``User.permissions`` (or
    superuser) only, never the member/group fallback.

    A non-User identity (a group-scoped ``ApiKey`` authenticating via
    ``Authorization: apikey <token>``) is rejected by default — a group
    credential must not satisfy a platform-global gate. ``is_request_user`` is
    the framework's canonical "real request User?" marker (``User`` defines it,
    ``ApiKey`` does not). Pass ``allow_api_keys=True`` ONLY for endpoints that
    are themselves a federation / machine-ingest surface (e.g. the geoip sync
    receiver), where an ApiKey holding the perm is the intended caller; the
    member/group fallback is still never consulted.
    """
    perm_set = set(required_perms)

    def decorator(func):
        func._mojo_requires_perms = True
        func._mojo_required_permissions = list(required_perms)
        func._mojo_security_type = "permissions"

        key = f"{func.__module__}.{func.__name__}"
        SECURITY_REGISTRY[key] = {
            'type': 'permissions',
            'permissions': list(required_perms),
            'function': func,
            'requires_auth': True,
            'global_only': True,
        }

        @wraps(func)
        def wrapper(request, *args, **kwargs):
            user = getattr(request, "user", None)
            if user is None or not getattr(user, "is_authenticated", False):
                raise mojo.errors.PermissionDeniedException()
            if not allow_api_keys and not hasattr(user, "is_request_user"):
                # A group-scoped ApiKey (or any non-User identity) must not
                # satisfy a platform-global gate.
                raise mojo.errors.PermissionDeniedException()
            if not user.has_permission(perm_set):
                logger.error(f"{getattr(user, 'username', user)} is missing global {perm_set}")
                raise mojo.errors.PermissionDeniedException()
            return func(request, *args, **kwargs)
        return wrapper
    return decorator


def public_endpoint(reason=""):
    """
    Decorator to explicitly mark an endpoint as intentionally public.
    This helps security auditing distinguish between endpoints that are
    intentionally public vs those missing security.

    Usage: @public_endpoint("GeoIP lookup for security monitoring")
    """
    def decorator(func):
        func._mojo_public_endpoint = True
        func._mojo_public_reason = reason
        func._mojo_security_type = "public"

        # Register in global security registry
        key = f"{func.__module__}.{func.__name__}"
        SECURITY_REGISTRY[key] = {
            'type': 'public',
            'reason': reason,
            'function': func,
            'requires_auth': False
        }

        return func
    return decorator


def custom_security(description=""):
    """
    Decorator to mark endpoints with custom security logic that doesn't
    fit standard patterns (like dynamic permission checking, token validation, etc.)

    Usage: @custom_security("Dynamic account-level permission checking")
    """
    def decorator(func):
        func._mojo_custom_security = True
        func._mojo_security_description = description
        func._mojo_security_type = "custom"

        # Register in global security registry
        key = f"{func.__module__}.{func.__name__}"
        SECURITY_REGISTRY[key] = {
            'type': 'custom',
            'description': description,
            'function': func,
            'requires_auth': True  # Custom security usually requires auth
        }

        return func
    return decorator


def uses_model_security(model_class=None):
    """
    Decorator to explicitly indicate that an endpoint relies on model-level
    security (RestMeta permissions) for its protection.

    Usage: @uses_model_security(User)
    """
    def decorator(func):
        func._mojo_uses_model_security = True
        func._mojo_secured_model = model_class
        func._mojo_secured_model_name = model_class.__name__ if model_class else None
        func._mojo_security_type = "model"

        # Register in global security registry
        key = f"{func.__module__}.{func.__name__}"
        SECURITY_REGISTRY[key] = {
            'type': 'model',
            'model_class': model_class,
            'model_name': model_class.__name__ if model_class else None,
            'function': func,
            'requires_auth': True
        }

        return func
    return decorator


def token_secured(token_types=None, description=""):
    """
    Decorator to mark endpoints secured by token-based authentication
    (like upload tokens, download tokens, etc.)

    Usage: @token_secured(['upload_token'], "Secured by upload token validation")
    """
    def decorator(func):
        func._mojo_token_secured = True
        func._mojo_token_types = token_types or []
        func._mojo_security_description = description
        func._mojo_security_type = "token"

        # Register in global security registry
        key = f"{func.__module__}.{func.__name__}"
        SECURITY_REGISTRY[key] = {
            'type': 'token',
            'token_types': token_types or [],
            'description': description,
            'function': func,
            'requires_auth': False  # Token auth doesn't require user session
        }

        return func
    return decorator


def requires_auth():
    def decorator(func):
        # Add metadata for security detection
        func._mojo_requires_auth = True
        func._mojo_security_type = "authentication"

        # Register in global security registry
        key = f"{func.__module__}.{func.__name__}"
        SECURITY_REGISTRY[key] = {
            'type': 'authentication',
            'function': func,
            'requires_auth': True
        }

        @wraps(func)
        def wrapper(request, *args, **kwargs):
            if not request.user.is_authenticated:
                raise mojo.errors.PermissionDeniedException()
            return func(request, *args, **kwargs)
        return wrapper
    return decorator


def requires_fresh_auth(seconds=None):
    """Require recent ("step-up") authentication for a sensitive endpoint.

    Authenticated callers whose JWT auth_time is older than the window get a
    440 reauth_required (see mojo.apps.account.services.fresh_auth). Inert when
    FRESH_AUTH_WINDOW <= 0 (the default), so this is safe to apply broadly.
    Pass `seconds` to override the global window for one endpoint.
    """
    def decorator(func):
        func._mojo_requires_fresh_auth = True
        func._mojo_fresh_auth_seconds = seconds
        func._mojo_security_type = "fresh_auth"

        key = f"{func.__module__}.{func.__name__}"
        SECURITY_REGISTRY[key] = {
            'type': 'fresh_auth',
            'seconds': seconds,
            'function': func,
            'requires_auth': True
        }

        @wraps(func)
        def wrapper(request, *args, **kwargs):
            if not request.user.is_authenticated:
                raise mojo.errors.PermissionDeniedException()
            from mojo.apps.account.services import fresh_auth
            fresh_auth.require_fresh(request, seconds)
            return func(request, *args, **kwargs)
        return wrapper
    return decorator


def requires_bearer(bearer):
    def decorator(func):
        # Add metadata for security detection
        func._mojo_requires_bearer = True
        func._mojo_bearer_token = bearer
        func._mojo_security_type = "bearer_token"

        # Register in global security registry
        key = f"{func.__module__}.{func.__name__}"
        SECURITY_REGISTRY[key] = {
            'type': 'bearer_token',
            'bearer_token': bearer,
            'function': func,
            'requires_auth': False  # Bearer token is alternative to user auth
        }

        @wraps(func)
        def wrapper(request, *args, **kwargs):
            if request.bearer is None or request.bearer.lower() != bearer.lower():
                raise mojo.errors.PermissionDeniedException(f"invalid bearer token '{request.bearer}'")
            return func(request, *args, **kwargs)
        return wrapper
    return decorator
