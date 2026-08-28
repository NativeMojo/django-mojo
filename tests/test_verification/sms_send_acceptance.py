"""
Tests for mojo.apps.account.services.sms_delivery — did the SMS transport
actually accept the message? (#3368, the SMS twin of #3253)

Contract this file enforces:
  - was_accepted() is False for None (transport raised, or returned nothing)
  - was_accepted() is False for a persisted-but-refused SMS row
    (status="failed", provider text in error_message)
  - was_accepted() is False for every state outside ACCEPTED_STATES — the
    predicate fails CLOSED on queued/sending/undelivered/received/unknown
  - was_accepted() is True for sent/delivered — with NO provider_message_id
    requirement (deliberate deviation from email_delivery: the mojo remote
    provider can accept a send and return no id)
  - recipient_rejected() is True only for a failed row carrying one of the
    sanctioned Twilio recipient codes (21211/21610/21614), str or int; every
    unknown or absent code fails toward the retryable 503
  - send_unavailable_response() is a 503 carrying exactly the fixed safe-retry
    body — provider error text never reaches the client
  - POST /api/auth/verify/phone/send answers 503 for an unaccepted send,
    400 with fixed copy for a recipient rejection, and its normal 200 success
    when the transport accepted

The endpoint failure paths are driven in-process through the view's
keyword-only ``send=`` seam with a RequestFactory request: the test project's
+1555 short-circuit means a send over ``opts.client`` is ALWAYS accepted, so
the failure branches are not reachable over the wire. No mock.patch and no
attribute assignment on production modules — this package is default_core and
its cold_budget must not grow. The one wire test locks the accepted path end
to end, persisted SMS row included.
"""
from testit import helpers as th
from testit.helpers import assert_eq, assert_true, assert_in

TESTIT_TIER = "extended"

SMS_USER = "sms_sendaccept_user"
SMS_PWORD = "smssendaccept##mojo99"
TEST_PHONE = "+15550004433"
FAKE_PROVIDER_ID = "SMsms-sendacceptance-test"


@th.django_unit_setup()
def setup_sms_send_acceptance(opts):
    from mojo.apps.account.models import User
    from mojo.apps.incident.models import Event
    from mojo.apps.phonehub.models import SMS

    # Delete before creating — the suite runs against a long-lived database.
    User.objects.filter(username=SMS_USER).delete()

    user = User(username=SMS_USER, email=f"{SMS_USER}@example.com")
    user.save()
    # Claim the fixture number FIRST — phone_number is unique, so a stale row
    # left by another module would make our save fail (verification.py
    # precedent).
    User.objects.exclude(pk=user.pk).filter(phone_number=TEST_PHONE).update(phone_number=None)
    user.is_active = True
    user.phone_number = TEST_PHONE
    user.is_email_verified = False
    user.is_phone_verified = False
    user.requires_mfa = False
    user.save_password(SMS_PWORD)
    user.save()
    opts.sms_user_id = user.pk

    # SMS rows are matched by the fixture number and events by this user's pk —
    # clear anything a previous run left behind.
    SMS.objects.filter(to_number=TEST_PHONE).delete()
    Event.objects.filter(model_id=user.pk, category__startswith="phone_verify:").delete()


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


def _sms(status, error_code=None, error_message=None, provider_message_id=None):
    """An UNSAVED phonehub SMS — the real result shape, no DB write, no provider."""
    from mojo.apps.phonehub.models import SMS
    return SMS(
        direction="outbound",
        from_number="+15550000000",
        to_number=TEST_PHONE,
        body="Your verification code is: 000000",
        status=status,
        error_code=error_code,
        error_message=error_message,
        provider_message_id=provider_message_id,
    )


def _post_request():
    """A RequestFactory POST carrying the attributes mojo middleware adds."""
    from django.test import RequestFactory
    from objict import objict

    factory = RequestFactory(REMOTE_ADDR="127.0.0.1")
    request = factory.post("/api/auth/verify/phone/send", {})
    request.DATA = objict()
    return request


def _call_send_endpoint(opts, send):
    """Drive the real (decorated) view in-process with an injected sender."""
    from mojo.apps.account.models import User
    from mojo.apps.account.rest import verify
    from mojo.decorators.limits import clear_rate_limits

    clear_rate_limits(ip="127.0.0.1")
    User.objects.filter(pk=opts.sms_user_id).update(
        is_phone_verified=False, is_active=True, phone_number=TEST_PHONE)
    request = _post_request()
    request.user = User.objects.get(pk=opts.sms_user_id)
    return verify.on_phone_verify_send(request, send=send)


