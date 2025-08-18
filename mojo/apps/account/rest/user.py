from mojo import decorators as md
from mojo.apps.account.utils.jwtoken import JWToken
# from django.http import JsonResponse
from mojo.helpers.response import JsonResponse
from mojo.apps.account.models.user import User
from mojo.helpers import dates

@md.URL('user')
@md.URL('user/<int:pk>')
def on_user(request, pk=None):
    return User.on_rest_request(request, pk)


@md.GET('user/me')
def on_user_me(request):
    return User.on_rest_request(request, request.user.pk)


@md.POST('refresh_token')
@md.POST('token/refresh')
@md.POST("auth/token/refresh")
@md.requires_params("refresh_token")
def on_refresh_token(request):
    user, error = User.validate_jwt(request.DATA.refresh_token)
    if error is not None:
        return JsonResponse({'error': error}, status=401)
    # future look at keeping the refresh token the same but updating the access_token
    # TODO add device id to the token as well
    user.touch()
    token_package = JWToken(user.get_auth_key()).create(uid=user.id)
    return JsonResponse(dict(status=True, data=token_package))


@md.POST("login")
@md.POST("auth/login")
@md.requires_params("username", "password")
def on_user_login(request):
    username = request.DATA.username
    password = request.DATA.password
    user = User.objects.filter(username=username.lower().strip()).last()
    if user is None:
        return JsonResponse(dict(status=False, error="Invalid username or password", code=403))
    if not user.check_password(password):
        # Authentication successful
        user.report_incident(f"{user.username} enter an invalid password", "invalid_password")
        return JsonResponse(dict(status=False, error="Invalid username or password", code=401))
    user.last_login = dates.utcnow()
    user.touch()
    token_package = JWToken(user.get_auth_key()).create(uid=user.id)
    token_package['user'] = user.to_dict("basic")
    return JsonResponse(dict(status=True, data=token_package))
