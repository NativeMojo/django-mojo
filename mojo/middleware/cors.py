from django.http import HttpResponse
from mojo.helpers.settings import settings

DUID_HEADER = settings.get_static('DUID_HEADER', 'X-Mojo-UID')

_BOUNCER_PREFIXES = (
    '/api/account/bouncer/',
    '/account/static/mojo-',
)


def _is_bouncer_path(path):
    for prefix in _BOUNCER_PREFIXES:
        if path.startswith(prefix):
            return True
    return False


def _credentialed_origin(request):
    """Return the request Origin if it is on the bouncer allowlist, else ''."""
    origin = request.META.get('HTTP_ORIGIN', '')
    if not origin:
        return ''
    allowed = settings.get_static('BOUNCER_ALLOWED_ORIGINS') or []
    if origin in allowed:
        return origin
    return ''


# middleware/cors.py
class CORSMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Handle preflight requests
        if request.method == 'OPTIONS':
            response = HttpResponse()
        else:
            response = self.get_response(request)

        # Bouncer paths: credentialed CORS with specific Origin (browsers
        # cannot send cookies with Allow-Origin: *), but only if the request
        # Origin is on the BOUNCER_ALLOWED_ORIGINS allowlist. Non-allowlisted
        # cross-origin requests still get the wildcard fallback below, which
        # blocks credentialed flows at the browser but keeps non-credentialed
        # API use working.
        if _is_bouncer_path(request.path):
            origin = _credentialed_origin(request)
            if origin:
                response['Access-Control-Allow-Origin'] = origin
                response['Access-Control-Allow-Credentials'] = 'true'
                response['Vary'] = 'Origin'

        # Default wildcard origin for any response the bouncer block didn't set
        if 'Access-Control-Allow-Origin' not in response:
            response['Access-Control-Allow-Origin'] = '*'

        # Allow all methods to minimize preflight requests
        response['Access-Control-Allow-Methods'] = 'GET, POST, PUT, PATCH, DELETE, HEAD, OPTIONS'

        # Allow common headers to minimize preflight requests
        response['Access-Control-Allow-Headers'] = (
            'Accept, Accept-Encoding, Authorization, Content-Type, '
            'Origin, User-Agent, X-Requested-With, X-CSRFToken, '
            f'X-API-Key, {DUID_HEADER}, Cache-Control, Pragma'
        )

        # Long preflight cache (24 hours)
        response['Access-Control-Max-Age'] = '86400'

        # Expose headers that frontend might need
        response['Access-Control-Expose-Headers'] = (
            'Content-Disposition, X-Total-Count, X-Bouncer-Muid, X-Bouncer-Reason'
        )

        return response
