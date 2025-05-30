from django.utils.deprecation import MiddlewareMixin
# from django.http import JsonResponse
from mojo.helpers.response import JsonResponse
from mojo.apps.account.utils.jwtoken import JWToken
from mojo.apps.account.models.user import User
from mojo.helpers import request as rhelper
from mojo.helpers.request import get_user_agent

class JWTAuthenticationMiddleware(MiddlewareMixin):
    def process_request(self, request):
        request.group = None
        request.ip = rhelper.get_remote_ip(request)
        request.user_agent = rhelper.get_user_agent(request)
        token = request.META.get('HTTP_AUTHORIZATION', None)
        if token is None:
            return
        prefix, token = token.split()
        if prefix.lower() != 'bearer':
            return
        # decode data to find the user
        token_manager = JWToken()
        jwt_data = token_manager.decode(token, validate=False)
        if jwt_data.uid is None:
            return JsonResponse({'error': 'Invalid token data'}, status=401)
        user = User.objects.filter(id=jwt_data.uid).last()
        if user is None:
            return JsonResponse({'error': 'Invalid token user'}, status=401)
        token_manager.key = user.auth_key
        if not token_manager.is_token_valid(token):
            if token_manager.is_expired:
                return JsonResponse({'error': 'Token expired'}, status=401)
            return JsonResponse({'error': 'Token has invalid signature'}, status=401)
        request.user = user
