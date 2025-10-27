from django.utils.deprecation import MiddlewareMixin
# from django.http import JsonResponse
from mojo.helpers.response import JsonResponse
from mojo.apps.account.models.user import User

from mojo.helpers.settings import settings
from mojo.helpers import modules
from objict import objict


AUTH_BEARER_HANDLER_PATHS = settings.get("AUTH_BEARER_HANDLERS", {})

AUTH_BEARER_HANDLERS_CACHE = {
    "bearer": User.validate_jwt
}

AUTH_BEARER_NAME_MAP = settings.get("AUTH_BEARER_NAME_MAP", {"bearer": "user"})

class AuthenticationMiddleware(MiddlewareMixin):
    def process_request(self, request):
        request.bearer = None
        token = request.META.get('HTTP_AUTHORIZATION', None)
        if token is None:
            return
        prefix, token = token.split()
        prefix = prefix.lower()
        if prefix not in AUTH_BEARER_HANDLERS_CACHE:
            if prefix not in AUTH_BEARER_HANDLER_PATHS:
                return JsonResponse({'error': f'Invalid token type: {prefix}', 'paths': AUTH_BEARER_HANDLER_PATHS}, status=401)
            try:
                AUTH_BEARER_HANDLERS_CACHE[prefix] = modules.load_function(AUTH_BEARER_HANDLER_PATHS[prefix])
            except Exception as e:
                return JsonResponse({'error': "failed to load handler"}, status=500)

        handler = AUTH_BEARER_HANDLERS_CACHE[prefix]
        request.auth_token = objict(prefix=prefix, token=token)

        # decode data to find the instance
        instance, error = handler(token, request)
        if error is not None:
            return JsonResponse({'error': error}, status=401)
        key = AUTH_BEARER_NAME_MAP.get(prefix, prefix)
        setattr(request, key, instance)
        request.bearer = prefix
