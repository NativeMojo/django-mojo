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
ALLOW_PHONE_LOGIN = settings.get("ALLOW_PHONE_LOGIN", False)

@md.URL('user')
@md.URL('user/<int:pk>')
def on_user(request, pk=None):
    return User.on_rest_request(request, pk)


@md.GET('user/me')
@md.GET('account/user/me')
@md.requires_auth()
def on_user_me(request):
    if not hasattr(request.user, "is_request_user"):
        raise merrors.PermissionDeniedException("not valid user", 401, 401)
    return User.on_rest_request(request, request.user.pk)


@md.POST('refresh_token')
@md.POST('token/refresh')
@md.POST("auth/token/refresh")
@md.POST('account/jwt/refresh')
@md.rate_limit("refresh_token", ip_limit=30)
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
@md.strict_rate_limit("login", ip_limit=100, duid_limit=10, duid_window=300)
@md.endpoint_metrics("login_attempts", by=["ip", "duid"])
@md.requires_params("username", "password")
def on_user_login(request):
    username = request.DATA.username
    password = request.DATA.password
    from django.db.models import Q

    lookup = username.lower().strip()
    q = Q(username=lookup) | Q(email=lookup)
    if ALLOW_PHONE_LOGIN:
        from mojo.apps.phonehub.services.phonenumbers import normalize as normalize_phone
        normalized_phone = normalize_phone(username)
        if normalized_phone:
            q |= Q(phone_number=normalized_phone)
    user = User.objects.filter(q).last()
    if user is None:
        User.class_report_incident(
            f"login attempt with unknown username {username}",
            event_type="login:unknown",
            level=8,
            request=request)
        raise merrors.PermissionDeniedException()
    if not user.check_password(password):
        user.report_incident(f"{user.username} enter an invalid password", "invalid_password")
        raise merrors.PermissionDeniedException("Invalid username or password", 401, 401)
    mfa_methods = get_mfa_methods(user)
    if mfa_methods:
        return mfa_required_response(user, mfa_methods)
    return jwt_login(request, user, "account/jwt/login" in request.path)


def get_mfa_methods(user):
    """Return list of enabled MFA methods for a user."""
    methods = []
    from mojo.apps.account.models.totp import UserTOTP
    if UserTOTP.objects.filter(user=user, is_enabled=True).exists():
        methods.append("totp")
    if user.phone_number and user.is_phone_verified:
        methods.append("sms")
    return methods


def mfa_required_response(user, methods):
    """Return an MFA challenge response instead of a full JWT."""
    from mojo.apps.account.services import mfa as mfa_service
    token = mfa_service.create_mfa_token(user, methods)
    return JsonResponse({
        "status": True,
        "data": {
            "mfa_required": True,
            "mfa_token": token,
            "mfa_methods": methods,
            "expires_in": mfa_service.MFA_TOKEN_TTL,
        },
    })


def jwt_login(request, user, legacy=False):
    user.last_login = dates.utcnow()
    user.track()
    keys = dict(uid=user.id, ip=request.ip)
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
@md.strict_rate_limit("auth_forgot", ip_limit=5, ip_window=300)
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
@md.strict_rate_limit("password_reset_code", ip_limit=5, ip_window=300)
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
    # If the user has never logged in, this token was consumed via an invite link —
    # the fact they received and clicked it proves email ownership.
    if user.last_login is None:
        user.is_email_verified = True
    user.set_password(new_password)
    user.save()
    return jwt_login(request, user)


@md.POST("auth/magic/send")
@md.strict_rate_limit("magic_login_send", ip_limit=5, ip_window=300)
@md.requires_params("email")
@md.public_endpoint()
def on_magic_login_send(request):
    """Send a magic login link to the user's email address."""
    email = request.DATA.email.lower().strip()
    user = User.objects.filter(email=email).last()
    if user is None:
        User.class_report_incident(
            f"magic login attempt with unknown email {email}",
            event_type="magic:unknown",
            level=8,
            request=request)
    else:
        user.report_incident(f"{user.username} requested a magic login link", "magic_login")
        user.send_template_email("magic_login_link", dict(token=tokens.generate_magic_login_token(user)))
    # Always return success to prevent email enumeration
    return JsonResponse(dict(status=True, message="If email is in our system a login link was sent."))


@md.POST("auth/magic/login")
@md.strict_rate_limit("magic_login", ip_limit=10, ip_window=300)
@md.custom_security("requires valid magic login token")
@md.requires_params("token")
def on_magic_login_complete(request):
    """Exchange a magic login token for a JWT — logs the user in."""
    token = request.DATA.get("token")
    user = tokens.verify_magic_login_token(token)
    # Magic login always proves email ownership — mark verified on first use too.
    if not user.is_email_verified:
        user.is_email_verified = True
        user.save(update_fields=["is_email_verified", "modified"])
    return jwt_login(request, user)


@md.POST("auth/generate_api_key")
@md.requires_auth()
@md.requires_params("allowed_ips")
def generate_api_key(request):
    allowed_ips = request.DATA.get_typed("allowed_ips", typed=list)
    if len(allowed_ips) == 0:
        raise merrors.ValueException("Requires allowed_ips")
    expire_days = request.DATA.get_typed("expire_days", 360, typed=int)
    if expire_days > 360:
        raise merrors.ValueException("Invalid expire_days")
    token = request.user.generate_api_token(allowed_ips=allowed_ips, expire_days=expire_days)
    request.user.log(f"API Key Generated {token.jti} expire {expire_days} days", "api_key:generated")
    return dict(status=True, data=token)


@md.POST("auth/manage/generate_api_key")
@md.requires_perms("manage_users")
@md.requires_params("allowed_ips", "uid")
def generate_user_api_key(request):
    allowed_ips = request.DATA.get_typed("allowed_ips", typed=list)
    if len(allowed_ips) == 0:
        raise merrors.ValueException("Requires allowed_ips")
    expire_days = request.DATA.get_typed("expire_days", 360, typed=int)
    if expire_days > 360:
        raise merrors.ValueException("Invalid expire_days")
    user = User.objects.filter(pk=request.DATA.uid).last()
    if user is None:
        raise merrors.ValueException("Invalid User")
    token = user.generate_api_token(allowed_ips=allowed_ips, expire_days=expire_days)
    user.log(f"API Key Generated {token.jti} expire {expire_days} days by {request.user.username}", "api_key:generated")
    return dict(status=True, data=token)
