"""Django exception hook for bounded database-pool acquisition failures."""

from django.utils.deprecation import MiddlewareMixin

from mojo.db.errors import http_pool_error_response, is_pool_acquisition_error


class DatabasePoolErrorMiddleware(MiddlewareMixin):
    """Map view-time pool queue failures through Django's exception hook."""

    def process_exception(self, request, error):
        if not is_pool_acquisition_error(error):
            return None
        return http_pool_error_response(request, error)
