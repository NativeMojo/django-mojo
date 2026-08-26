from django.utils.deprecation import MiddlewareMixin
# from django.http import JsonResponse
from mojo.helpers.response import JsonResponse
from mojo.apps.account.models.user import User
from mojo.apps.account.models.api_key import ApiKey

from mojo.helpers.settings import settings
from mojo.helpers import modules
from objict import objict
from mojo.helpers import logit
from mojo.db import use_primary
from mojo.db.errors import http_pool_error_response, is_pool_acquisition_error

AUTH_BEARER_HANDLER_PATHS = settings.get_static("AUTH_BEARER_HANDLERS", {})

AUTH_BEARER_HANDLERS_CACHE = {
    "bearer": User.validate_jwt,
    "apikey": ApiKey.validate_token,
}

AUTH_BEARER_NAME_MAP = settings.get_static("AUTH_BEARER_NAME_MAP", {"bearer": "user", "apikey": "user"})

class AuthenticationMiddleware(MiddlewareMixin):
    def process_request(self, request):
        request.bearer = None
        token = request.META.get('HTTP_AUTHORIZATION', None)
        if token is None:
            return
        parts = token.split()
        if len(parts) == 1:
            # bare, scheme-less token (e.g. a Coinflow webhook validation key):
            # expose it for a downstream/public endpoint to read, but do NOT
            # authenticate — request.bearer stays None (fail-closed).
            request.auth_token = objict(prefix="raw", token=parts[0])
            return
        if len(parts) != 2:
            return  # empty or 3+ parts: genuinely malformed -> no credentials
        prefix, token = parts
        prefix = prefix.lower()
        if prefix not in AUTH_BEARER_HANDLERS_CACHE:
            if prefix not in AUTH_BEARER_HANDLER_PATHS:
                return JsonResponse({'error': f'Invalid token type: {prefix}', 'paths': AUTH_BEARER_HANDLER_PATHS}, status=401)
            try:
                AUTH_BEARER_HANDLERS_CACHE[prefix] = modules.load_function(AUTH_BEARER_HANDLER_PATHS[prefix])
            except Exception as e:
                logit.exception(f"failed to load handler for {prefix}: {e}")
                return JsonResponse({'error': "failed to load handler"}, status=500)

        handler = AUTH_BEARER_HANDLERS_CACHE[prefix]
        request.auth_token = objict(prefix=prefix, token=token)

        # decode data to find the instance
        try:
            with use_primary():
                instance, error = handler(token, request)
        except Exception as error:
            # Middleware exceptions are converted to 500 responses by Django
            # before an outer middleware's __call__ can see them. Authentication
            # is itself DB-touching, so its pool boundary must live here.
            if is_pool_acquisition_error(error):
                return http_pool_error_response(request, error)
            raise
        if error is not None:
            response = JsonResponse({'error': error}, status=401)
            # A bad bearer never reaches a view, so this is the ONLY place a
            # spec-compliant WWW-Authenticate can be attached to the 401 that
            # an OAuth resource server's client is waiting for. The handler
            # stamps the value (only at a live registered resource path); we
            # just copy it, so no other endpoint's 401 grows a header.
            challenge = getattr(request, "www_authenticate", None)
            if challenge:
                response["WWW-Authenticate"] = challenge
            return response
        key = AUTH_BEARER_NAME_MAP.get(prefix, prefix)
        setattr(request, key, instance)
        request.bearer = prefix
