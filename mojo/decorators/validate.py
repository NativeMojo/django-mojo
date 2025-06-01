from functools import wraps
import mojo.errors

def requires_params(*required_params):
    def decorator(func):
        @wraps(func)
        def wrapper(request, *args, **kwargs):
            missing_params = [param for param in required_params if param not in request.DATA]
            if missing_params:
                str_params = ', '.join(missing_params)
                raise mojo.errors.ValueException(f"missing required parameters: {str_params}")
            return func(request, *args, **kwargs)
        return wrapper
    return decorator
