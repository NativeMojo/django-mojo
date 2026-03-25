from mojo import decorators as md
from mojo.apps.account.utils.jwtoken import JWToken
# from django.http import JsonResponse
from mojo.helpers.response import JsonResponse
from django.shortcuts import render
from django.http import HttpResponseRedirect
from mojo.apps.account.models.user import User
from mojo.apps.account.utils import tokens
from mojo.apps.account.utils.webapp_url import build_token_url
from mojo.apps.shortlink import maybe_shorten_url
from mojo.helpers import dates, crypto
from mojo import errors as merrors
from mojo.helpers.settings import settings


@md.URL('user')
@md.URL('user/<int:pk>')
def on_user(request, pk=None):
    return User.on_rest_request(request, pk)


@md.URL('user/me')
@md.URL('account/user/me')
@md.requires_auth()
def on_user_me(request):
    if not hasattr(request.user, "is_request_user"):
        raise merrors.PermissionDeniedException("not valid user", 401, 401)
    return User.on_rest_request(request, request.user.pk)


@md.POST('auth/manage/clear_rate_limit')
@md.requires_perms("manage_users")
def on_clear_rate_limit(request):
    """Clear rate limit counters for an IP or device. Requires manage_users permission."""
    from mojo.decorators.limits import clear_rate_limits
    ip = request.DATA.get("ip")
    key = request.DATA.get("key")
    duid = request.DATA.get("duid")
    deleted = clear_rate_limits(ip=ip, key=key, duid=duid)
    return JsonResponse({"status": True, "data": {"deleted": deleted}})


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
@md.requires_params("password")
@md.requires_bouncer_token('login')
def on_user_login(request):
    username = request.DATA.username
    password = request.DATA.password

    user, source = User.lookup_from_request_with_source(request, phone_as_username=settings.get("ALLOW_PHONE_LOGIN", False, kind="bool"))
    if user is None:
        User.class_report_incident(
            f"login attempt with unknown username {username}",
            event_type="login:unknown",
            level=8,
            request=request)
        raise merrors.PermissionDeniedException("Invalid username or password", 401, 401)
    if not user.check_password(password):
        user.report_incident(f"{user.username} enter an invalid password", "invalid_password")
        raise merrors.PermissionDeniedException("Invalid username or password", 401, 401)
    mfa_methods = get_mfa_methods(user)
    if mfa_methods:
        return mfa_required_response(user, mfa_methods)
    return jwt_login(request, user, "account/jwt/login" in request.path, source=source)


@md.POST("auth/register")
@md.public_endpoint()
@md.strict_rate_limit("register", ip_limit=5, ip_window=300)
@md.requires_bouncer_token('registration')
@md.requires_params("email", "password")
def on_register(request):
    """
    Create a new user account.

    Gated by the ALLOW_USER_REGISTRATION setting (default False).

    Required body params: email, password
    Optional body params: first_name, last_name

    When REQUIRE_VERIFIED_EMAIL is True the response includes
    requires_verification=True and no JWT is issued — the user must
    verify their email before they can log in.

    When REQUIRE_VERIFIED_EMAIL is False the user is logged in
    immediately and a verification email is still sent as a nudge.
    """
    if not settings.get("ALLOW_USER_REGISTRATION", False, kind="bool"):
        raise merrors.PermissionDeniedException("Registration is not enabled", 403, 403)

    email = request.DATA.email.lower().strip()
    password = request.DATA.password

    if User.objects.filter(email=email).exists():
        raise merrors.ValueException("An account with this email already exists")

    user = User(email=email)
    user.username = user.generate_username_from_email()
    first_name = request.DATA.get("first_name", "").strip()
    last_name = request.DATA.get("last_name", "").strip()
    if first_name:
        user.first_name = first_name
    if last_name:
        user.last_name = last_name
    user.check_password_strength(password)
    user.set_password(password)
    user.save()

    # send verification email
    token = tokens.generate_email_verify_token(user)
    user.send_template_email("email_verify", context=dict(token=token))
    user.report_incident(f"{user.username} registered via email", "register:success")

    require_verified = settings.get("REQUIRE_VERIFIED_EMAIL", False, kind="bool")
    if require_verified:
        return JsonResponse(dict(
            status=True,
            requires_verification=True,
            message="Account created. Please check your email to verify your account before logging in."
        ))
    return jwt_login(request, user)


def get_mfa_methods(user):
    """Return list of enabled MFA methods for a user, or empty if MFA not required."""
    if not user.requires_mfa:
        return []
    methods = []
    from mojo.apps.account.models.totp import UserTOTP
    if UserTOTP.objects.filter(user=user, is_enabled=True).exists():
        methods.append("totp")
    if user.phone_number and user.is_phone_verified:
        methods.append("sms")
    from mojo.apps.account.models.pkey import Passkey
    if Passkey.objects.filter(user=user, is_enabled=True).exists():
        methods.append("passkey")
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
            "expires_in": settings.get("MFA_TOKEN_TTL", 300, kind="int"),
        },
    })


