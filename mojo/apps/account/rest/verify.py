import mojo.decorators as md
from mojo.helpers.response import JsonResponse
from mojo.apps.account.models.user import User
from mojo.apps.account.utils import tokens
from mojo import errors as merrors


# ---------------------------------------------------------------------------
# Email verification
# ---------------------------------------------------------------------------

@md.POST('auth/verify/email/send')
@md.requires_auth()
def on_email_verify_send(request):
    """Send a verification link to the requesting user's email address."""
    user = request.user
    if user.is_email_verified:
        return JsonResponse(dict(status=True, message="Email is already verified"))
    token = tokens.generate_email_verify_token(user)
    user.send_template_email("email_verify", context=dict(token=token))
    user.report_incident(f"{user.username} requested email verification", "email_verify:sent")
    return JsonResponse(dict(status=True, message="Verification email sent"))


@md.GET('auth/verify/email/confirm')
@md.requires_params('token')
@md.public_endpoint()
def on_email_verify_confirm(request):
    """Confirm email ownership via the token link sent to the user's inbox."""
    user = tokens.verify_email_verify_token(request.DATA.token)
    user.is_email_verified = True
    user.save(update_fields=["is_email_verified", "modified"])
    user.report_incident(f"{user.username} email verified", "email_verify:confirmed")
    return JsonResponse(dict(status=True, message="Email verified"))


# ---------------------------------------------------------------------------
# Phone verification
# ---------------------------------------------------------------------------

@md.POST('auth/verify/phone/send')
@md.requires_auth()
def on_phone_verify_send(request):
    """Send a 6-digit SMS code to the requesting user's phone number."""
    from mojo.apps import phonehub

    user = request.user
    if user.is_phone_verified:
        return JsonResponse(dict(status=True, message="Phone is already verified"))
    if not user.phone_number:
        raise merrors.ValueException("No phone number on account")

    normalized = phonehub.normalize(user.phone_number)
    if not normalized:
        raise merrors.ValueException("Phone number is invalid")

    code = tokens.generate_phone_verify_code(user)
    sms = phonehub.send_sms(normalized, f"Your verification code is: {code}")
    if sms and sms.status == "failed":
        raise merrors.ValueException("Failed to send SMS — check your phone number")

    user.report_incident(f"{user.username} requested phone verification", "phone_verify:sent")
    return JsonResponse(dict(status=True, message="Verification code sent"))


@md.POST('auth/verify/phone/confirm')
@md.requires_auth()
@md.requires_params('code')
def on_phone_verify_confirm(request):
    """Confirm phone ownership by submitting the 6-digit code."""
    user = request.user
    tokens.verify_phone_verify_code(user, request.DATA.code)
    user.is_phone_verified = True
    user.save(update_fields=["is_phone_verified", "modified"])
    user.report_incident(f"{user.username} phone verified", "phone_verify:confirmed")
    return JsonResponse(dict(status=True, message="Phone verified"))