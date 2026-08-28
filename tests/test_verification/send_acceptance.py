"""
Tests for mojo.apps.account.services.email_delivery — did the provider
actually accept the message? (#3253)

Contract this file enforces:
  - was_accepted() is False for None (no mailbox configured, or a caught
    provider exception inside User.send_template_email)
  - was_accepted() is False for a persisted-but-refused SentMessage
    (status="failed", no ses_message_id, provider text in status_reason)
  - was_accepted() is False for any state outside ACCEPTED_STATES, and for an
    accepted state with no message id — the predicate fails CLOSED
  - was_accepted() is True only for sending/delivered WITH a message id
  - send_unavailable_response() is a 503 carrying exactly the fixed safe-retry
    body — provider error text never reaches the client
  - POST /api/auth/verify/email/send returns its normal 200 success on the
    accepted path, for both the code and the link branch

The last one is why the endpoint tests here are driven in-process through the
view's keyword-only ``send=`` seam with a RequestFactory request: the test
project ships no system default mailbox by design (tests/test_aws/email_admin.py,
maestro #2789), so a send through ``opts.client`` can never be accepted and the
200 branch is not provable over the wire. No mock.patch and no attribute
assignment on production modules — this package is default_core and its
cold_budget must not grow.
"""
from testit import helpers as th
from testit.helpers import assert_eq, assert_true

TESTIT_TIER = "extended"

SA_USER = "send_acceptance_user"
SA_PWORD = "sendaccept##mojo99"
FAKE_SES_ID = "0100018f-sendacceptance-test"


@th.django_unit_setup()
def setup_send_acceptance(opts):
    from mojo.apps.account.models import User
    from mojo.apps.incident.models import Event

    # Delete before creating — the suite runs against a long-lived database.
    User.objects.filter(username=SA_USER).delete()

    user = User(username=SA_USER, email=f"{SA_USER}@example.com")
    user.save()
    user.is_active = True
    user.is_email_verified = False
    user.is_phone_verified = False
    user.requires_mfa = False
    user.save_password(SA_PWORD)
    user.save()
    opts.sa_user_id = user.pk

    # Events are matched by this user's pk, so clear anything a previous run
    # left behind for it.
    Event.objects.filter(model_id=user.pk, category__startswith="email_verify:").delete()


class _FakeSend:
    """Stand-in for User.send_template_email, injected through the view seam.

    Records what the endpoint asked to send and returns a caller-chosen result
    shape. Never touches SES, a Mailbox, or the database.
    """

    def __init__(self, result):
        self.result = result
        self.calls = []

    def __call__(self, template_name, context=None, **kwargs):
        self.calls.append(dict(template_name=template_name, context=context or {}))
        return self.result


def _sent(status, ses_message_id=None, status_reason=None):
    """An UNSAVED SentMessage — the real result shape, no DB write, no SES."""
    from mojo.apps.aws.models import SentMessage
    return SentMessage(
        status=status,
        ses_message_id=ses_message_id,
        status_reason=status_reason,
    )


def _post_request(payload):
    """A RequestFactory POST carrying the attributes mojo middleware adds."""
    from django.test import RequestFactory
    from objict import objict

    factory = RequestFactory(REMOTE_ADDR="127.0.0.1")
    request = factory.post("/api/auth/verify/email/send", payload or {})
    request.DATA = objict.from_dict(payload or {})
    return request


def _call_send_endpoint(opts, payload, send):
    """Drive the real (decorated) view in-process with an injected sender."""
    from mojo.apps.account.models import User
    from mojo.apps.account.rest import verify
    from mojo.decorators.limits import clear_rate_limits

    clear_rate_limits(ip="127.0.0.1")
    User.objects.filter(pk=opts.sa_user_id).update(
        is_email_verified=False, is_active=True)
    request = _post_request(payload)
    request.user = User.objects.get(pk=opts.sa_user_id)
    return verify.on_email_verify_send(request, send=send)


def _clear_verify_secrets(opts):
    from mojo.apps.account.models import User
    import mojo.apps.account.utils.tokens as tok_module

    user = User.objects.get(pk=opts.sa_user_id)
    user.set_secret("email_verify_code", None)
    user.set_secret("email_verify_code_ts", None)
    user.set_secret(tok_module._JTI_KEYS[tok_module.KIND_EMAIL_VERIFY], None)
    user.save(update_fields=["mojo_secrets", "modified"])


