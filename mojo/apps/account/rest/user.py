from mojo import decorators as md
from mojo.apps.account.utils.jwtoken import JWToken
# from django.http import JsonResponse
from mojo.helpers.response import JsonResponse
from mojo.apps.account.models.user import User
from mojo.apps.account.utils import tokens
from mojo.helpers import dates, crypto
from mojo import errors as merrors
from mojo.helpers.settings import settings

JWT_TOKEN_EXPIRY = settings.get("JWT_TOKEN_EXPIRY", 21600)
JWT_REFRESH_TOKEN_EXPIRY = settings.get("JWT_REFRESH_TOKEN_EXPIRY", 604800)
PASSWORD_RESET_TOKEN_TTL = settings.get("PASSWORD_RESET_TOKEN_TTL", 3600)
PASSWORD_RESET_CODE_TTL = settings.get("PASSWORD_RESET_CODE_TTL", 600)


@md.URL('user')
@md.URL('user/<int:pk>')
def on_user(request, pk=None):
    return User.on_rest_request(request, pk)


@md.GET('user/me')
@md.GET('account/user/me')
@md.requires_auth()
def on_user_me(request):
    return User.on_rest_request(request, request.user.pk)


@md.POST('refresh_token')
@md.POST('token/refresh')
@md.POST("auth/token/refresh")
@md.POST('account/jwt/refresh')
@md.requires_params("refresh_token")
def on_refresh_token(request):
    user, error = User.validate_jwt(request.DATA.refresh_token)
    if error is not None:
        raise merrors.PermissionDeniedException(error, 401, 401)
    # future look at keeping the refresh token the same but updating the access_token
    # TODO add device id to the token as well
    # user.touch()
    token_package = JWToken(user.get_auth_key()).create(uid=user.id)
    return JsonResponse(dict(status=True, data=token_package))


@md.POST("login")
@md.POST("auth/login")
@md.POST('account/jwt/login')
@md.requires_params("username", "password")
def on_user_login(request):
    username = request.DATA.username
    password = request.DATA.password
    from django.db.models import Q
    user = User.objects.filter(Q(username=username.lower().strip()) | Q(email=username.lower().strip())).last()
    if user is None:
        User.class_report_incident(
            f"login attempt with unknown username {username}",
            event_type="login:unknown",
            level=8,
            request=request)
        raise merrors.PermissionDeniedException()
    if not user.check_password(password):
        # Authentication successful
        user.report_incident(f"{user.username} enter an invalid password", "invalid_password")
        raise merrors.PermissionDeniedException("Invalid username or password", 401, 401)
    return jwt_login(request, user, "account/jwt/login" in request.path)


def jwt_login(request, user, legacy=False):
    user.last_login = dates.utcnow()
    user.track()
    keys = dict(uid=user.id)
    if request.device:
        keys['device'] = request.device.id
    access_token_expiry = JWT_TOKEN_EXPIRY
    refresh_token_expiry = JWT_REFRESH_TOKEN_EXPIRY
    if user.org:
        access_token_expiry = user.org.metadata.get("access_token_expiry", JWT_TOKEN_EXPIRY)
        refresh_token_expiry = user.org.metadata.get("refresh_token_expiry", JWT_REFRESH_TOKEN_EXPIRY)
    if legacy:
        keys.update(dict(user_id=user.id, device_id=request.DATA.get(["device_id", "deviceID"], request.device.id)))
    token_package = JWToken(
        user.get_auth_key(),
        access_token_expiry=access_token_expiry,
        refresh_token_expiry=refresh_token_expiry).create(**keys)
    token_package['user'] = user.to_dict("basic")
    if legacy:
        return {
            "status": True,
            "data": {
                "access": token_package.access_token,
                "refresh": token_package.refresh_token,
                "id": user.id
            }
        }
    return JsonResponse(dict(status=True, data=token_package))


@md.POST("auth/forgot")
@md.requires_params("email")
@md.public_endpoint()
def on_user_forgot(request):
    email = request.DATA.email
    user = User.objects.filter(email=email.lower().strip()).last()
    if user is None:
        User.class_report_incident(
            f"reset password with unknown email {email}",
            event_type="reset:unknown",
            level=8,
            request=request)
    else:
        user.report_incident(f"{user.username} requested a password reset", "password_reset")
        if request.DATA.get("method") == "code":
            code = crypto.random_string(6, True, False, False)
            user.set_secret("password_reset_code", code)
            user.set_secret("password_reset_code_ts", int(dates.utcnow().timestamp()))
            user.save()
            user.send_template_email("password_reset_code", dict(code=code))
        elif request.DATA.get("method") in ["link", "email"]:
            user.send_template_email("password_reset_link", dict(token=tokens.generate_password_reset_token(user)))
        else:
            raise merrors.ValueException("Invalid method")
    return JsonResponse(dict(status=True, message="If email in our system a reset email was sent."))


@md.POST("auth/password/reset/code")
@md.public_endpoint()
@md.requires_params("code", "email", "new_password")
def on_user_password_reset_code(request):
    code = request.DATA.get("code")
    email = request.DATA.get("email")
    new_password = request.DATA.get("new_password")
    user = User.objects.get(email=email)
    sec_code = user.get_secret("password_reset_code")
    code_ts = int(user.get_secret("password_reset_code_ts") or 0)
    now_ts = int(dates.utcnow().timestamp())
    if len(code or "") != 6 or code != (sec_code or ""):
        user.report_incident(f"{user.username} invalid password reset code", "password_reset")
        raise merrors.ValueException("Invalid code")
    if now_ts - code_ts > int(PASSWORD_RESET_CODE_TTL):
        user.report_incident(f"{user.username} expired password reset code", "password_reset")
        raise merrors.ValueException("Expired code")
    user.set_password(new_password)
    user.set_secret("password_reset_code", None)
    user.set_secret("password_reset_code_ts", None)
    user.save()
    return jwt_login(request, user)


@md.POST("auth/password/reset/token")
@md.custom_security("requires valid token")
@md.requires_params("token", "new_password")
def on_user_password_reset_token(request):
    token = request.DATA.get("token")
    user = tokens.verify_password_reset_token(token)
    new_password = request.DATA.get("new_password")
    user.set_password(new_password)
    user.save()
    return jwt_login(request, user)
