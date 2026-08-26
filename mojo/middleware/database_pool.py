"""Django exception hook for bounded database-pool acquisition failures."""

from django.db import close_old_connections
from django.utils.deprecation import MiddlewareMixin

from mojo.db.errors import http_pool_error_response, is_pool_acquisition_error


class DatabasePoolErrorMiddleware(MiddlewareMixin):
    """Own pool cleanup and bounded acquisition failures at the HTTP edge.

    Django's ASGI handler can finish a response outside the synchronous thread
    that evaluated middleware and opened its thread-local connection wrapper.
    Returning the lease here keeps cleanup on the exact request thread before
    that long-lived executor worker is reused.
    """

    def process_request(self, request):
        close_old_connections()

    def process_response(self, request, response):
        close_old_connections()
        return response

    def process_exception(self, request, error):
        if not is_pool_acquisition_error(error):
            return None
        return http_pool_error_response(request, error)