def _clear_phone_verify_secrets(opts):
    from mojo.apps.account.models import User

    user = User.objects.get(pk=opts.sms_user_id)
    user.set_secret("phone_verify_code", None)
    user.set_secret("phone_verify_ts", None)
    user.save(update_fields=["mojo_secrets", "modified"])


# ===========================================================================
# was_accepted() — the SMS acceptance predicate
# ===========================================================================

@th.tier("bug")
@th.django_unit_test("sms_delivery: was_accepted(None) is False — transport raised or returned nothing")
def test_sms_was_accepted_rejects_none(opts):
    from mojo.apps.account.services.sms_delivery import was_accepted

    assert_eq(was_accepted(None), False,
              "None is what the endpoint substitutes when the transport raised "
              "— never an accepted send")


@th.tier("bug")
@th.django_unit_test("sms_delivery: was_accepted(failed SMS) is False — a refused send is persisted, not accepted")
def test_sms_was_accepted_rejects_failed_sms(opts):
    from mojo.apps.account.services.sms_delivery import was_accepted

    refused = _sms(
        "failed",
        error_code="config_error",
        error_message="Twilio config supplies only half a credential pair — set "
                      "both twilio_account_sid and twilio_auth_token, or neither",
    )
    assert_eq(was_accepted(refused), False,
              "SMS.send persists and RETURNS a failed row when the provider "
              "refuses — a truthy result is not proof of acceptance")


@th.django_unit_test("sms_delivery: was_accepted() is False for every state outside ACCEPTED_STATES")
def test_sms_was_accepted_rejects_nonterminal_states(opts):
    from mojo.apps.account.services.sms_delivery import was_accepted

    for status in ("queued", "sending", "undelivered", "received", "unknown"):
        assert_eq(was_accepted(_sms(status, provider_message_id=FAKE_PROVIDER_ID)), False,
                  f"status={status!r} is not an accepted state, even with a "
                  f"provider id — the predicate must fail closed on shapes it "
                  f"does not recognise")


@th.tier("bug")
@th.django_unit_test("sms_delivery: was_accepted() is True for sent/delivered — no provider id required")
def test_sms_was_accepted_accepts_sent_and_delivered(opts):
    from mojo.apps.account.services.sms_delivery import ACCEPTED_STATES, was_accepted

    assert_eq(sorted(ACCEPTED_STATES), ["delivered", "sent"],
              "ACCEPTED_STATES is the cross-item contract — changing it changes "
              "what every caller reports as sent")
    for status in ACCEPTED_STATES:
        assert_eq(was_accepted(_sms(status)), True,
                  f"status={status!r} is transport custody even with NO "
                  f"provider_message_id — the mojo remote provider can accept "
                  f"a send and return no id (deliberate email_delivery deviation)")


# ===========================================================================
# recipient_rejected() — the sanctioned bad-number allowlist
# ===========================================================================

@th.tier("bug")
@th.django_unit_test("sms_delivery: recipient_rejected() classifies only the sanctioned Twilio codes on failed rows")
def test_sms_recipient_rejected_classifies_numeric_codes(opts):
    from mojo.apps.account.services.sms_delivery import recipient_rejected

    for code in ("21211", "21610", "21614", 21211, 21610, 21614):
        assert_eq(recipient_rejected(_sms("failed", error_code=code)), True,
                  f"a failed row with Twilio code {code!r} indicts the "
                  f"recipient number — the caller may honestly say so")
    for code in ("config_error", "timeout", None):
        assert_eq(recipient_rejected(_sms("failed", error_code=code)), False,
                  f"a failed row with code {code!r} is NOT a recipient "
                  f"rejection — unknown codes must fail toward the retryable 503")
    assert_eq(recipient_rejected(_sms("sent", error_code="21614")), False,
              "a sent row is never a recipient rejection, whatever code it carries")
    assert_eq(recipient_rejected(None), False,
              "None is never a recipient rejection — it is a retryable failure")


# ===========================================================================
# send_unavailable_response() — the fixed safe-retry payload
# ===========================================================================

