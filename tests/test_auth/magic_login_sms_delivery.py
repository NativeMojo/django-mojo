"""
Magic-login SMS: enumeration-invariant by construction (#3411).

`POST /api/auth/magic/send` promises one response for everybody — "If account
is in our system a login link was sent." — and the whole anti-enumeration
guarantee rests on that response never varying. The `method=sms` branch broke
it: it sent through `phonehub.send_sms(user.phone_number, ...)` with NO
try/except and NO normalization.

`SMS.send()` normalizes the recipient itself and gets `None` back for a stored
number `phonehub.normalize()` cannot parse; it then writes that `None` into a
NOT NULL column and raises. The endpoint 500s — but only for an identifier
that (a) resolves to a real account AND (b) whose stored number is
un-normalizable. Every other caller still gets the generic 200. That
difference is the enumeration oracle, and it is deterministic: an attacker can
repeat it.

Contract this file enforces:

  - an un-normalizable stored number is treated as "no usable number": the
    response stays the generic 200 and an operator incident
    (`magic_login:phone_unusable`) is filed, because this failure is permanent
    and nothing else would ever surface it
  - a transport exception is absorbed — same generic 200
  - a failing send returns a response BYTE-IDENTICAL to the one an unknown
    identifier gets
  - the recipient is normalized before the send
  - the accepted path is unchanged

Driven in-process through the view's keyword-only ``send=`` seam with a
RequestFactory request. No mock.patch and no attribute assignment on
production modules — this package is default_core and its cold_budget must
not grow.
"""
from testit import helpers as th
from testit.helpers import assert_eq, assert_true

TESTIT_TIER = "extended"

MAGIC_USER = "magic_smsdel_user"
MAGIC_PWORD = "magicsmsdel##mojo99"
MAGIC_PHONE = "+15550007722"
MAGIC_PHONE_RAW = "5550007722"
# phonehub.normalize() returns None for this — too few digits to resolve a
# country, and no leading '+'. Exactly the shape that 500s the old branch.
UNUSABLE_PHONE = "12345"
UNKNOWN_IDENTIFIER = "ghost_magic_smsdel_xyz"

GENERIC_MESSAGE = "If account is in our system a login link was sent."


@th.django_unit_setup()
def setup_magic_login_sms_delivery(opts):
    from mojo.apps.account.models import User
    from mojo.apps.incident.models import Event

    # Delete before creating — the suite runs against a long-lived database.
    User.objects.filter(username=MAGIC_USER).delete()
    User.objects.filter(username=UNKNOWN_IDENTIFIER).delete()

    user = User(username=MAGIC_USER, email=f"{MAGIC_USER}@example.com")
    user.save()
    # Claim the fixture number FIRST — phone_number is unique.
    User.objects.exclude(pk=user.pk).filter(phone_number=MAGIC_PHONE).update(
        phone_number=None)
    user.is_active = True
    user.phone_number = MAGIC_PHONE
    user.is_email_verified = True
    user.is_phone_verified = True
    user.requires_mfa = False
    user.save_password(MAGIC_PWORD)
    user.save()
    opts.magic_user_id = user.pk

    Event.objects.filter(
        model_id=user.pk, category__startswith="magic_login").delete()


class _FakeSend:
    """Stand-in for phonehub.send_sms, injected through the view seam."""

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
        to_number=MAGIC_PHONE,
        body="Your login link: https://example.com/ml",
        status=status,
        error_code=error_code,
        error_message=error_message,
    )


def _call_magic_send(username, send=None):
    """Drive the real (decorated) view in-process, optionally with a sender."""
    from django.test import RequestFactory
    from objict import objict
    from mojo.apps.account.rest import user as user_rest
    from mojo.decorators.limits import clear_rate_limits
    from mojo.middleware.mojo import ANONYMOUS_USER

    # The endpoint allows 5 requests / 300s per IP — every call clears first.
    clear_rate_limits(ip="127.0.0.1")
    factory = RequestFactory(REMOTE_ADDR="127.0.0.1")
    request = factory.post("/api/auth/magic/send", {})
    request.DATA = objict(username=username, method="sms")
    # Mirror the attributes MojoMiddleware stamps — the unknown-identifier
    # branch reports an incident with request=, and the reporter reads
    # request.ip / request.user directly.
    request.ip = "127.0.0.1"
    request.user = ANONYMOUS_USER
    request.bearer = None
    request.group = None
    request.duid = None
    request.user_agent = "testit"
    if send is None:
        return user_rest.on_magic_login_send(request)
    return user_rest.on_magic_login_send(request, send=send)


def _set_phone(opts, value):
    from mojo.apps.account.models import User

    # .update() bypasses the model's save-time normalization so the row can
    # hold exactly the raw value under test.
    User.objects.filter(pk=opts.magic_user_id).update(phone_number=value)


def _restore_phone(opts):
    _set_phone(opts, MAGIC_PHONE)


# ===========================================================================
# The enumeration oracle — an un-normalizable stored number
# ===========================================================================

