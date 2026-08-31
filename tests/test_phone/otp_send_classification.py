"""
SMS OTP send classification and the /auth/sms/* enumeration + throttle
contracts (#3411).

Three defects in `mojo/apps/account/rest/sms.py`, all exercised here:

1. `_send_otp` guarded the transport with `sms.status == "failed"`. A `None`
   result (transport returned nothing) raised `AttributeError` INSIDE the
   try, so the failure was mis-filed as `SMS OTP send exception: ...`; and a
   non-terminal state such as `queued` filed nothing at all, so a send that
   never happened looked clean in the incident trail.

2. `/auth/phone/register/start` returned 200 with a `session_token` no matter
   what the transport did — a caller then waits for a code that was never
   sent. This endpoint is user-less and Redis-backed with the
   account-existence check deliberately omitted, so it has NO enumeration
   surface: the fixed 503/400 bodies are identical for registered and
   unregistered numbers.

3. `/auth/sms/login` promises a uniform response to prevent account
   enumeration, but `_send_otp` raised `ValueException("No phone number on
   file for this account")` for a real account with no phone number, while an
   unknown identifier got the generic 200. That is a trivially usable oracle
   and it contradicts the documented invariant in
   docs/django_developer/account/auth_pages.md.

Plus the rate-limit binding: `@md.strict_rate_limit(10, 60)` bound
POSITIONALLY as `key=10, ip_limit=60`, so /auth/sms/send, /auth/sms/verify and
/auth/sms/login all shared ONE bucket (`srl:10:ip:<ip>`) at 60 requests a
minute — and /auth/sms/login sends a real SMS per unauthenticated request.
Each endpoint now has its own named bucket at 10/60s.

Driven in-process through keyword-only ``send=`` seams with RequestFactory
requests. No mock.patch and no attribute assignment on production modules —
this package is default_core and its cold_budget must not grow.
"""
from testit import helpers as th
from testit.helpers import assert_eq, assert_true

TESTIT_TIER = "extended"

OTP_USER = "otpcls_user"
OTP_PWORD = "otpcls##mojo99"
OTP_PHONE = "+15550006611"
NOPHONE_USER = "otpcls_nophone_user"
REG_PHONE = "+15550006622"
UNKNOWN_IDENTIFIER = "ghost_otpcls_xyz"

GENERIC_MESSAGE = "If the account exists, a code was sent."

# A loopback identity nothing else in the suite uses — the throttle test
# deliberately exhausts a bucket, and must never spend 127.0.0.1's budget.
RL_IP = "127.0.0.77"


@th.django_unit_setup()
def setup_otp_send_classification(opts):
    from mojo.apps.account.models import User
    from mojo.apps.incident.models import Event
    from mojo.decorators.limits import clear_rate_limits

    # Delete before creating — the suite runs against a long-lived database.
    User.objects.filter(username__in=[OTP_USER, NOPHONE_USER]).delete()
    User.objects.filter(username=UNKNOWN_IDENTIFIER).delete()

    user = User(username=OTP_USER, email=f"{OTP_USER}@example.com")
    user.save()
    # Claim the fixture number FIRST — phone_number is unique.
    User.objects.exclude(pk=user.pk).filter(phone_number=OTP_PHONE).update(
        phone_number=None)
    user.is_active = True
    user.phone_number = OTP_PHONE
    user.is_phone_verified = True
    user.requires_mfa = False
    user.save_password(OTP_PWORD)
    user.save()
    opts.otp_user_id = user.pk

    nophone = User(username=NOPHONE_USER, email=f"{NOPHONE_USER}@example.com")
    nophone.save()
    nophone.is_active = True
    nophone.phone_number = None
    nophone.is_email_verified = True
    nophone.save_password(OTP_PWORD)
    nophone.save()
    opts.nophone_user_id = nophone.pk

    Event.objects.filter(model_id__in=[user.pk, nophone.pk],
                         category__startswith="sms:").delete()

    # This module owns RL_IP outright — start it clean.
    clear_rate_limits(ip=RL_IP, key="sms_login")
    clear_rate_limits(ip=RL_IP, key="sms_verify")
    clear_rate_limits(ip=RL_IP, key="10")


class _FakeSend:
    """Stand-in for phonehub.send_sms, injected through a view/service seam."""

    def __init__(self, result=None, raises=None):
        self.result = result
        self.raises = raises
        self.calls = []

    def __call__(self, phone_number, message, **kwargs):
        self.calls.append(dict(phone_number=phone_number, message=message))
        if self.raises is not None:
            raise self.raises
        return self.result


def _sms(status, to_number=OTP_PHONE, error_code=None, error_message=None):
    """An UNSAVED phonehub SMS — the real result shape, no DB write, no provider."""
    from mojo.apps.phonehub.models import SMS
    return SMS(
        direction="outbound",
        from_number="+15550000000",
        to_number=to_number,
        body="Your verification code is: 000000",
        status=status,
        error_code=error_code,
        error_message=error_message,
    )