@th.tier("bug")
@th.django_unit_test("sms_delivery: send_unavailable_response() is a 503 with exactly the fixed body")
def test_sms_send_unavailable_response_shape(opts):
    import json
    from mojo.apps.account.services.sms_delivery import (
        SMS_SEND_UNAVAILABLE, send_unavailable_response)

    response = send_unavailable_response()
    assert_eq(response.status_code, 503,
              "an unaccepted send is a retryable server-side condition")
    body = json.loads(response.content.decode("utf-8"))
    assert_eq(body, {"status": False, "code": 503, "error": SMS_SEND_UNAVAILABLE},
              "the body must be exactly the fixed safe-retry payload")
    assert_true("Twilio" not in body["error"] and "credential" not in body["error"],
                f"provider error text must never reach the client: {body['error']!r}")
    assert_true("try again" in body["error"].lower(),
                f"the copy must tell the person to retry: {body['error']!r}")


# ===========================================================================
# POST /api/auth/verify/phone/send — in-process, through the send= seam
# ===========================================================================

@th.tier("bug")
@th.django_unit_test("verify/phone/send: an UNACCEPTED send returns 503, keeps the stored code, files no sent event")
def test_phone_send_unaccepted_returns_503(opts):
    import json
    from mojo.apps.account.models import User
    from mojo.apps.incident.models import Event
    from mojo.apps.account.services.sms_delivery import SMS_SEND_UNAVAILABLE

    expected = {"status": False, "code": 503, "error": SMS_SEND_UNAVAILABLE}
    try:
        # --- None: the transport returned nothing ---
        response = _call_send_endpoint(opts, _FakeSend(None))
        body = json.loads(response.content.decode("utf-8"))
        assert_eq(response.status_code, 503,
                  f"a None result must not be reported as sent, got {response.status_code}: {body}")
        assert_eq(body, expected, "the 503 body must be the fixed safe-retry payload")
        stored = User.objects.get(pk=opts.sms_user_id).get_secret("phone_verify_code")
        assert_true(
            stored is not None and len(stored) == 6 and stored.isdigit(),
            f"code generation is unchanged by the failure — an unconditional 503 "
            f"stub must not pass this test, got {stored!r}")

        # --- persisted-but-refused SMS row (provider/config outage) ---
        refused = _sms("failed", error_code="config_error",
                       error_message="Twilio config supplies only half a credential pair")
        response = _call_send_endpoint(opts, _FakeSend(refused))
        body = json.loads(response.content.decode("utf-8"))
        assert_eq(response.status_code, 503,
                  f"a refused SMS row must not be reported as sent, got "
                  f"{response.status_code}: {body}")
        assert_eq(body, expected, "the 503 body must be the fixed safe-retry payload")
        assert_true("credential" not in json.dumps(body),
                    f"provider error text must never reach the client: {body}")
        assert_true(
            not Event.objects.filter(
                model_id=opts.sms_user_id, category="phone_verify:sent").exists(),
            "an unaccepted send must not record a phone_verify:sent incident")
    finally:
        _clear_phone_verify_secrets(opts)


@th.tier("bug")
@th.django_unit_test("verify/phone/send: a transport exception returns the same 503, never a 500")
def test_phone_send_transport_exception_returns_503(opts):
    import json
    from mojo.apps.account.services.sms_delivery import SMS_SEND_UNAVAILABLE

    try:
        response = _call_send_endpoint(
            opts, _FakeSend(raises=RuntimeError("twilio socket timeout")))
        body = json.loads(response.content.decode("utf-8"))
        assert_eq(response.status_code, 503,
                  f"a raising transport must answer the same retryable 503, "
                  f"got {response.status_code}: {body}")
        assert_eq(body, {"status": False, "code": 503, "error": SMS_SEND_UNAVAILABLE},
                  "the 503 body must be the fixed safe-retry payload")
        assert_true("socket timeout" not in json.dumps(body),
                    f"exception text must never reach the client: {body}")
    finally:
        _clear_phone_verify_secrets(opts)


@th.tier("bug")
@th.django_unit_test("verify/phone/send: a provider recipient-rejection returns 400 with the fixed copy")
def test_phone_send_recipient_rejected_returns_400(opts):
    import json
    from mojo.apps.incident.models import Event
    from mojo.apps.account.services.sms_delivery import SMS_NUMBER_UNREACHABLE

    provider_text = "The 'To' phone number is not currently reachable via SMS or MMS"
    try:
        rejected = _sms("failed", error_code="21614", error_message=provider_text)
        response = _call_send_endpoint(opts, _FakeSend(rejected))
        body = json.loads(response.content.decode("utf-8"))
        assert_eq(response.status_code, 400,
                  f"a recipient rejection is the caller's number, not a retryable "
                  f"outage — got {response.status_code}: {body}")
        assert_eq(body, {"status": False, "code": 400, "error": SMS_NUMBER_UNREACHABLE},
                  "the 400 body must be exactly the fixed number-unreachable payload")
        assert_true(provider_text not in json.dumps(body) and "21614" not in json.dumps(body),
                    f"provider error text must never reach the client: {body}")
        assert_true(
            not Event.objects.filter(
                model_id=opts.sms_user_id, category="phone_verify:sent").exists(),
            "a rejected send must not record a phone_verify:sent incident")
    finally:
        _clear_phone_verify_secrets(opts)


