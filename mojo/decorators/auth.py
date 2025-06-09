from functools import wraps
import mojo.errors
from mojo.helpers import logit

logger = logit.get_logger("error", "error.log")

def requires_perms(*required_perms):
    def decorator(func):
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


def requires_auth():
    def decorator(func):
        @wraps(func)
        def wrapper(request, *args, **kwargs):
            if not request.user.is_authenticated:
                raise mojo.errors.PermissionDeniedException()
            return func(request, *args, **kwargs)
        return wrapper
    return decorator
