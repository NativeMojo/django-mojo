from django.utils.deprecation import MiddlewareMixin
# from django.http import JsonResponse
from mojo.helpers.response import JsonResponse
from mojo.apps.account.utils.jwtoken import JWToken
from mojo.apps.account.models.user import User
from mojo.helpers import request as rhelper
from mojo.helpers.request import get_user_agent
from objict import objict



class AuthenticationMiddleware(MiddlewareMixin):
    def process_request(self, request):
        token = request.META.get('HTTP_AUTHORIZATION', None)
        if token is None:
            return
        prefix, token = token.split()
        request.auth_token = objict(prefix=prefix, token=token)
        if prefix.lower() != 'bearer':
            return
        # decode data to find the user
        user, error = User.validate_jwt(token)
        if error is not None:
            return JsonResponse({'error': error}, status=401)
        request.user = user
        user.track(request)
