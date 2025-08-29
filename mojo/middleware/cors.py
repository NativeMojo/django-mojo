from django.http import HttpResponse
from mojo.helpers.settings import settings

DUID_HEADER = settings.get('DUID_HEADER', 'X-Mojo-UID')

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

        # Always allow all origins
        response['Access-Control-Allow-Origin'] = '*'

        # Allow credentials if needed (note: can't use * origin with credentials)
        # response['Access-Control-Allow-Credentials'] = 'true'

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
        response['Access-Control-Expose-Headers'] = 'Content-Disposition, X-Total-Count'

        return response
