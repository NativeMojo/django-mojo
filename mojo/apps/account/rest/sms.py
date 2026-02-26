"""
SMS OTP REST endpoints.

Uses the existing phonehub Twilio service to send codes.
Codes stored in mojo_secrets on the User (same pattern as password reset).

2FA flow (after password login):
  POST /api/auth/sms/send    -> send SMS code, requires mfa_token
  POST /api/auth/sms/verify  -> verify code + mfa_token, issue JWT

Standalone login flow (no password):
  POST /api/auth/sms/login   -> send SMS code to username's phone
  POST /api/auth/sms/verify  -> verify code + username, issue JWT
"""
from django.db.models import Q

from mojo import decorators as md
from mojo import errors as merrors
from mojo.apps.account.models import User
from mojo.apps.account.rest.user import jwt_login
from mojo.apps.account.services import mfa as mfa_service
from mojo.apps.phonehub.services.twilio import send_sms
from mojo.helpers import crypto, dates, logit
from mojo.helpers.response import JsonResponse
from mojo.helpers.settings import settings

SMS_OTP_TTL = settings.get("SMS_OTP_TTL", 600)  # 10 minutes


def _send_otp(user):
    """Generate a 6-digit code, store it on the user, and send via SMS."""
    if not user.phone_number:
        raise merrors.ValueException("No phone number on file for this account")

    code = crypto.random_string(6, allow_digits=True, allow_chars=False, allow_special=False)
    user.set_secret("sms_otp_code", code)
    user.set_secret("sms_otp_ts", int(dates.utcnow().timestamp()))
    user.save()

    try:
        result = send_sms(body=f"Your verification code is: {code}", to_number=user.phone_number)
        if not result.sent:
            logit.error("account.sms", f"Failed to send SMS OTP to {user.phone_number}: {result.error}")
            user.report_incident(f"SMS OTP send failed: {result.error}", "sms:send_failed", level=6)
    except Exception as e:
        logit.error("account.sms", f"SMS OTP exception for {user.phone_number}: {e}")
        user.report_incident(f"SMS OTP send exception: {e}", "sms:send_failed", level=6)


def _verify_otp(user, code):
    """Verify the submitted code against what's stored. Returns True/False."""
    stored_code = user.get_secret("sms_otp_code")
    stored_ts = int(user.get_secret("sms_otp_ts") or 0)
    now_ts = int(dates.utcnow().timestamp())

    if not stored_code or now_ts - stored_ts > SMS_OTP_TTL:
        return False
    return code == stored_code


def _clear_otp(user):
    user.set_secret("sms_otp_code", None)
    user.set_secret("sms_otp_ts", None)
    user.save()


# -----------------------------------------------------------------
# 2FA flow (after password login, uses mfa_token)
# -----------------------------------------------------------------

@md.POST("auth/sms/send")
@md.requires_params("mfa_token")
@md.public_endpoint()
def on_sms_send(request):
    """Send an SMS OTP code to the user identified by mfa_token."""
    token_data = mfa_service.consume_mfa_token(request.DATA.get("mfa_token"))
    if not token_data:
        raise merrors.PermissionDeniedException("Invalid or expired MFA token", 401, 401)

    user = User.objects.filter(pk=token_data["uid"]).first()
    if not user:
        raise merrors.PermissionDeniedException()

    # Re-issue a fresh mfa_token so the user can still verify after this send
    new_token = mfa_service.create_mfa_token(user, token_data.get("methods", ["sms"]))
    _send_otp(user)

    return JsonResponse({
        "status": True,
        "data": {
            "mfa_token": new_token,
            "expires_in": mfa_service.MFA_TOKEN_TTL,
        },
    })


@md.POST("auth/sms/verify")
@md.requires_params("code")
@md.public_endpoint()
def on_sms_verify(request):
    """
    Verify an SMS OTP code.

    Accepts either:
      - mfa_token + code  (2FA step after password login)
      - username + code   (standalone login)
    """
    mfa_token = request.DATA.get("mfa_token")
    username = request.DATA.get("username", "").lower().strip()
    code = request.DATA.get("code", "").strip()

    if mfa_token:
        token_data = mfa_service.consume_mfa_token(mfa_token)
        if not token_data:
            raise merrors.PermissionDeniedException("Invalid or expired MFA token", 401, 401)
        user = User.objects.filter(pk=token_data["uid"]).first()
    elif username:
        user = User.objects.filter(Q(username=username) | Q(email=username)).first()
    else:
        raise merrors.ValueException("Provide either mfa_token or username")

    if not user:
        raise merrors.PermissionDeniedException()

    if not _verify_otp(user, code):
        user.report_incident("Invalid SMS OTP code", "sms:otp_failed")
        raise merrors.PermissionDeniedException("Invalid or expired code", 401, 401)

    _clear_otp(user)
    return jwt_login(request, user)


# -----------------------------------------------------------------
# Standalone login (no password — send code then verify)
# -----------------------------------------------------------------

@md.POST("auth/sms/login")
@md.requires_params("username")
@md.public_endpoint()
def on_sms_login(request):
    """Send an SMS OTP to start a passwordless login."""
    username = request.DATA.get("username", "").lower().strip()
    user = User.objects.filter(Q(username=username) | Q(email=username)).first()

    if not user:
        User.class_report_incident(
            f"SMS login attempt with unknown username: {username}",
            event_type="sms:login_unknown",
            level=8,
            request=request,
        )
        # Return success to avoid user enumeration
        return JsonResponse({"status": True, "message": "If the account exists, a code was sent."})

    _send_otp(user)
    return JsonResponse({"status": True, "message": "If the account exists, a code was sent."})
