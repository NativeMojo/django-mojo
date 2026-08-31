"""
Truthful SMS reporting for the phone-change request endpoint (#3411).

`POST /api/auth/phone/change/request` used to answer the SMS transport with
`if sms and sms.status == "failed"` — a guard that is wrong three ways:

  - a `None` result (the transport raised, or returned nothing) is falsy, so
    the endpoint returned its normal 200 with a `session_token` for a change
    whose OTP nobody ever received, AND texted the OLD number that a change
    was under way;
  - a transport EXCEPTION was never caught at all, so it escaped as a 500;
  - a persisted `status="failed"` row was blamed on the person's number with
    "check the number and try again", even when the real cause was a
    half-configured provider on this deployment.

Contract this file enforces, mirroring `on_phone_verify_send` (#3368):

  - a transport exception answers 503 with the fixed safe-retry body
  - an unaccepted send (None, or a persisted failed row) answers the same 503
  - a sanctioned recipient rejection answers 400 with the fixed copy, and no
    provider text or error code reaches the client
  - EVERY failure path clears the pending phone-change state (pending_phone,
    phone_change_otp, phone_change_otp_ts and the pc: JTI) so the caller
    restarts cleanly at step 1
  - no failure path texts the OLD number — an aborted change must never
    alert the previous owner
  - the accepted path is unchanged: 200, session_token, pending_phone stored,
    old-number notification sent, `phone_change:requested` incident filed

Driven in-process through the view's keyword-only ``send=`` seam with a
RequestFactory request: the test project's +1555 short-circuit means a send
over the wire is ALWAYS accepted, so the failure branches are unreachable over
HTTP. No mock.patch and no attribute assignment on production modules — this
package is default_core and its cold_budget must not grow.
"""
from testit import helpers as th
from testit.helpers import assert_eq, assert_true, assert_in

TESTIT_TIER = "extended"

PC_USER = "pc_smsdel_user"
PC_PWORD = "pcsmsdel##mojo99"
PC_PHONE = "+15550005511"
PC_NEW_PHONE = "+15550005522"

SECRET_KEYS = ("pending_phone", "phone_change_otp", "phone_change_otp_ts",
               "phone_change_jti")


@th.django_unit_setup()
def setup_phone_change_sms_delivery(opts):
    from mojo.apps.account.models import User
    from mojo.apps.incident.models import Event

    # Delete before creating — the suite runs against a long-lived database.
    User.objects.filter(username=PC_USER).delete()

    user = User(username=PC_USER, email=f"{PC_USER}@example.com")
    user.save()
    # Claim BOTH fixture numbers first — phone_number is unique, and the
    # endpoint refuses a target number another account already holds.
    User.objects.exclude(pk=user.pk).filter(
        phone_number__in=[PC_PHONE, PC_NEW_PHONE]).update(phone_number=None)
    user.is_active = True
    user.phone_number = PC_PHONE
    user.is_phone_verified = True
    user.requires_mfa = False
    user.save_password(PC_PWORD)
    user.save()
    for key in SECRET_KEYS:
        user.set_secret(key, None)
    user.save(update_fields=["mojo_secrets", "modified"])
    opts.pc_user_id = user.pk

    Event.objects.filter(
        model_id=user.pk, category__startswith="phone_change:").delete()


class _FakeSend:
    """Stand-in for phonehub.send_sms, injected through the view seam.

    Records what the endpoint asked to send and returns a caller-chosen result
    shape (or raises). Never touches Twilio, a PhoneConfig, or the database.
    """

    def __init__(self, result=None, raises=None):
        self.result = result
        self.raises = raises
        self.calls = []

    def __call__(self, phone_number, message, **kwargs):
        self.calls.append(dict(phone_number=phone_number, message=message))
        if self.raises is not None:
            raise self.raises
        return self.result


def _sms(status, error_code=None, error_message=None):
    """An UNSAVED phonehub SMS — the real result shape, no DB write, no provider."""
    from mojo.apps.phonehub.models import SMS
    return SMS(
        direction="outbound",
        from_number="+15550000000",
        to_number=PC_NEW_PHONE,
        body="Your phone change verification code is: 000000",
        status=status,
        error_code=error_code,
        error_message=error_message,
    )


