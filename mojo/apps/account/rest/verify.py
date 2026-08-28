import mojo.decorators as md
from mojo.helpers.response import JsonResponse
from mojo.apps.account.services import email_delivery
from mojo.apps.account.services import token_landing
from mojo.apps.account.utils import tokens
from mojo.apps.account.utils.webapp_url import build_token_url
from mojo.apps.shortlink import maybe_shorten_url
from mojo import errors as merrors


def _send_realtime_event(user, event, data):
    """Fire-and-forget realtime event. Silently swallows errors — best-effort only."""
    try:
        from mojo.apps import realtime
        realtime.send_to_user("user", user.pk, {"event": event, "data": data})
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Email verification
# ---------------------------------------------------------------------------

@md.POST('auth/verify/email/send')
@md.strict_rate_limit("email_verify_send", ip_limit=5, ip_window=300)
@md.requires_auth()
def on_email_verify_send(request, *, send=None):
    """
    Send an email verification message to the requesting user's email address.

    Optional body param:
      method: "link" (default) — send a verification link (ev: token)
      method: "code"           — send a 6-digit OTP code instead

    Answers 503 with a safe retry message when the email provider did not
    accept the message (no mailbox configured, provider refusal or outage) —
    telling someone an email is on its way when none was sent leaves them
    waiting forever. The token/code is still generated and stored: it is
    single-use and TTL-bounded, and the next request rotates it.

    `send` is a test seam, not part of the wire contract — the dispatcher never
    passes it. It exists because the test project ships no mailbox, so the
    accepted path is not reachable over HTTP.
    """
    user = request.user
    if user.is_email_verified:
        return JsonResponse(dict(status=True, message="Email is already verified"))
    if not user.email:
        raise merrors.ValueException("No email address on account")
    sender = send if send is not None else user.send_template_email
    method = request.DATA.get("method", "link")
    if method == "code":
        code = tokens.generate_email_verify_code(user)
        sent = sender("email_verify_code", context=dict(code=code))
        if not email_delivery.was_accepted(sent):
            return email_delivery.send_unavailable_response()
        user.report_incident(f"{user.username} requested email verification (code)", "email_verify:sent_code")
        return JsonResponse(dict(status=True, message="Verification code sent"))
    token = tokens.generate_email_verify_token(user)
    group = getattr(request, "group", None)
    token_url = build_token_url(
        "email_verify", token, request=request, user=user, group=group)
    token_url = maybe_shorten_url(
        token_url, source="email_verify", user=user, expire_hours=24)
    sent = sender(
        "email_verify", context=dict(token=token, token_url=token_url))
    if not email_delivery.was_accepted(sent):
        return email_delivery.send_unavailable_response()
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
@md.strict_rate_limit("email_verify_landing", ip_limit=10, ip_window=3600,
                      include_request_in_incident=False)
@md.public_endpoint("Email verification landing page — presentation only")
def on_email_verify_confirm(request):
    """
    The page an ev: link from the user's inbox opens.

    Presentation ONLY. It does not verify the token, does not consume it, does
    not touch the account, and does not name the account it belongs to — a mail
    scanner, a link preview or a browser prefetch opening this URL must leave
    the account exactly as it found it. #3257: this handler used to verify and
    commit here (reading request.GET directly, at that), which is precisely how
    a token got burned with nobody present.

    Verification happens on POST auth/email/verify/confirm — the verify-ONLY
    endpoint, deliberately not the verify-then-login one — which the page's
    button calls with the token the person actually clicked through to.

    The GET has its OWN rate bucket, separate from the POST's, so previews and
    reloads cannot eat the confirmation budget — and it opts out of
    request-stamped incident metadata so a throttled preview never files the
    token in its own query string.

    Deployments override account/email_verify_landing.html (or the shared
    account/token_landing_base.html) via TEMPLATES.DIRS.
    """
    ctx = token_landing.landing_context(
        request, token_landing.confirm_path("ev"))
    return token_landing.render_landing(request, "email_verify_landing.html", ctx)


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
