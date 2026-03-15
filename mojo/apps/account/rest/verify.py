import mojo.decorators as md
from mojo.helpers.response import JsonResponse
from django.shortcuts import render
from mojo.apps.account.utils import tokens
from mojo import errors as merrors


def _send_realtime_event(user, event, data):
    """Fire-and-forget realtime event. Silently swallows errors — best-effort only."""
    try:
        from mojo.apps import realtime
        realtime.send_to_user("user", user.pk, {"event": event, "data": data})
    except Exception:
        pass


def _render_verify(request, template, ctx):
    """
    Render an account verify template.
    If ?redirect=<url> is present and success=True, adds a Continue button and
    auto-redirect via <meta http-equiv=refresh> after a brief delay.
    """
    redirect_url = request.GET.get("redirect", "")
    redirect_delay = 3 if ctx.get("success") else 0
    ctx["redirect_url"] = redirect_url
    ctx["redirect_delay"] = redirect_delay
    return render(request, f"account/{template}", ctx)


# ---------------------------------------------------------------------------
# Email verification
# ---------------------------------------------------------------------------

@md.POST('auth/verify/email/send')
@md.strict_rate_limit("email_verify_send", ip_limit=5, ip_window=300)
@md.requires_auth()
def on_email_verify_send(request):
    """
    Send an email verification message to the requesting user's email address.

    Optional body param:
      method: "link" (default) — send a verification link (ev: token)
      method: "code"           — send a 6-digit OTP code instead
    """
    user = request.user
    if user.is_email_verified:
        return JsonResponse(dict(status=True, message="Email is already verified"))
    method = request.DATA.get("method", "link")
    if method == "code":
        code = tokens.generate_email_verify_code(user)
        user.send_template_email("email_verify_code", context=dict(code=code))
        user.report_incident(f"{user.username} requested email verification (code)", "email_verify:sent_code")
        return JsonResponse(dict(status=True, message="Verification code sent"))
    token = tokens.generate_email_verify_token(user)
    user.send_template_email("email_verify", context=dict(token=token))
    user.report_incident(f"{user.username} requested email verification", "email_verify:sent")
    return JsonResponse(dict(status=True, message="Verification email sent"))


@md.POST('auth/verify/email/confirm')
@md.requires_auth()
@md.requires_params('code')
def on_email_verify_code_confirm(request):
    """
    Confirm email ownership by submitting the 6-digit code sent via
    POST /api/auth/verify/email/send with method=code.

    On success sets is_email_verified=True. Does not issue a new JWT —
    the user's existing session remains active.
    """
    user = request.user
    tokens.verify_email_verify_code(user, request.DATA.code)
    user.is_email_verified = True
    user.save(update_fields=["is_email_verified", "modified"])
    user.report_incident(f"{user.username} email verified (code)", "email_verify:confirmed_code")
    _send_realtime_event(user, "account:email:verified", {"email": user.email})
    return JsonResponse(dict(status=True, message="Email verified"))


@md.GET('auth/verify/email/confirm')
@md.public_endpoint()
def on_email_verify_confirm(request):
    """
    GET handler for the email verification link clicked from the user's inbox.

    Renders account/email_verify_confirm.html on success or error.
    If ?redirect=<url> is present, a Continue button and auto-redirect are shown.
    Downstream projects can override the template via TEMPLATES.DIRS.
    """
    token = request.GET.get("token", "")
    if not token:
        return _render_verify(request, "email_verify_confirm.html", {
            "success": False,
            "error_title": "Link invalid",
            "error_message": "No token was provided. Please use the link from your verification email.",
        })

    try:
        user = tokens.verify_email_verify_token(token)
    except Exception:
        return _render_verify(request, "email_verify_confirm.html", {
            "success": False,
            "error_title": "Link invalid or expired",
            "error_message": "This verification link is invalid or has already been used. Links expire after 24 hours and can only be used once.",
        })

    if not user.is_active:
        return _render_verify(request, "email_verify_confirm.html", {
            "success": False,
            "error_title": "Account disabled",
            "error_message": "This account has been disabled. Please contact support.",
        })

    user.is_email_verified = True
    user.save(update_fields=["is_email_verified", "modified"])
    user.report_incident(f"{user.username} email verified", "email_verify:confirmed")
    _send_realtime_event(user, "account:email:verified", {"email": user.email})

    return _render_verify(request, "email_verify_confirm.html", {
        "success": True,
        "email": user.email,
    })


# ---------------------------------------------------------------------------
# Phone verification
# ---------------------------------------------------------------------------

@md.POST('auth/verify/phone/send')
@md.strict_rate_limit("phone_verify_send", ip_limit=5, ip_window=300)
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
    _send_realtime_event(user, "account:phone:verified", {"phone_number": user.phone_number})
    return JsonResponse(dict(status=True, message="Phone verified"))