from functools import wraps
import mojo.errors
from mojo.helpers import logit
from mojo.helpers import modules

logger = logit.get_logger("error", "error.log")

# Global security registry - stores security metadata for all decorated functions
SECURITY_REGISTRY = {}

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
            if not request.user.has_permission(perms):
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
            if "group" in request.DATA:
                request.group = modules.get_model_instance("account", "Group", int(request.DATA.group))
            if not request.group.user_has_permission(request.user, perms, True):
                logger.error(f"{request.user.username} is missing {perms}")
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