# ===========================================================================
# was_accepted() — the shared acceptance predicate
# ===========================================================================

@th.tier("bug")
@th.django_unit_test("email_delivery: was_accepted(None) is False — no mailbox / caught provider error")
def test_was_accepted_rejects_none(opts):
    from mojo.apps.account.services.email_delivery import was_accepted

    assert_eq(was_accepted(None), False,
              "None is what send_template_email returns for a missing mailbox "
              "and for any caught provider exception — never an accepted send")


@th.tier("bug")
@th.django_unit_test("email_delivery: was_accepted(failed SentMessage) is False — a refused send is persisted, not accepted")
def test_was_accepted_rejects_failed_sent_message(opts):
    from mojo.apps.account.services.email_delivery import was_accepted

    refused = _sent(
        "failed",
        status_reason="An error occurred (MessageRejected) when calling SendEmail",
    )
    assert_eq(was_accepted(refused), False,
              "send_with_template persists and RETURNS a failed SentMessage when "
              "SES refuses — a truthy result is not proof of acceptance")


@th.django_unit_test("email_delivery: was_accepted() is False for an accepted state with no message id")
def test_was_accepted_rejects_missing_message_id(opts):
    from mojo.apps.account.services.email_delivery import was_accepted

    assert_eq(was_accepted(_sent("sending")), False,
              "an accepted state with no ses_message_id means the provider "
              "never handed back a message id — fail closed")


@th.django_unit_test("email_delivery: was_accepted() is False for every state outside ACCEPTED_STATES")
def test_was_accepted_rejects_unknown_states(opts):
    from mojo.apps.account.services.email_delivery import was_accepted

    for status in ("queued", "bounced", "complained", "failed", "unknown"):
        assert_eq(was_accepted(_sent(status, ses_message_id=FAKE_SES_ID)), False,
                  f"status={status!r} is not an accepted state, even with a "
                  f"message id — the predicate must fail closed on shapes it "
                  f"does not recognise")


@th.tier("bug")
@th.django_unit_test("email_delivery: was_accepted() is True for sending/delivered with a message id")
def test_was_accepted_accepts_sending_and_delivered_with_id(opts):
    from mojo.apps.account.services.email_delivery import ACCEPTED_STATES, was_accepted

    assert_eq(sorted(ACCEPTED_STATES), ["delivered", "sending"],
              "ACCEPTED_STATES is the cross-item contract — changing it changes "
              "what every caller reports as sent")
    for status in ACCEPTED_STATES:
        assert_eq(was_accepted(_sent(status, ses_message_id=FAKE_SES_ID)), True,
                  f"status={status!r} with a message id is provider custody — "
                  f"the caller may honestly say it was sent")


# ===========================================================================
# send_unavailable_response() — the fixed safe-retry payload
# ===========================================================================

@th.tier("bug")
@th.django_unit_test("email_delivery: send_unavailable_response() is a 503 with exactly the fixed body")
def test_send_unavailable_response_shape(opts):
    import json
    from mojo.apps.account.services.email_delivery import (
        EMAIL_SEND_UNAVAILABLE, send_unavailable_response)

    response = send_unavailable_response()
    assert_eq(response.status_code, 503,
              "an unaccepted send is a retryable server-side condition")
    body = json.loads(response.content.decode("utf-8"))
    assert_eq(body, {"status": False, "code": 503, "error": EMAIL_SEND_UNAVAILABLE},
              "the body must be exactly the fixed safe-retry payload")
    assert_true("MessageRejected" not in body["error"] and "SES" not in body["error"],
                f"provider error text must never reach the client: {body['error']!r}")
    assert_true("try again" in body["error"].lower(),
                f"the copy must tell the person to retry: {body['error']!r}")


# ===========================================================================
# POST /api/auth/verify/email/send — in-process, through the send= seam
# ===========================================================================