@th.tier("bug")
@th.django_unit_test("magic/send sms: an un-normalizable stored number stays generic and files an incident")
def test_magic_send_unnormalizable_number_stays_generic(opts):
    import json
    from mojo.apps import phonehub
    from mojo.apps.incident.models import Event

    # The precondition IS the bug: prove this value has no normalized form
    # before asserting what the endpoint does with it.
    assert_eq(phonehub.normalize(UNUSABLE_PHONE), None,
              f"the fixture depends on {UNUSABLE_PHONE!r} being un-normalizable "
              f"— if normalize() learns to parse it, pick another value")

    try:
        _set_phone(opts, UNUSABLE_PHONE)
        Event.objects.filter(model_id=opts.magic_user_id,
                             category="magic_login:phone_unusable").delete()

        response = _call_magic_send(MAGIC_USER)
        body = json.loads(response.content.decode("utf-8"))

        assert_eq(response.status_code, 200,
                  f"an un-normalizable stored number must not 500 — that "
                  f"difference is an account-existence oracle, got "
                  f"{response.status_code}: {body}")
        assert_eq(body.get("status"), True, f"status must stay True: {body}")
        assert_eq(body.get("message"), GENERIC_MESSAGE,
                  f"the generic copy must be unchanged: {body}")
        assert_true(
            Event.objects.filter(model_id=opts.magic_user_id,
                                 category="magic_login:phone_unusable").exists(),
            "a permanently unusable number must reach an operator — this is the "
            "one branch with no other signal that anything is wrong")
    finally:
        _restore_phone(opts)


# ===========================================================================
# Send failures are absorbed
# ===========================================================================

@th.tier("bug")
@th.django_unit_test("magic/send sms: a transport exception still returns the generic response")
def test_magic_send_sms_transport_exception_stays_generic(opts):
    import json

    sender = _FakeSend(raises=RuntimeError("twilio socket timeout"))
    response = _call_magic_send(MAGIC_USER, send=sender)
    body = json.loads(response.content.decode("utf-8"))

    assert_eq(response.status_code, 200,
              f"a raising transport must not turn into a 500 for accounts that "
              f"exist, got {response.status_code}: {body}")
    assert_eq(body.get("message"), GENERIC_MESSAGE,
              f"the generic copy must be unchanged: {body}")
    assert_true("socket timeout" not in json.dumps(body),
                f"exception text must never reach the client: {body}")
    assert_eq(len(sender.calls), 1,
              f"the send must still have been attempted: {sender.calls}")


@th.tier("bug")
@th.django_unit_test("magic/send sms: a failing send answers byte-identically to an unknown identifier")
def test_magic_send_sms_failure_matches_unknown_account_response(opts):
    refused = _sms("failed", error_code="config_error",
                   error_message="Twilio config supplies only half a credential pair")
    known = _call_magic_send(MAGIC_USER, send=_FakeSend(refused))
    unknown = _call_magic_send(UNKNOWN_IDENTIFIER, send=_FakeSend(refused))

    assert_eq(known.status_code, unknown.status_code,
              f"a real account whose send failed must answer with the same "
              f"status as an unknown identifier — got {known.status_code} vs "
              f"{unknown.status_code}")
    assert_eq(known.content, unknown.content,
              f"the two bodies must be byte-identical or the difference IS the "
              f"oracle — got {known.content!r} vs {unknown.content!r}")


@th.tier("bug")
@th.django_unit_test("magic/send sms: the recipient number is normalized before the send")
def test_magic_send_sms_normalizes_recipient(opts):
    try:
        # A raw national-format number, stored exactly as given.
        _set_phone(opts, MAGIC_PHONE_RAW)
        sender = _FakeSend(_sms("sent"))
        response = _call_magic_send(MAGIC_USER, send=sender)

        assert_eq(response.status_code, 200,
                  f"the send must succeed, got {response.status_code}")
        assert_eq(len(sender.calls), 1,
                  f"exactly one send must be attempted: {sender.calls}")
        assert_eq(sender.calls[0]["phone_number"], MAGIC_PHONE,
                  f"the transport must receive the E.164 form, not the raw "
                  f"stored value: {sender.calls[0]}")
    finally:
        _restore_phone(opts)


# ===========================================================================
# Guard — the accepted path is untouched
# ===========================================================================

@th.tier("bug")
@th.django_unit_test("magic/send sms: an ACCEPTED send returns the same generic response")
def test_magic_send_sms_accepted_stays_generic(opts):
    import json

    sender = _FakeSend(_sms("sent"))
    response = _call_magic_send(MAGIC_USER, send=sender)
    body = json.loads(response.content.decode("utf-8"))

    assert_eq(response.status_code, 200,
              f"an accepted send must keep returning 200, got "
              f"{response.status_code}: {body}")
    assert_eq(body.get("message"), GENERIC_MESSAGE,
              f"the response must not vary on send outcome: {body}")
    assert_eq(len(sender.calls), 1,
              f"exactly one send must be attempted: {sender.calls}")
    assert_eq(sender.calls[0]["phone_number"], MAGIC_PHONE,
              f"the login link goes to the account's number: {sender.calls[0]}")
    message = sender.calls[0]["message"]
    prefix = "Your login link: "
    assert_true(message.startswith(prefix) and len(message) > len(prefix),
                f"the SMS body must carry a non-empty login link — it may be a "
                f"shortlink path rather than an absolute URL: {sender.calls[0]}")
