from functools import wraps
import mojo.errors

def requires_perms(*required_perms):
    def decorator(func):
        @wraps(func)
        def wrapper(request, *args, **kwargs):
            if not request.user.is_authenticated:
                raise mojo.errors.PermissionDeniedException()
            if not request.user.has_permission(required_perms):
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