@th.tier("bug")
@th.django_unit_test("verify/email/send: an ACCEPTED send returns the 200 success response (code and link)")
def test_endpoint_accepted_path_in_process(opts):
    """
    The only place the 200 branch is provable: over the wire the test project
    has no mailbox, so no send can ever be accepted.
    """
    import json
    from mojo.apps.account.models import User
    from mojo.apps.incident.models import Event

    try:
        # --- code branch ---
        sender = _FakeSend(_sent("sending", ses_message_id=FAKE_SES_ID))
        response = _call_send_endpoint(opts, {"method": "code"}, sender)
        body = json.loads(response.content.decode("utf-8"))

        assert_eq(response.status_code, 200,
                  f"an accepted send must keep returning 200, got {response.status_code}: {body}")
        assert_eq(body.get("status"), True, f"status must be True on success: {body}")
        assert_eq(body.get("message"), "Verification code sent",
                  f"the success copy must be unchanged: {body}")
        assert_eq(len(sender.calls), 1, f"exactly one send must be attempted: {sender.calls}")
        assert_eq(sender.calls[0]["template_name"], "email_verify_code",
                  f"the code branch must send the OTP template: {sender.calls[0]}")
        code = sender.calls[0]["context"].get("code")
        assert_true(code is not None and len(code) == 6 and code.isdigit(),
                    f"the 6-digit code must be passed to the template, got {code!r}")
        stored = User.objects.get(pk=opts.sa_user_id).get_secret("email_verify_code")
        assert_eq(stored, code, "the emailed code must be the one stored in secrets")
        assert_true(
            Event.objects.filter(
                model_id=opts.sa_user_id, category="email_verify:sent_code").exists(),
            "an accepted code send must still record the email_verify:sent_code incident")

        # --- link branch ---
        sender = _FakeSend(_sent("delivered", ses_message_id=FAKE_SES_ID))
        response = _call_send_endpoint(opts, {"method": "link"}, sender)
        body = json.loads(response.content.decode("utf-8"))

        assert_eq(response.status_code, 200,
                  f"an accepted send must keep returning 200, got {response.status_code}: {body}")
        assert_eq(body.get("message"), "Verification email sent",
                  f"the success copy must be unchanged: {body}")
        assert_eq(sender.calls[0]["template_name"], "email_verify",
                  f"the link branch must send the link template: {sender.calls[0]}")
        context = sender.calls[0]["context"]
        assert_true(context.get("token", "").startswith("ev:"),
                    f"an ev: token must be passed to the template, got {context.get('token')!r}")
        assert_true(bool(context.get("token_url")),
                    f"a server-resolved token_url must be passed to the template: {context}")
        assert_true(
            Event.objects.filter(
                model_id=opts.sa_user_id, category="email_verify:sent").exists(),
            "an accepted link send must still record the email_verify:sent incident")
    finally:
        _clear_verify_secrets(opts)


@th.tier("bug")
@th.django_unit_test("verify/email/send: an UNACCEPTED send returns 503 and leaves the stored code/JTI intact")
def test_endpoint_unaccepted_path_in_process(opts):
    import json
    from mojo.apps.account.models import User
    import mojo.apps.account.utils.tokens as tok_module
    from mojo.apps.account.services.email_delivery import EMAIL_SEND_UNAVAILABLE

    expected = {"status": False, "code": 503, "error": EMAIL_SEND_UNAVAILABLE}
    try:
        # --- None: no mailbox configured, or a caught provider exception ---
        response = _call_send_endpoint(opts, {"method": "code"}, _FakeSend(None))
        body = json.loads(response.content.decode("utf-8"))
        assert_eq(response.status_code, 503,
                  f"a None result must not be reported as sent, got {response.status_code}: {body}")
        assert_eq(body, expected, "the 503 body must be the fixed safe-retry payload")
        stored = User.objects.get(pk=opts.sa_user_id).get_secret("email_verify_code")
        assert_true(
            stored is not None and len(stored) == 6 and stored.isdigit(),
            f"code generation is unchanged by the failure — an unconditional 503 "
            f"stub must not pass this test, got {stored!r}")

        # --- persisted-but-refused SentMessage ---
        refused = _sent("failed", status_reason="An error occurred (MessageRejected)")
        response = _call_send_endpoint(opts, {"method": "link"}, _FakeSend(refused))
        body = json.loads(response.content.decode("utf-8"))
        assert_eq(response.status_code, 503,
                  f"a refused SentMessage must not be reported as sent, got "
                  f"{response.status_code}: {body}")
        assert_eq(body, expected, "the 503 body must be the fixed safe-retry payload")
        assert_true("MessageRejected" not in json.dumps(body),
                    f"provider error text must never reach the client: {body}")
        jti = User.objects.get(pk=opts.sa_user_id).get_secret(
            tok_module._JTI_KEYS[tok_module.KIND_EMAIL_VERIFY])
        assert_true(jti is not None,
                    "token generation is unchanged by the failure — the ev: JTI "
                    "must still be stored")
    finally:
        _clear_verify_secrets(opts)
