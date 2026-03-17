"""
TOTP REST endpoints.

Setup flow (authenticated):
  POST /api/account/totp/setup    -> generate secret, return QR code + plain secret
  POST /api/account/totp/confirm  -> verify first code, mark is_enabled=True
  DELETE /api/account/totp        -> disable TOTP

Recovery codes (authenticated):
  GET  /api/account/totp/recovery-codes           -> masked codes + remaining count
  POST /api/account/totp/recovery-codes/regenerate -> new codes (requires valid TOTP code)

Login (2FA verify step — consumes mfa_token):
  POST /api/auth/totp/verify      -> verify code + mfa_token, issue JWT

Recovery login (consumes mfa_token + recovery code):
  POST /api/auth/totp/recover     -> recovery_code + mfa_token, issue JWT

Standalone login (no password):
  POST /api/auth/totp/login       -> username + code -> JWT
"""
from django.db import transaction
from django.db.models import Q

from mojo import decorators as md
from mojo import errors as merrors
from mojo.apps.account.models import User
from mojo.apps.account.models.totp import UserTOTP
from mojo.apps.account.rest.user import jwt_login
from mojo.apps.account.services import mfa as mfa_service
from mojo.apps.account.services import totp as totp_service
from mojo.helpers import logit
from mojo.helpers.qrcode import generate_qrcode
from mojo.helpers.response import JsonResponse


# -----------------------------------------------------------------
# Setup (requires auth)
# -----------------------------------------------------------------

@md.POST("account/totp/setup")
@md.requires_auth()
def on_totp_setup(request):
    """Generate a new TOTP secret and return setup data."""
    secret = totp_service.generate_secret()
    uri = totp_service.get_provisioning_uri(secret, request.user.username)

    # Store unconfirmed secret — not yet enabled
    totp, _ = UserTOTP.objects.get_or_create(user=request.user)
    totp.set_secret("totp_secret", secret)
    totp.is_enabled = False
    totp.save()

    qr = generate_qrcode(data=uri, fmt="base64")

    return JsonResponse({
        "status": True,
        "data": {
            "secret": secret,
            "uri": uri,
            "qr_code": f"data:{qr.content_type};base64,{qr.content}",
        },
    })


@md.POST("account/totp/confirm")
@md.requires_auth()
@md.requires_params("code")
def on_totp_confirm(request):
    """Verify the first TOTP code to activate TOTP for the account."""
    totp = UserTOTP.objects.filter(user=request.user).first()
    if not totp:
        raise merrors.ValueException("TOTP setup not started. Call /api/account/totp/setup first.")

    secret = totp.get_secret("totp_secret")
    if not secret:
        raise merrors.ValueException("TOTP setup not started. Call /api/account/totp/setup first.")

    code = request.DATA.get("code", "").strip()
    if not totp_service.verify_code(secret, code):
        request.user.report_incident("Invalid TOTP confirmation code", "totp:confirm_failed")
        raise merrors.ValueException("Invalid code")

    totp.is_enabled = True
    totp.save()
    codes = totp.generate_recovery_codes()
    request.user.requires_mfa = True
    request.user.save()
    logit.info("account.totp", f"TOTP enabled for user {request.user.username}")
    return JsonResponse({"status": True, "data": {"is_enabled": True, "recovery_codes": codes}})


@md.DELETE("account/totp")
@md.requires_auth()
def on_totp_disable(request):
    """Disable TOTP for the authenticated user."""
    UserTOTP.objects.filter(user=request.user).update(is_enabled=False)
    logit.info("account.totp", f"TOTP disabled for user {request.user.username}")
    return JsonResponse({"status": True})


# -----------------------------------------------------------------
# Recovery codes (requires auth)
# -----------------------------------------------------------------

@md.GET("account/totp/recovery-codes")
@md.requires_auth()
def on_totp_recovery_codes_get(request):
    """Return masked recovery codes and remaining count."""
    totp = UserTOTP.objects.filter(user=request.user, is_enabled=True).first()
    if not totp:
        raise merrors.ValueException("TOTP is not enabled for this account")
    data = totp.get_masked_recovery_codes()
    return JsonResponse({"status": True, "data": data})