def _check_verification_gate(user, source=None):
    """
    Raises PermissionDeniedException with error='email_not_verified' or
    'phone_not_verified' if the relevant verification setting is enabled
    and the user's channel is not yet verified.

    Blocks login regardless of how the user authenticated (email, username,
    phone). If REQUIRE_VERIFIED_EMAIL is True the user must have a verified
    email — period.

    Settings are read at call time (not cached at import time) so that
    Django's override_settings works correctly in tests.
    """
    require_verified_email = settings.get("REQUIRE_VERIFIED_EMAIL", False)
    require_verified_phone = settings.get("REQUIRE_VERIFIED_PHONE", False)
    if require_verified_email and not user.is_email_verified:
        raise merrors.PermissionDeniedException(
            "email_not_verified", 403, 403
        )
    if require_verified_phone and not user.is_phone_verified:
        raise merrors.PermissionDeniedException(
            "phone_not_verified", 403, 403
        )


def jwt_login(request, user, legacy=False, source=None, extra=None):
    if source is not None:
        _check_verification_gate(user, source)
    user.last_login = dates.utcnow()
    user.track()
    keys = dict(uid=user.id, ip=request.ip)
    if request.device:
        keys['device'] = request.device.id
    access_token_expiry = settings.get("JWT_TOKEN_EXPIRY", 21600, kind="int")
    refresh_token_expiry = settings.get("JWT_REFRESH_TOKEN_EXPIRY", 604800, kind="int")
    if user.org:
        access_token_expiry = user.org.metadata.get("access_token_expiry", access_token_expiry)
        refresh_token_expiry = user.org.metadata.get("refresh_token_expiry", refresh_token_expiry)
    if legacy:
        keys.update(dict(user_id=user.id, device_id=request.DATA.get(["device_id", "deviceID"], request.device.id)))
    token_package = JWToken(
        user.get_auth_key(),
        access_token_expiry=access_token_expiry,
        refresh_token_expiry=refresh_token_expiry).create(**keys)
    token_package['user'] = user.to_dict("basic")
    # track webapp origin for multi-tenant URL resolution
    webapp_url = request.DATA.get("webapp_base_url") or request.META.get("HTTP_ORIGIN")
    if webapp_url:
        if not user.get_protected_metadata("orig_webapp_url"):
            user.set_protected_metadata("orig_webapp_url", webapp_url)
        else:
            user.set_protected_metadata("last_webapp_url", webapp_url)
    if legacy:
        return {
            "status": True,
            "data": {
                "access": token_package.access_token,
                "refresh": token_package.refresh_token,
                "id": user.id
            }
        }
    response_data = dict(token_package)
    if extra:
        response_data.update(extra)
    return JsonResponse(dict(status=True, data=response_data))