def _call_change_request(opts, send):
    """Drive the real (decorated) view in-process with an injected sender."""
    from django.test import RequestFactory
    from objict import objict
    from mojo.apps.account.models import User
    from mojo.apps.account.rest import user as user_rest
    from mojo.decorators.limits import clear_rate_limits

    # The endpoint allows 5 requests / 3600s per IP — every call clears first.
    clear_rate_limits(ip="127.0.0.1")
    User.objects.filter(pk=opts.pc_user_id).update(
        is_active=True, phone_number=PC_PHONE)
    _clear_phone_change_secrets(opts)

    factory = RequestFactory(REMOTE_ADDR="127.0.0.1")
    request = factory.post("/api/auth/phone/change/request", {})
    request.DATA = objict(phone_number=PC_NEW_PHONE)
    # Mirror the attributes MojoMiddleware stamps — the incident reporter and
    # the throttle blocker read them directly, not through getattr.
    request.ip = "127.0.0.1"
    request.bearer = None
    request.group = None
    request.duid = None
    request.user_agent = "testit"
    request.user = User.objects.get(pk=opts.pc_user_id)
    return user_rest.on_phone_change_request(request, send=send)


def _clear_phone_change_secrets(opts):
    from mojo.apps.account.models import User

    user = User.objects.get(pk=opts.pc_user_id)
    for key in SECRET_KEYS:
        user.set_secret(key, None)
    user.save(update_fields=["mojo_secrets", "modified"])


def _assert_state_cleared(opts, context):
    from mojo.apps.account.models import User

    user = User.objects.get(pk=opts.pc_user_id)
    for key in SECRET_KEYS:
        assert_eq(user.get_secret(key), None,
                  f"{context}: {key} must be cleared so the caller can restart "
                  f"at step 1 — a surviving pending change lets a dead "
                  f"session_token look alive")


# ===========================================================================
# Failure paths — 503 / 400, state cleared, old number never notified
# ===========================================================================

@th.tier("bug")
@th.django_unit_test("phone/change/request: a transport exception returns 503 and clears the pending change")
def test_phone_change_transport_exception_returns_503_and_clears_state(opts):
    import json
    from mojo.apps.account.services.sms_delivery import SMS_SEND_UNAVAILABLE

    try:
        sender = _FakeSend(raises=RuntimeError("twilio socket timeout"))
        response = _call_change_request(opts, sender)
        body = json.loads(response.content.decode("utf-8"))

        assert_eq(response.status_code, 503,
                  f"a raising transport must answer the retryable 503 rather "
                  f"than escaping as a 500, got {response.status_code}: {body}")
        assert_eq(body, {"status": False, "code": 503, "error": SMS_SEND_UNAVAILABLE},
                  "the 503 body must be the fixed safe-retry payload")
        assert_true("socket timeout" not in json.dumps(body),
                    f"exception text must never reach the client: {body}")
        assert_eq(len(sender.calls), 1,
                  f"only the new-number send may be attempted — an aborted "
                  f"change must never alert the old number: {sender.calls}")
        _assert_state_cleared(opts, "transport exception")
    finally:
        _clear_phone_change_secrets(opts)


@th.tier("bug")
@th.django_unit_test("phone/change/request: an UNACCEPTED send returns 503 and clears the pending change")
def test_phone_change_unaccepted_returns_503_and_clears_state(opts):
    import json
    from mojo.apps.account.services.sms_delivery import SMS_SEND_UNAVAILABLE

    expected = {"status": False, "code": 503, "error": SMS_SEND_UNAVAILABLE}
    try:
        # --- None: the transport returned nothing ---
        sender = _FakeSend(None)
        response = _call_change_request(opts, sender)
        body = json.loads(response.content.decode("utf-8"))
        assert_eq(response.status_code, 503,
                  f"a None result must not be reported as sent, got "
                  f"{response.status_code}: {body}")
        assert_eq(body, expected, "the 503 body must be the fixed safe-retry payload")
        assert_true("session_token" not in body,
                    f"a failed send must not hand back a session_token: {body}")
        assert_eq(len(sender.calls), 1,
                  f"the old number must not be notified for a change that never "
                  f"started: {sender.calls}")
        _assert_state_cleared(opts, "None result")

        # --- persisted-but-refused SMS row (provider/config outage) ---
        sender = _FakeSend(_sms(
            "failed", error_code="config_error",
            error_message="Twilio config supplies only half a credential pair"))
        response = _call_change_request(opts, sender)
        body = json.loads(response.content.decode("utf-8"))
        assert_eq(response.status_code, 503,
                  f"a deployment configuration failure is retryable and is NOT "
                  f"the caller's number, got {response.status_code}: {body}")
        assert_eq(body, expected, "the 503 body must be the fixed safe-retry payload")
        assert_true("credential" not in json.dumps(body),
                    f"provider error text must never reach the client: {body}")
        assert_eq(len(sender.calls), 1,
                  f"the old number must not be notified: {sender.calls}")
        _assert_state_cleared(opts, "refused SMS row")
    finally:
        _clear_phone_change_secrets(opts)