@th.tier("bug")
@th.django_unit_test("verify/phone/send: an ACCEPTED send returns the 200 success response")
def test_phone_send_accepted_returns_200_in_process(opts):
    import json
    from mojo.apps.account.models import User
    from mojo.apps.incident.models import Event

    try:
        sender = _FakeSend(_sms("sent", provider_message_id=FAKE_PROVIDER_ID))
        response = _call_send_endpoint(opts, sender)
        body = json.loads(response.content.decode("utf-8"))

        assert_eq(response.status_code, 200,
                  f"an accepted send must keep returning 200, got {response.status_code}: {body}")
        assert_eq(body.get("status"), True, f"status must be True on success: {body}")
        assert_eq(body.get("message"), "Verification code sent",
                  f"the success copy must be unchanged: {body}")
        assert_eq(len(sender.calls), 1, f"exactly one send must be attempted: {sender.calls}")
        assert_eq(sender.calls[0]["phone_number"], TEST_PHONE,
                  f"the send must go to the normalized number: {sender.calls[0]}")
        stored = User.objects.get(pk=opts.sms_user_id).get_secret("phone_verify_code")
        assert_true(stored is not None and len(stored) == 6 and stored.isdigit(),
                    f"a fresh 6-digit code must be stored in secrets, got {stored!r}")
        assert_in(stored, sender.calls[0]["message"],
                  "the SMS body must carry the stored code")
        assert_true(
            Event.objects.filter(
                model_id=opts.sms_user_id, category="phone_verify:sent").exists(),
            "an accepted send must still record the phone_verify:sent incident")
    finally:
        _clear_phone_verify_secrets(opts)


# ===========================================================================
# POST /api/auth/verify/phone/send — over the wire (test-mode short-circuit)
# ===========================================================================

@th.django_unit_test("verify/phone/send: success over the wire — a persisted test-mode SMS row carries the code")
def test_phone_send_success_over_wire(opts):
    """
    The accepted path end to end: the testproject ships TWILIO_NUMBER
    ("+15550000000") and the fixture number starts with +1555, so SMS.send
    short-circuits into test mode and persists a status="sent" row without
    touching Twilio. This is a guard on the success contract, not the
    regression — it must pass before and after the fix.
    """
    from mojo.apps.account.models import User
    from mojo.apps.phonehub.models import SMS
    from mojo.decorators.limits import clear_rate_limits

    User.objects.filter(pk=opts.sms_user_id).update(
        is_phone_verified=False, is_active=True, phone_number=TEST_PHONE)
    SMS.objects.filter(to_number=TEST_PHONE).delete()
    clear_rate_limits(ip="127.0.0.1")

    try:
        opts.client.login(SMS_USER, SMS_PWORD)
        assert_true(opts.client.is_authenticated, "login failed for the SMS fixture user")

        resp = opts.client.post("/api/auth/verify/phone/send", {})
        assert_eq(resp.status_code, 200,
                  f"Expected 200 over the wire (test-mode +1555 short-circuit), "
                  f"got {resp.status_code}: {resp.response}")

        row = SMS.objects.filter(to_number=TEST_PHONE, direction="outbound").order_by("-created").first()
        assert_true(row is not None,
                    "the send must persist an outbound SMS row for the fixture number")
        assert_eq(row.status, "sent",
                  f"the test-mode short-circuit marks the row sent, got {row.status!r}")
        assert_true(row.is_test,
                    "a +1555 send in the test project must be flagged is_test")
        stored = User.objects.get(pk=opts.sms_user_id).get_secret("phone_verify_code")
        assert_true(stored is not None and stored in row.body,
                    f"the persisted SMS body must contain the stored code "
                    f"{stored!r}, got body {row.body!r}")
    finally:
        opts.client.logout()
        _clear_phone_verify_secrets(opts)