@md.POST("auth/forgot")
@md.strict_rate_limit("auth_forgot", ip_limit=5, ip_window=300)
@md.public_endpoint()
def on_user_forgot(request):
    user = User.lookup_from_request(request, phone_as_username=True)
    if user is None:
        User.class_report_incident(
            f"reset password with details {request.DATA.username} - {request.DATA.email} - {request.DATA.phone_number}",
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
            token = tokens.generate_password_reset_token(user)
            token_url = build_token_url("password_reset", token, request=request, user=user, group=getattr(request, "group", None))
            token_url = maybe_shorten_url(token_url, source="password_reset", user=user, expire_hours=1)
            user.send_template_email("password_reset_link", dict(token=token, token_url=token_url))
        else:
            raise merrors.ValueException("Invalid method")
    return JsonResponse(dict(status=True, message="If email in our system a reset email was sent."))


@md.POST("auth/password/reset/code")
@md.strict_rate_limit("password_reset_code", ip_limit=5, ip_window=300)
@md.public_endpoint()
@md.requires_params("code", "new_password")
def on_user_password_reset_code(request):
    code = request.DATA.get("code")
    new_password = request.DATA.get("new_password")
    user = User.lookup_from_request(request, phone_as_username=True)
    if user is None:
        User.class_report_incident(
            f"invalid reset password code with details {request.DATA.username} - {request.DATA.email} - {request.DATA.phone_number}",
            event_type="reset:unknown",
            level=8,
            request=request)
        raise merrors.ValueException("Invalid code")

    sec_code = user.get_secret("password_reset_code")
    code_ts = int(user.get_secret("password_reset_code_ts") or 0)
    now_ts = int(dates.utcnow().timestamp())
    if len(code or "") != 6 or code != (sec_code or ""):
        user.report_incident(f"{user.username} invalid password reset code", "password_reset")
        raise merrors.ValueException("Invalid code")
    if now_ts - code_ts > settings.get("PASSWORD_RESET_CODE_TTL", 600, kind="int"):
        user.report_incident(f"{user.username} expired password reset code", "password_reset")
        raise merrors.ValueException("Expired code")
    user.check_password_strength(new_password)
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
    new_password = request.DATA.get("new_password")
    if token.startswith("iv:"):
        user = tokens.verify_invite_token(token)
        user.is_email_verified = True
    elif token.startswith("pr:"):
        user = tokens.verify_password_reset_token(token)
        # If the user has never logged in, this token was consumed via an invite link —
        # the fact they received and clicked it proves email ownership.
        if user.last_login is None:
            user.is_email_verified = True
    else:
        raise merrors.ValueException("Invalid token kind")
    user.check_password_strength(new_password)
    user.set_password(new_password)
    user.save()
    return jwt_login(request, user)


@md.POST("auth/magic/send")
@md.strict_rate_limit("magic_login_send", ip_limit=5, ip_window=300)
@md.public_endpoint()
def on_magic_login_send(request):
    """Send a magic login link via email (default) or SMS (method=sms)."""
    channel = request.DATA.get("method", "email")
    if channel not in ("email", "sms"):
        channel = "email"

    user = User.lookup_from_request(request, phone_as_username=True)

    if user is None:
        User.class_report_incident(
            f"magic login attempt with unknown identifier {request.DATA.username} - {request.DATA.email} - {request.DATA.phone_number}",
            event_type="magic:unknown",
            level=8,
            request=request)
    else:
        user.report_incident(f"{user.username} requested a magic login link via {channel}", "magic_login")
        magic_token = tokens.generate_magic_login_token(user, channel=channel)
        group = getattr(request, "group", None)
        token_url = build_token_url("magic_login", magic_token, request=request, user=user, group=group)
        if channel == "sms":
            from mojo.apps import phonehub
            if user.phone_number:
                login_url = maybe_shorten_url(token_url, source="magic_login_sms", user=user, expire_hours=1)
                phonehub.send_sms(user.phone_number, f"Your login link: {login_url}")
        else:
            token_url = maybe_shorten_url(token_url, source="magic_login", user=user, expire_hours=1)
            user.send_template_email("magic_login_link", dict(token=magic_token, token_url=token_url))
    return JsonResponse(dict(status=True, message="If account is in our system a login link was sent."))


@md.POST("auth/magic/login")
@md.strict_rate_limit("magic_login", ip_limit=10, ip_window=300)
@md.custom_security("requires valid magic login token")
@md.requires_params("token")
def on_magic_login_complete(request):
    """Exchange a magic login token for a JWT — logs the user in."""
    token = request.DATA.get("token")
    user, channel = tokens.verify_magic_login_token(token)
    if channel == "sms" and not user.is_phone_verified:
        user.is_phone_verified = True
        user.save(update_fields=["is_phone_verified", "modified"])
    elif channel == "email" and not user.is_email_verified:
        user.is_email_verified = True
        user.save(update_fields=["is_email_verified", "modified"])
    return jwt_login(request, user)


# -----------------------------------------------------------------
# Email verification
# -----------------------------------------------------------------

@md.POST("auth/email/verify/send")
@md.strict_rate_limit("email_verify_send", ip_limit=5, ip_window=300)
@md.public_endpoint()
def on_email_verify_send(request):
    """Send an email verification link. Accepts username or email."""
    from mojo.apps.account.utils import tokens as tok_utils
    user = User.lookup_from_request(request)
    if user is None or not user.is_active:
        # No enumeration — always return success regardless of existence or active state
        return JsonResponse({"status": True, "message": "If the account exists, a verification email was sent."})
    if user.is_email_verified:
        return JsonResponse({"status": True, "message": "Email is already verified."})
    token = tok_utils.generate_email_verify_token(user)
    token_url = build_token_url("email_verify", token, request=request, user=user, group=getattr(request, "group", None))
    token_url = maybe_shorten_url(token_url, source="email_verify", user=user, expire_hours=24)
    user.send_template_email("email_verify_link", dict(token=token, token_url=token_url))
    return JsonResponse({"status": True, "message": "If the account exists, a verification email was sent."})


@md.POST("auth/email/verify")
@md.strict_rate_limit("email_verify", ip_limit=10, ip_window=300)
@md.custom_security("requires valid email verify token")
@md.requires_params("token")
def on_email_verify(request):
    """Exchange an email verify token — marks email verified and logs the user in."""
    from mojo.apps.account.utils import tokens as tok_utils
    token = request.DATA.get("token")
    user = tok_utils.verify_email_verify_token(token)
    if not user.is_active:
        raise merrors.PermissionDeniedException("Account is disabled", 403, 403)
    user.is_email_verified = True
    user.save(update_fields=["is_email_verified", "modified"])
    _send_account_realtime_event(user, "account:email:verified", {"email": user.email})
    return jwt_login(request, user)


@md.POST("auth/invite/accept")
@md.strict_rate_limit("invite_accept", ip_limit=10, ip_window=300)
@md.custom_security("requires valid invite token")
@md.requires_params("token")
def on_invite_accept(request):
    """
    Accept an invite token.
    Marks email as verified and issues a JWT.
    If the user has no password yet, the client should prompt them to set one
    via POST /api/auth/password/reset/token (the invite token is the same shape).
    """
    from mojo.apps.account.utils import tokens as tok_utils
    token = request.DATA.get("token")
    user = tok_utils.verify_invite_token(token)
    if not user.is_active:
        raise merrors.PermissionDeniedException("Account is disabled", 403, 403)
    user.is_email_verified = True
    user.save(update_fields=["is_email_verified", "modified"])
    return jwt_login(request, user)


# -----------------------------------------------------------------
# Email change (self-service, verify-then-commit)
# -----------------------------------------------------------------

@md.POST("auth/email/change/request")
@md.requires_auth()
@md.strict_rate_limit("email_change_request", ip_limit=5, ip_window=3600)
@md.requires_params("email")
def on_email_change_request(request):
    """
    Begin a self-service email change. current_password is optional.
    If provided and non-empty, it is validated; otherwise the password check
    is skipped (supports OAuth/passkey-only users).

    Optional body param:
      method: "link" (default) — send a confirmation link (ec: token) to the new address
      method: "code"           — send a 6-digit OTP to the new address instead

    In both cases a notification is sent to the OLD address alerting them of
    the change request. The current email is NOT changed until the confirm
    step is completed.
    """
    if not settings.get("ALLOW_EMAIL_CHANGE", True):
        raise merrors.PermissionDeniedException("Email change is not allowed")

    import re
    from mojo.apps.account.utils import tokens as tok_utils

    user = request.user
    new_email = request.DATA.get("email", "").lower().strip()
    current_password = request.DATA.get("current_password", "")

    if current_password:
        if not user.check_password(current_password):
            user.report_incident("Invalid password on email change request", "email_change:bad_password")
            raise merrors.PermissionDeniedException("Incorrect password", 401, 401)
    if not new_email or not re.match(r"[^@]+@[^@]+\.[^@]+", new_email):
        raise merrors.ValueException("Invalid email address")
    if new_email == str(user.email).lower():
        raise merrors.ValueException("New email must be different from current email")
    if User.objects.filter(email=new_email).exclude(pk=user.pk).exists():
        raise merrors.ValueException("Email already in use")

    method = request.DATA.get("method", "link")

    if method == "code":
        otp = tok_utils.generate_email_change_otp(user, new_email)
        _send_email_change_code(user, new_email, otp)
        user.send_template_email("email_change_notify", dict(new_email=new_email))
        user.report_incident(f"{user.username} requested email change to {new_email} (code)", "email_change:requested_code")
        return JsonResponse({"status": True, "message": "A verification code has been sent to your new email address."})

    token = tok_utils.generate_email_change_token(user, new_email)

    # Confirmation link sent to the NEW address — resolve the mailbox the same way
    # send_template_email does internally, since that method always sends to self.email.
    _send_email_change_confirm(user, new_email, token)

    # Notification to the OLD address — no cancel token (single-JTI design means issuing
    # a second ec: token would immediately invalidate the first). The user can cancel via
    # POST /api/auth/email/change/cancel while authenticated, or simply let the 1h link expire.
    user.send_template_email("email_change_notify", dict(new_email=new_email))

    user.report_incident(f"{user.username} requested email change to {new_email}", "email_change:requested")
    return JsonResponse({"status": True, "message": "A confirmation link has been sent to your new email address."})


def _send_email_change_confirm(user, new_email, token):
    """
    Send the email-change confirmation link to the NEW address.
    Uses the same mailbox-resolution logic as user.send_template_email but
    overrides the recipient so the message goes to new_email, not user.email.
    """
    from mojo.apps.aws.models import Mailbox

    mailbox = None
    if user.org and hasattr(user.org, "metadata"):
        domain = user.org.metadata.get("domain")
        if domain:
            mailbox = Mailbox.get_domain_default(domain)
            if not mailbox:
                mailbox = Mailbox.objects.filter(
                    domain__name__iexact=domain,
                    allow_outbound=True,
                ).first()
    if not mailbox:
        mailbox = Mailbox.get_system_default()

    if not mailbox:
        user.report_incident(
            "No mailbox available to send email change confirmation",
            "email:no_mailbox",
            level=6,
        )
        return

    context = {
        "user": user.to_dict("basic"),
        "token": token,
        "new_email": new_email,
    }
    try:
        mailbox.send_template_email(
            to=new_email,
            template_name="email_change_confirm",
            context=context,
            allow_unverified=True,
        )
    except Exception as e:
        user.report_incident(
            f"email change confirm send failed: {e}",
            "email:send_failed",
            level=6,
        )


def _send_email_change_code(user, new_email, otp):
    """
    Send the email-change OTP code to the NEW address.
    Uses identical mailbox resolution to _send_email_change_confirm.
    """
    from mojo.apps.aws.models import Mailbox

    mailbox = None
    if user.org and hasattr(user.org, "metadata"):
        domain = user.org.metadata.get("domain")
        if domain:
            mailbox = Mailbox.get_domain_default(domain)
            if not mailbox:
                mailbox = Mailbox.objects.filter(
                    domain__name__iexact=domain,
                    allow_outbound=True,
                ).first()
    if not mailbox:
        mailbox = Mailbox.get_system_default()

    if not mailbox:
        user.report_incident(
            "No mailbox available to send email change code",
            "email:no_mailbox",
            level=6,
        )
        return

    context = {
        "user": user.to_dict("basic"),
        "code": otp,
        "new_email": new_email,
    }
    try:
        mailbox.send_template_email(
            to=new_email,
            template_name="email_change_code",
            context=context,
            allow_unverified=True,
        )
    except Exception as e:
        user.report_incident(
            f"email change code send failed: {e}",
            "email:send_failed",
            level=6,
        )


def _send_account_realtime_event(user, event, data):
    """
    Fire-and-forget realtime event to all of a user's active WebSocket connections.
    Silently swallows errors — realtime delivery is best-effort, never blocking.
    """
    try:
        from mojo.apps import realtime
        realtime.send_to_user("user", user.pk, {"event": event, "data": data})
    except Exception:
        pass


def _render_confirm(request, template, ctx):
    """
    Render an account confirmation template.
    If ?redirect=<url> is present and success=True, honour it:
      - immediately if redirect_delay is falsy / zero
      - after a short delay via <meta http-equiv=refresh> otherwise
    Downstream projects can override the templates by placing their own versions
    under templates/account/<name>.html with higher priority in TEMPLATES.DIRS.
    """
    redirect_url = request.DATA.get("redirect") or request.GET.get("redirect", "")
    redirect_delay = 3 if ctx.get("success") else 0

    if redirect_url and ctx.get("success") and not redirect_delay:
        return HttpResponseRedirect(redirect_url)

    ctx["redirect_url"] = redirect_url
    ctx["redirect_delay"] = redirect_delay
    return render(request, f"account/{template}", ctx)


@md.POST("auth/email/change/confirm")
@md.strict_rate_limit("email_change_confirm", ip_limit=10, ip_window=3600)
@md.custom_security("requires valid email change token, or authenticated session with valid OTP code")
def on_email_change_confirm(request):
    """
    Complete an email change.

    Accepts either:
      { "token": "ec:..." }   — existing link flow; no auth required (token is the credential)
      { "code": "123456" }    — code flow; requires authentication (Bearer token)

    In both cases: commits the new email, marks it verified, rotates auth_key
    (invalidates all other sessions), and issues a fresh JWT.
    """
    import uuid
    from mojo.apps.account.utils import tokens as tok_utils

    token = request.DATA.get("token")
    code = request.DATA.get("code")

    if not token and not code:
        raise merrors.ValueException("token or code is required")

    if code:
        # Code path — user must be authenticated; identity comes from the JWT
        if not request.user or not request.user.is_authenticated:
            raise merrors.PermissionDeniedException("Authentication required", 401, 401)
        user = request.user
        new_email = tok_utils.verify_email_change_otp(user, code)
    else:
        # Link/token path — token is the credential; no active session required
        user, new_email = tok_utils.verify_email_change_token(token)

    if not user.is_active:
        raise merrors.PermissionDeniedException("Account is disabled", 403, 403)

    # Confirm new email is still available (another account may have claimed it in the interim)
    if User.objects.filter(email=new_email).exclude(pk=user.pk).exists():
        raise merrors.ValueException("Email address is no longer available")

    old_email = str(user.email)

    # Commit the change — bypass the REST guard by updating directly
    User.objects.filter(pk=user.pk).update(
        email=new_email,
        is_email_verified=True,
        auth_key=uuid.uuid4().hex,  # invalidate all other active sessions
    )
    # Update username too if it mirrored the old email
    if str(user.username).lower() == old_email.lower():
        User.objects.filter(pk=user.pk).update(username=new_email)

    user.refresh_from_db()
    user.log(kind="email:changed", log=f"{old_email} to {new_email}")

    # Notify any other open sessions — they should refresh their profile
    # (auth_key was just rotated so their JWTs are already invalid, but the
    # event gives them a clean signal to re-prompt login rather than silently failing)
    _send_account_realtime_event(user, "account:email:changed", {"email": new_email})

    return jwt_login(request, user)


@md.GET("auth/email/change/confirm")
@md.strict_rate_limit("email_change_confirm", ip_limit=10, ip_window=3600)
@md.public_endpoint()
def on_email_change_confirm_get(request):
    """
    GET handler for the email-change confirmation link clicked from the user's inbox.

    Validates the ec: token, commits the change, then renders a Django template
    page (account/email_change_confirm.html).  If a ?redirect=<url> param is
    present it will be used as a "Continue" button target and as an automatic
    redirect (after a brief delay) on success.

    Downstream projects can override the template by placing their own version at
    templates/account/email_change_confirm.html with higher priority in TEMPLATES.DIRS.
    """
    import uuid
    from mojo.apps.account.utils import tokens as tok_utils

    token = request.DATA.get("token") or request.GET.get("token", "")
    if not token:
        return _render_confirm(request, "email_change_confirm.html", {
            "success": False,
            "error_title": "Link invalid",
            "error_message": "No token was provided. Please use the link from your confirmation email.",
        })

    try:
        user, new_email = tok_utils.verify_email_change_token(token)
    except Exception:
        return _render_confirm(request, "email_change_confirm.html", {
            "success": False,
            "error_title": "Link invalid or expired",
            "error_message": "This email change link is invalid or has already been used. Links expire after 1 hour and can only be used once.",
        })

    if not user.is_active:
        return _render_confirm(request, "email_change_confirm.html", {
            "success": False,
            "error_title": "Account disabled",
            "error_message": "This account has been disabled. Please contact support.",
        })

    if User.objects.filter(email=new_email).exclude(pk=user.pk).exists():
        return _render_confirm(request, "email_change_confirm.html", {
            "success": False,
            "error_title": "Address no longer available",
            "error_message": f"The address {new_email} has been registered by another account since this link was issued. Please request a new email change.",
        })

    old_email = str(user.email)

    User.objects.filter(pk=user.pk).update(
        email=new_email,
        is_email_verified=True,
        auth_key=uuid.uuid4().hex,
    )
    if str(user.username).lower() == old_email.lower():
        User.objects.filter(pk=user.pk).update(username=new_email)

    user.refresh_from_db()
    user.log(kind="email:changed", log=f"{old_email} to {new_email}")
    _send_account_realtime_event(user, "account:email:changed", {"email": new_email})

    return _render_confirm(request, "email_change_confirm.html", {
        "success": True,
        "new_email": new_email,
    })


@md.POST("auth/email/change/cancel")
@md.requires_auth()
@md.strict_rate_limit("email_change_cancel", ip_limit=10, ip_window=3600)
def on_email_change_cancel(request):
    """
    Cancel a pending email change. Clears the stored pending_email so the
    outstanding confirmation link becomes useless even before it expires.
    Requires authentication (the real owner cancels via their active session).
    """
    import mojo.apps.account.utils.tokens as tok_module

    user = request.user
    pending = user.get_secret("pending_email")
    if not pending:
        return JsonResponse({"status": True, "message": "No pending email change to cancel."})
    user.set_secret("pending_email", None)
    # Clear the link-flow JTI so any outstanding ec: token is immediately dead
    user.set_secret(tok_module._JTI_KEYS[tok_module.KIND_EMAIL_CHANGE], None)
    # Clear the code-flow OTP so any outstanding code is immediately dead
    user.set_secret("email_change_otp", None)
    user.set_secret("email_change_otp_ts", None)
    user.save(update_fields=["mojo_secrets", "modified"])
    user.report_incident(f"{user.username} cancelled pending email change to {pending}", "email_change:cancelled")
    return JsonResponse({"status": True, "message": "Pending email change has been cancelled."})


# -----------------------------------------------------------------
# Phone number change (self-service, OTP-verify-then-commit)
# -----------------------------------------------------------------

@md.POST("auth/phone/change/request")
@md.requires_auth()
@md.strict_rate_limit("phone_change_request", ip_limit=5, ip_window=3600)
@md.requires_params("phone_number")
def on_phone_change_request(request):
    """
    Begin a self-service phone number change. current_password is optional.
    If provided and non-empty, it is validated; otherwise the password check is skipped.
    Sends a 6-digit OTP to the NEW number via SMS.
    The phone number is NOT changed until the user submits the correct OTP in /confirm.
    """
    from mojo.apps.account.utils import tokens as tok_utils
    from mojo.apps import phonehub

    if not settings.get("ALLOW_PHONE_CHANGE", True):
        raise merrors.PermissionDeniedException("Phone number change is not allowed")

    user = request.user
    current_password = request.DATA.get("current_password")
    if current_password:
        if not user.check_password(current_password):
            user.report_incident("Invalid password on phone change request", "phone_change:bad_password")
            raise merrors.PermissionDeniedException("Incorrect password", 401, 401)

    new_phone_raw = request.DATA.get("phone_number", "").strip()
    normalized = user.normalize_phone(new_phone_raw)
    if not normalized:
        raise merrors.ValueException("Invalid phone number format")

    if normalized == user.phone_number:
        raise merrors.ValueException("New phone number must be different from current phone number")

    if User.objects.filter(phone_number=normalized).exclude(pk=user.pk).exists():
        raise merrors.ValueException("Phone number already in use")

    session_token, otp = tok_utils.generate_phone_change_token(user, normalized)

    sms = phonehub.send_sms(normalized, f"Your phone change verification code is: {otp}")
    if sms and sms.status == "failed":
        # Clear the pending state so the user can retry cleanly
        user.set_secret("pending_phone", None)
        user.set_secret("phone_change_otp", None)
        user.set_secret("phone_change_otp_ts", None)
        user.save(update_fields=["mojo_secrets", "modified"])
        raise merrors.ValueException("Failed to send SMS to the new number — check the number and try again")

    # Notify the OLD phone number so the real owner knows a change was requested
    if user.phone_number:
        try:
            phonehub.send_sms(user.phone_number, "A request was made to change your phone number. If this wasn't you, secure your account immediately.")
        except Exception:
            pass

    user.report_incident(
        f"{user.username} requested phone number change to {normalized}",
        "phone_change:requested")
    return JsonResponse({
        "status": True,
        "session_token": session_token,
        "message": "A verification code has been sent to your new phone number.",
    })


@md.POST("auth/phone/change/confirm")
@md.strict_rate_limit("phone_change_confirm", ip_limit=10, ip_window=3600)
@md.requires_auth()
@md.requires_params("session_token", "code")
def on_phone_change_confirm(request):
    """
    Complete a phone number change by submitting the session token and OTP.
    Commits the new phone number and resets is_phone_verified to True.
    The user stays logged in — no session rotation needed (phone is not used
    for auth_key signing the way email is).
    """
    from mojo.apps.account.utils import tokens as tok_utils

    session_token = request.DATA.get("session_token")
    code = request.DATA.get("code")

    # Must be authenticated AND the token must belong to the same user —
    # verify_phone_change_token checks JTI / TTL / OTP; we additionally
    # confirm the resolved user matches the session to prevent token-swap attacks.
    token_user, new_phone = tok_utils.verify_phone_change_token(session_token, code)
    if token_user.pk != request.user.pk:
        raise merrors.PermissionDeniedException("Session mismatch")

    user = request.user

    if not user.is_active:
        raise merrors.PermissionDeniedException("Account is disabled", 403, 403)

    # Re-check availability — another account may have claimed the number in the window
    if User.objects.filter(phone_number=new_phone).exclude(pk=user.pk).exists():
        raise merrors.ValueException("Phone number is no longer available")

    old_phone = str(user.phone_number or "")

    # Commit directly — bypass on_rest_pre_save guard (which blocks direct phone changes)
    User.objects.filter(pk=user.pk).update(
        phone_number=new_phone,
        is_phone_verified=True,
    )

    user.refresh_from_db()
    user.log(kind="phone:changed", log=f"{old_phone} to {new_phone}")
    user.report_incident(
        f"{user.username} phone number changed to {new_phone}",
        "phone_change:confirmed")

    return JsonResponse({"status": True, "message": "Phone number updated successfully."})


@md.POST("auth/phone/change/cancel")
@md.requires_auth()
@md.strict_rate_limit("phone_change_cancel", ip_limit=10, ip_window=3600)
def on_phone_change_cancel(request):
    """
    Cancel a pending phone number change. Clears the stored pending_phone and OTP
    so the outstanding session token is immediately dead even before the TTL expires.
    Idempotent — returns 200 if there is no pending change.
    """
    import mojo.apps.account.utils.tokens as tok_module

    user = request.user
    pending = user.get_secret("pending_phone")
    if not pending:
        return JsonResponse({"status": True, "message": "No pending phone change to cancel."})

    user.set_secret("pending_phone", None)
    user.set_secret("phone_change_otp", None)
    user.set_secret("phone_change_otp_ts", None)
    # Kill the session token JTI so any outstanding pc: token is immediately invalid
    user.set_secret(tok_module._JTI_KEYS[tok_module.KIND_PHONE_CHANGE], None)
    user.save(update_fields=["mojo_secrets", "modified"])
    user.report_incident(
        f"{user.username} cancelled pending phone change to {pending}",
        "phone_change:cancelled")
    return JsonResponse({"status": True, "message": "Pending phone number change has been cancelled."})


# -----------------------------------------------------------------
# Username change
# -----------------------------------------------------------------

@md.POST("auth/username/change")
@md.requires_auth()
@md.requires_params("username", "current_password")
def on_username_change(request):
    """Self-service username change. Requires current_password as proof of ownership."""
    if not settings.get("ALLOW_USERNAME_CHANGE", True):
        raise merrors.PermissionDeniedException("Username change is not allowed")

    user = request.user
    current_password = request.DATA.get("current_password", "")

    if not user.has_usable_password():
        raise merrors.ValueException(
            "No password set on this account. Use password reset to set one first."
        )

    if not user.check_password(current_password):
        user.report_incident(
            f"{user.username} entered invalid password on username change",
            "username:change_failed",
        )
        raise merrors.PermissionDeniedException("Incorrect password", 401, 401)

    new_username = request.DATA.get("username", "").lower().strip()
    if not new_username:
        raise merrors.ValueException("Username is required")

    if new_username == user.username:
        raise merrors.ValueException("New username must be different from current username")

    # Set on the user instance so validate_username() reads self.username
    old_username = user.username
    user.username = new_username
    try:
        user.validate_username()
    except Exception:
        user.username = old_username
        raise

    # Uniqueness check (exclude self)
    if User.objects.filter(username=new_username).exclude(pk=user.pk).exists():
        user.username = old_username
        raise merrors.ValueException("Username already taken")

    user.save(update_fields=["username", "modified"])
    user.log(f"Username changed from {old_username} to {new_username}", "username:changed")

    return JsonResponse({
        "status": True,
        "data": {"username": user.username},
    })


# -----------------------------------------------------------------
# Session revoke (log out everywhere)
# -----------------------------------------------------------------

@md.POST("auth/sessions/revoke")
@md.requires_auth()
@md.requires_params("current_password")
@md.rate_limit("sessions_revoke", ip_limit=5, ip_window=300)
def on_sessions_revoke(request):
    """
    Rotate auth_key to invalidate all active sessions. Returns a fresh JWT
    for the calling session so the user stays logged in.
    """
    import uuid

    user = request.user
    current_password = request.DATA.get("current_password", "")

    if not user.check_password(current_password):
        user.report_incident(
            f"{user.username} entered invalid password on session revoke",
            "sessions:revoke_failed",
        )
        raise merrors.PermissionDeniedException("Incorrect password", 401, 401)

    # Rotate auth_key — immediately invalidates every other JWT
    user.auth_key = uuid.uuid4().hex
    user.save(update_fields=["auth_key", "modified"])

    user.report_incident(f"{user.username} revoked all sessions", "sessions:revoked")

    # Issue fresh JWT signed with the new key
    return jwt_login(request, user)


# -----------------------------------------------------------------
# Account deactivation
# -----------------------------------------------------------------

@md.POST("account/deactivate")
@md.requires_auth()
@md.rate_limit("account_deactivate", ip_limit=5, ip_window=300)
def on_account_deactivate(request):
    """
    Step 1: Send a confirmation email with a short-lived dv: token.
    The account is NOT deactivated until the token is confirmed.
    """
    if not settings.get("ALLOW_SELF_DEACTIVATION", True):
        raise merrors.PermissionDeniedException("Account deactivation is not allowed")

    user = request.user
    token = tokens.generate_deactivate_token(user)

    try:
        user.send_template_email("account_deactivate_confirm", {"token": token})
    except Exception:
        pass

    user.report_incident(f"{user.username} requested account deactivation", "account:deactivate_requested")

    return JsonResponse({
        "status": True,
        "message": "A confirmation email has been sent. Follow the link to complete deactivation.",
    })


@md.POST("account/deactivate/confirm")
@md.requires_params("token")
@md.public_endpoint()
def on_account_deactivate_confirm(request):
    """
    Step 2: Validate the dv: token and call pii_anonymize().
    Public endpoint — the token is the credential.
    """
    raw_token = request.DATA.get("token", "")
    user = tokens.verify_deactivate_token(raw_token)

    if not user.is_active:
        return JsonResponse({"status": True, "message": "Your account has been deactivated."})

    # Log BEFORE anonymisation so username is still readable
    user.report_incident(f"{user.username} account deactivated", "account:deactivated", uid=user.pk)

    user.pii_anonymize()

    return JsonResponse({"status": True, "message": "Your account has been deactivated."})


# -----------------------------------------------------------------
# Security events log
# -----------------------------------------------------------------

_SECURITY_CATEGORY_PREFIXES = [
    "login",
    "invalid_password",
    "password_reset",
    "totp:",
    "email_change:",
    "email_verify:",
    "phone_change:",
    "phone_verify:",
    "username:",
    "oauth",
    "passkey:",
    "account:deactivat",
    "sessions:",
    "api_key:",
    "magic_login",
]


@md.GET("account/security-events")
@md.requires_auth()
def on_account_security_events(request):
    """
    Return auth-relevant audit events for the authenticated user.
    Scoped unconditionally to request.user — no cross-user access.
    Uses the Event model's 'security' graph for serialization and the
    framework's built-in date-range filtering, sorting, and pagination.
    """
    from django.db.models import Q
    from mojo.apps.incident.models.event import Event

    # Cap page size at 100
    raw_size = request.DATA.get("size", 25)
    capped = min(int(raw_size or 25), 100)
    request.DATA["size"] = capped

    # Force the restricted security graph and default sort
    request.DATA["graph"] = "security"
    if not request.DATA.get("sort"):
        request.DATA["sort"] = "-created"

    # Build category filter scoped to the authenticated user
    q = Q()
    for prefix in _SECURITY_CATEGORY_PREFIXES:
        q |= Q(category__startswith=prefix)

    qs = Event.objects.filter(q, uid=request.user.pk)

    # Delegate to framework — handles date range, sorting, pagination, serialization
    return Event.on_rest_list(request, qs)
