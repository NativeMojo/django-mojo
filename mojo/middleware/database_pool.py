"""Outermost HTTP boundary for bounded database-pool acquisition failures."""

from mojo.db.errors import emit_pool_error, is_pool_acquisition_error
from mojo.helpers import error_pages


class DatabasePoolErrorMiddleware:
    """Map pool queue failures from authentication or later work to HTTP 503."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            return self.get_response(request)
        except Exception as error:
            if not is_pool_acquisition_error(error):
                raise
            request._mojo_pool_acquisition_error = True
            emit_pool_error(error, path=getattr(request, "path", None))
            response = error_pages.error_response(
                request,
                {
                    "status": False,
                    "error": "Database temporarily unavailable",
                    "code": 503,
                },
                503,
            )
            response["Retry-After"] = "1"
            return response
