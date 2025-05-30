from mojo import decorators as jd
from mojo.apps.account.utils.jwtoken import JWToken
# from django.http import JsonResponse
from mojo.helpers.response import JsonResponse
from mojo.apps.account.models.user import User
import datetime

@jd.URL('user')
@jd.URL('user/<int:pk>')
def on_user(request, pk=None):
    return User.on_rest_request(request, pk)


@jd.GET("login")
@jd.requires_params("username", "password")
def on_user_login(request):
    username = request.DATA.username
    password = request.DATA.password
    user = User.objects.filter(username=username.lower().strip()).last()
    if user is None:
        return JsonResponse(dict(status=False, error="Invalid username or password", code=403))
    if not user.check_password(password):
        # Authentication successful
        return JsonResponse(dict(status=False, error="Invalid username or password", code=401))
    user.last_login = datetime.datetime.utcnow()
    user.save()
    token_package = JWToken(user.get_auth_key()).create(uid=user.id)
    return JsonResponse(dict(status=True, data=token_package))