@th.tier("bug")
@th.django_unit_test("phone/change/request: a recipient rejection returns 400 with the fixed copy")
def test_phone_change_recipient_rejected_returns_400_fixed_copy(opts):
    import json
    from mojo.apps.account.services.sms_delivery import SMS_NUMBER_UNREACHABLE

    provider_text = "The 'To' phone number is not currently reachable via SMS or MMS"
    try:
        sender = _FakeSend(_sms("failed", error_code="21614",
                                error_message=provider_text))
        response = _call_change_request(opts, sender)
        body = json.loads(response.content.decode("utf-8"))

        assert_eq(response.status_code, 400,
                  f"a sanctioned recipient rejection is the number itself, not "
                  f"a retryable outage — got {response.status_code}: {body}")
        assert_eq(body, {"status": False, "code": 400, "error": SMS_NUMBER_UNREACHABLE},
                  "the 400 body must be exactly the fixed number-unreachable payload")
        rendered = json.dumps(body)
        assert_true(provider_text not in rendered and "21614" not in rendered,
                    f"provider error text and codes must never reach the client: {body}")
        assert_eq(len(sender.calls), 1,
                  f"the old number must not be notified: {sender.calls}")
        _assert_state_cleared(opts, "recipient rejection")
    finally:
        _clear_phone_change_secrets(opts)


# ===========================================================================
# Guard — the accepted path is untouched
# ===========================================================================

@th.tier("bug")
@th.django_unit_test("phone/change/request: an ACCEPTED send still returns 200 with the session token")
def test_phone_change_accepted_returns_200_with_session_token(opts):
    import json
    from mojo.apps.account.models import User
    from mojo.apps.incident.models import Event

    try:
        sender = _FakeSend(_sms("sent"))
        response = _call_change_request(opts, sender)
        body = json.loads(response.content.decode("utf-8"))

        assert_eq(response.status_code, 200,
                  f"an accepted send must keep returning 200, got "
                  f"{response.status_code}: {body}")
        assert_eq(body.get("status"), True, f"status must be True on success: {body}")
        assert_true(bool(body.get("session_token")),
                    f"the caller needs the session_token for step 2: {body}")

        user = User.objects.get(pk=opts.pc_user_id)
        assert_eq(user.get_secret("pending_phone"), PC_NEW_PHONE,
                  "the normalized target number must be stored as pending_phone")
        otp = user.get_secret("phone_change_otp")
        assert_true(otp is not None and len(otp) == 6 and otp.isdigit(),
                    f"a fresh 6-digit OTP must be stored in secrets, got {otp!r}")

        assert_eq(len(sender.calls), 2,
                  f"an accepted change texts the NEW number then notifies the "
                  f"OLD one: {sender.calls}")
        assert_eq(sender.calls[0]["phone_number"], PC_NEW_PHONE,
                  f"the OTP goes to the new number: {sender.calls[0]}")
        assert_in(otp, sender.calls[0]["message"],
                  "the first SMS body must carry the stored OTP")
        assert_eq(sender.calls[1]["phone_number"], PC_PHONE,
                  f"the notification goes to the old number: {sender.calls[1]}")

        assert_true(
            Event.objects.filter(model_id=opts.pc_user_id,
                                 category="phone_change:requested").exists(),
            "an accepted send must still record the phone_change:requested incident")
    finally:
        _clear_phone_change_secrets(opts)