def _request(path, data, ip="127.0.0.1"):
    """A RequestFactory POST carrying the attributes mojo middleware stamps."""
    from django.test import RequestFactory
    from objict import objict
    from mojo.middleware.mojo import ANONYMOUS_USER

    factory = RequestFactory(REMOTE_ADDR=ip)
    request = factory.post(path, {})
    request.DATA = objict(data)
    request.ip = ip
    request.user = ANONYMOUS_USER
    request.bearer = None
    request.group = None
    request.duid = None
    request.muid = None
    request.user_agent = "testit"
    return request


def _sms_failed_events(user_id):
    from mojo.apps.incident.models import Event
    return Event.objects.filter(
        model_id=user_id, category="sms:send_failed",
        details="SMS OTP send failed").count()


# ===========================================================================
# _send_otp — every unaccepted shape files the send_failed incident
# ===========================================================================

@th.tier("bug")
@th.django_unit_test("_send_otp: every unaccepted send files the sms:send_failed incident, none raise")
def test_send_otp_unaccepted_files_the_send_failed_incident(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.rest import sms as sms_rest

    shapes = (
        ("None", None),
        ("failed row", _sms("failed", error_code="config_error",
                            error_message="Twilio config supplies only half a credential pair")),
        ("queued row", _sms("queued")),
    )
    for label, result in shapes:
        before = _sms_failed_events(opts.otp_user_id)
        user = User.objects.get(pk=opts.otp_user_id)
        sender = _FakeSend(result)
        sms_rest._send_otp(user, None, send=sender)

        assert_eq(len(sender.calls), 1,
                  f"{label}: the send must be attempted exactly once: {sender.calls}")
        assert_eq(_sms_failed_events(opts.otp_user_id), before + 1,
                  f"{label}: an unaccepted send must file exactly one "
                  f"'SMS OTP send failed' incident — a None result must not be "
                  f"mis-filed as a transport exception, and a non-terminal "
                  f"state must not pass silently")

    user = User.objects.get(pk=opts.otp_user_id)
    user.set_secret("sms_otp_code", None)
    user.set_secret("sms_otp_ts", None)
    user.save()


# ===========================================================================
# /auth/phone/register/start — truthful failure reporting
# ===========================================================================

def _call_register_start(send, ip="127.0.0.1"):
    from mojo.apps.account.rest import sms as sms_rest
    from mojo.decorators.limits import clear_rate_limits

    # The endpoint allows 5 requests / 300s per IP — every call clears first.
    clear_rate_limits(ip=ip)
    request = _request("/api/auth/phone/register/start",
                       dict(phone=REG_PHONE), ip=ip)
    return sms_rest.on_phone_register_start(request, send=send)


@th.tier("bug")
@th.django_unit_test("phone/register/start: an UNACCEPTED send returns 503 and hands back no session_token")
def test_phone_register_start_unaccepted_returns_503(opts):
    import json
    from mojo.apps.account.services.sms_delivery import SMS_SEND_UNAVAILABLE

    expected = {"status": False, "code": 503, "error": SMS_SEND_UNAVAILABLE}
    cases = (
        ("None", _FakeSend(None)),
        ("failed row", _FakeSend(_sms("failed", to_number=REG_PHONE,
                                      error_code="config_error",
                                      error_message="Twilio credentials missing"))),
        ("raising transport", _FakeSend(raises=RuntimeError("twilio socket timeout"))),
    )
    for label, sender in cases:
        response = _call_register_start(sender)
        body = json.loads(response.content.decode("utf-8"))
        assert_eq(response.status_code, 503,
                  f"{label}: a caller must not be told to wait for a code that "
                  f"was never sent, got {response.status_code}: {body}")
        assert_eq(body, expected,
                  f"{label}: the 503 body must be the fixed safe-retry payload")
        rendered = json.dumps(body)
        assert_true("session_token" not in rendered,
                    f"{label}: a failed start must not hand back a session "
                    f"token: {body}")
        assert_true("credential" not in rendered and "socket timeout" not in rendered,
                    f"{label}: provider/exception text must never reach the "
                    f"client: {body}")


@th.tier("bug")
@th.django_unit_test("phone/register/start: a recipient rejection returns 400 with the fixed copy")
def test_phone_register_start_recipient_rejected_returns_400(opts):
    import json
    from mojo.apps.account.services.sms_delivery import SMS_NUMBER_UNREACHABLE

    provider_text = "The 'To' number is not a valid phone number"
    sender = _FakeSend(_sms("failed", to_number=REG_PHONE, error_code="21211",
                            error_message=provider_text))
    response = _call_register_start(sender)
    body = json.loads(response.content.decode("utf-8"))

    assert_eq(response.status_code, 400,
              f"a sanctioned recipient rejection is the number itself, not a "
              f"retryable outage — got {response.status_code}: {body}")
    assert_eq(body, {"status": False, "code": 400, "error": SMS_NUMBER_UNREACHABLE},
              "the 400 body must be exactly the fixed number-unreachable payload")
    rendered = json.dumps(body)
    assert_true(provider_text not in rendered and "21211" not in rendered,
                f"provider error text and codes must never reach the client: {body}")


# ===========================================================================
# /auth/sms/login — one response for everybody
# ===========================================================================

def _call_sms_login(username, send=None, ip="127.0.0.1"):
    from mojo.apps.account.rest import sms as sms_rest
    from mojo.decorators.limits import clear_rate_limits

    clear_rate_limits(ip=ip, key="sms_login")
    request = _request("/api/auth/sms/login", dict(username=username), ip=ip)
    if send is None:
        return sms_rest.on_sms_login(request)
    return sms_rest.on_sms_login(request, send=send)


@th.tier("bug")
@th.django_unit_test("sms/login: an account with no phone number answers exactly like an unknown identifier")
def test_sms_login_no_phone_matches_unknown_account_response(opts):
    from mojo.apps.incident.models import Event

    Event.objects.filter(model_id=opts.nophone_user_id,
                         category="sms:login_no_phone").delete()

    no_phone = _call_sms_login(NOPHONE_USER)
    unknown = _call_sms_login(UNKNOWN_IDENTIFIER)

    assert_eq(no_phone.status_code, unknown.status_code,
              f"a real account with no phone number must answer with the same "
              f"status as an unknown identifier — got {no_phone.status_code} "
              f"vs {unknown.status_code}")
    assert_eq(no_phone.content, unknown.content,
              f"the two bodies must be byte-identical or the difference IS the "
              f"enumeration oracle — got {no_phone.content!r} vs "
              f"{unknown.content!r}")
    assert_true(GENERIC_MESSAGE in no_phone.content.decode("utf-8"),
                f"both must carry the generic copy: {no_phone.content!r}")
    assert_true(
        Event.objects.filter(model_id=opts.nophone_user_id,
                             category="sms:login_no_phone").exists(),
        "the uniform response must not cost the operator the signal — a real "
        "account with no usable number still files an incident")


@th.tier("bug")
@th.django_unit_test("sms/login: a failing send answers exactly like an unknown identifier")
def test_sms_login_response_identical_for_failing_send_and_unknown_account(opts):
    refused = _sms("failed", error_code="config_error",
                   error_message="Twilio credentials missing")
    known = _call_sms_login(OTP_USER, send=_FakeSend(refused))
    unknown = _call_sms_login(UNKNOWN_IDENTIFIER, send=_FakeSend(refused))

    assert_eq(known.status_code, unknown.status_code,
              f"a real account whose send failed must answer with the same "
              f"status as an unknown identifier — got {known.status_code} vs "
              f"{unknown.status_code}")
    assert_eq(known.content, unknown.content,
              f"the two bodies must be byte-identical — got {known.content!r} "
              f"vs {unknown.content!r}")


# ===========================================================================
# Rate limiting — one named bucket per endpoint
# ===========================================================================

@th.tier("bug")
@th.django_unit_test("sms auth endpoints: /auth/sms/login and /auth/sms/verify have independent rate buckets")
def test_sms_auth_endpoints_have_independent_rate_buckets(opts):
    from mojo import errors as merrors
    from mojo.apps.account.rest import sms as sms_rest
    from mojo.decorators.limits import clear_rate_limits

    clear_rate_limits(ip=RL_IP, key="sms_login")
    clear_rate_limits(ip=RL_IP, key="sms_verify")
    clear_rate_limits(ip=RL_IP, key="10")

    # Spend the sms_login budget from an identity nothing else shares.
    for attempt in range(10):
        request = _request("/api/auth/sms/login",
                           dict(username=UNKNOWN_IDENTIFIER), ip=RL_IP)
        response = sms_rest.on_sms_login(request)
        assert_eq(response.status_code, 200,
                  f"request {attempt + 1} must be inside the 10/minute budget, "
                  f"got {response.status_code}")

    request = _request("/api/auth/sms/login",
                       dict(username=UNKNOWN_IDENTIFIER), ip=RL_IP)
    blocked = sms_rest.on_sms_login(request)
    assert_eq(blocked.status_code, 429,
              f"the 11th /auth/sms/login in a minute must be throttled — this "
              f"endpoint sends a real SMS per unauthenticated request, got "
              f"{blocked.status_code}")

    # The verify endpoint must still be reachable: it is a DIFFERENT bucket.
    verify_request = _request("/api/auth/sms/verify",
                              dict(username=UNKNOWN_IDENTIFIER, code="000000"),
                              ip=RL_IP)
    reached_handler = False
    verify_response = None
    try:
        verify_response = sms_rest.on_sms_verify(verify_request)
    except merrors.PermissionDeniedException:
        # The handler ran and rejected an unknown account — exactly what we
        # want to observe: the request was NOT throttled on the way in.
        reached_handler = True

    assert_true(
        reached_handler,
        f"/auth/sms/verify must not be throttled by /auth/sms/login's budget — "
        f"a shared bucket lets code-request spam lock a legitimate user out of "
        f"submitting their code, got "
        f"{getattr(verify_response, 'status_code', None)}")

    clear_rate_limits(ip=RL_IP, key="sms_login")
    clear_rate_limits(ip=RL_IP, key="sms_verify")