@md.POST("account/totp/recovery-codes/regenerate")
@md.requires_auth()
@md.requires_params("code")
def on_totp_recovery_codes_regenerate(request):
    """Regenerate recovery codes. Requires a valid TOTP code to authorize."""
    totp = UserTOTP.objects.filter(user=request.user, is_enabled=True).first()
    if not totp:
        raise merrors.ValueException("TOTP is not enabled for this account")
    secret = totp.get_secret("totp_secret")
    code = request.DATA.get("code", "").strip()
    if not totp_service.verify_code(secret, code):
        raise merrors.PermissionDeniedException("Invalid TOTP code", 403, 403)
    codes = totp.generate_recovery_codes()
    return JsonResponse({"status": True, "data": {"is_enabled": True, "recovery_codes": codes}})


# -----------------------------------------------------------------
# 2FA verify step (consumes mfa_token)
# -----------------------------------------------------------------

@md.POST("auth/totp/verify")
@md.requires_params("mfa_token", "code")
@md.public_endpoint()
def on_totp_verify(request):
    """
    Second factor: verify TOTP code after password login.
    Consumes the mfa_token and issues a full JWT on success.
    """
    token_data = mfa_service.consume_mfa_token(request.DATA.get("mfa_token"))
    if not token_data:
        raise merrors.PermissionDeniedException("Invalid or expired MFA token", 401, 401)

    user = User.objects.filter(pk=token_data["uid"]).first()
    if not user:
        raise merrors.PermissionDeniedException()

    totp = UserTOTP.objects.filter(user=user, is_enabled=True).first()
    if not totp:
        raise merrors.PermissionDeniedException("TOTP not enabled for this account")

    secret = totp.get_secret("totp_secret")
    code = request.DATA.get("code", "").strip()
    if not totp_service.verify_code(secret, code):
        user.report_incident("Invalid TOTP code during login", "totp:login_failed")
        raise merrors.PermissionDeniedException("Invalid code", 401, 401)

    return jwt_login(request, user)


# -----------------------------------------------------------------
# Recovery login (consumes mfa_token + recovery code)
# -----------------------------------------------------------------

@md.POST("auth/totp/recover")
@md.requires_params("mfa_token", "recovery_code")
@md.public_endpoint()
def on_totp_recover(request):
    """
    Authenticate using a recovery code instead of a TOTP code.
    Consumes the mfa_token and one recovery code, then issues a JWT.
    """
    token_data = mfa_service.consume_mfa_token(request.DATA.get("mfa_token"))
    if not token_data:
        raise merrors.PermissionDeniedException("Invalid or expired MFA token", 401, 401)

    user = User.objects.filter(pk=token_data["uid"]).first()
    if not user or not user.is_active:
        raise merrors.PermissionDeniedException()

    with transaction.atomic():
        totp = UserTOTP.objects.filter(user=user, is_enabled=True).select_for_update().first()
        if not totp:
            raise merrors.PermissionDeniedException("TOTP not enabled for this account")

        recovery_code = request.DATA.get("recovery_code", "").strip()
        if not totp.verify_and_consume_recovery_code(recovery_code):
            raise merrors.PermissionDeniedException("Invalid recovery code", 403, 403)

    user.report_incident("TOTP recovery code used", "totp:recovery_used")
    remaining = len(totp.get_secret("recovery_codes") or [])
    if remaining == 0:
        user.notify(
            "You have used your last TOTP recovery code. Generate new ones in your security settings.",
            kind="security",
        )
    return jwt_login(request, user)


# -----------------------------------------------------------------
# Standalone login (username + TOTP code, no password)
# -----------------------------------------------------------------

@md.POST("auth/totp/login")
@md.requires_params("username", "code")
@md.public_endpoint()
def on_totp_login(request):
    """Passwordless login using a TOTP code."""
    username = request.DATA.get("username", "").lower().strip()
    user = User.objects.filter(Q(username=username) | Q(email=username)).first()

    if not user:
        User.class_report_incident(
            f"TOTP login attempt with unknown username: {username}",
            event_type="totp:login_unknown",
            level=8,
            request=request,
        )
        raise merrors.PermissionDeniedException()

    totp = UserTOTP.objects.filter(user=user, is_enabled=True).first()
    if not totp:
        raise merrors.PermissionDeniedException("TOTP not enabled for this account")

    secret = totp.get_secret("totp_secret")
    code = request.DATA.get("code", "").strip()
    if not totp_service.verify_code(secret, code):
        user.report_incident("Invalid TOTP code during standalone login", "totp:login_failed")
        raise merrors.PermissionDeniedException("Invalid code", 401, 401)

    return jwt_login(request, user)
