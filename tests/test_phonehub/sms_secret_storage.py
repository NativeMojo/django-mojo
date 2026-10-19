"""The SMS log never keeps a sign-in code or link readable (#8632).

Each of the six auth senders is driven in-process through its keyword-only
send seam, wrapped around the REAL phonehub.send_sms, to a fictional +1555
number (stored as a test row, no provider contact). The secret is read from
the text the sender was handed, never from the SMS row, and the stored row
must hold [redacted] in its place.

The relay path (mocked remote) is in
tests/test_phonehub_extended_serial/sms_secret_relay.py: a new patch of
production code is not accepted in this default-tier package.
"""

from testit import helpers as th

PREFIX = "sms_secret_storage"
USER = f"{PREFIX}_user"
PWORD = "smsSecretStore##mojo99"
VIEWER = f"{PREFIX}_viewer@example.com"
VIEWER_PWORD = "smsSecretViewer##mojo99"
PHONE = "+15550008631"
NEW_PHONE = "+15550008632"
REG_PHONE = "+15550008633"
REDACTED = "[redacted]"
SEED_BODY = "Your password reset code is: 314159"


@th.django_unit_setup()
def setup_sms_secret_storage(opts):
    from mojo.apps.account.models import User
    from mojo.apps.phonehub.models import SMS

    # Clean first: the suite runs on long-lived databases.
    SMS.objects.filter(to_number__in=[PHONE, NEW_PHONE, REG_PHONE]).delete()
    SMS.objects.filter(metadata__source=PREFIX).delete()
    User.objects.filter(username__in=[USER, VIEWER]).delete()

    user = User(username=USER, email=f"{USER}@example.com")
    user.save()
    User.objects.exclude(pk=user.pk).filter(
        phone_number__in=[PHONE, NEW_PHONE, REG_PHONE]).update(phone_number=None)
    user.is_active = True
    user.phone_number = PHONE
    user.is_email_verified = True
    user.is_phone_verified = True
    user.requires_mfa = False
    user.save_password(PWORD)
    user.save()
    opts.user_id = user.pk

    viewer = User.objects.create_user(username=VIEWER, email=VIEWER, password=VIEWER_PWORD)
    viewer.is_active = True
    viewer.is_email_verified = True
    viewer.requires_mfa = False
    viewer.add_permission("view_sms")
    viewer.save()


class _RealSend:
    """Records the text handed to the transport, then sends it for real."""

    def __init__(self):
        self.messages = []

    def __call__(self, phone_number, message, **kwargs):
        from mojo.apps import phonehub
        self.messages.append(message)
        return phonehub.send_sms(phone_number, message, **kwargs)


def _request(path, data, user=None):
    from django.test import RequestFactory
    from objict import objict
    from mojo.middleware.mojo import ANONYMOUS_USER

    request = RequestFactory(REMOTE_ADDR="127.0.0.1").post(path, {})
    request.DATA = objict(data)
    request.ip = "127.0.0.1"
    request.user = user or ANONYMOUS_USER
    request.bearer = None
    request.group = None
    request.duid = None
    request.muid = None
    request.user_agent = "testit"
    return request


def _fresh_user(opts, phone=PHONE, **fields):
    from mojo.apps.account.models import User
    from mojo.decorators import limits
    from mojo.decorators.limits import clear_rate_limits

    clear_rate_limits(ip="127.0.0.1")
    for kind in ("sms", "reset"):
        limits.clear_code_sends(kind, opts.user_id)
    limits.clear_code_sends("phone_register", REG_PHONE)
    User.objects.filter(pk=opts.user_id).update(
        is_active=True, phone_number=phone, **fields)
    user = User.objects.get(pk=opts.user_id)
    for key in ("sms_otp_code", "sms_otp_ts", "password_reset_code",
                "password_reset_code_ts", "pending_phone", "phone_change_otp",
                "phone_change_otp_ts", "phone_change_jti"):
        user.set_secret(key, None)
    user.save(update_fields=["mojo_secrets", "modified"])
    return User.objects.get(pk=opts.user_id)


def _assert_masked(opts, label, sender, prefix, to_number):
    from mojo.apps.phonehub.models import SMS

    assert len(sender.messages) == 1, \
        f"{label}: exactly one text must be sent, got {len(sender.messages)}"
    sent = sender.messages[0]
    assert sent.startswith(prefix) and len(sent) > len(prefix), \
        f"{label}: the sent text must carry its secret after {prefix!r}"
    secret = sent[len(prefix):]
    row = SMS.objects.filter(to_number=to_number).order_by("-pk").first()
    assert row is not None, f"{label}: the send must store an SMS row"
    assert secret not in row.body, f"{label}: the stored body still holds the secret"
    assert REDACTED in row.body, f"{label}: the stored body must say [redacted]"
    assert secret not in str(row.metadata), f"{label}: the stored metadata holds the secret"

    assert opts.client.login(VIEWER, VIEWER_PWORD), "SMS viewer login failed"
    resp = opts.client.get("/api/phonehub/sms", params={"search": secret})
    assert resp.status_code == 200, f"{label}: SMS list search failed: {resp.status_code}"
    assert resp.response.data == [], f"{label}: a search for the secret must find no row"
    seen = opts.client.get("/api/phonehub/sms", params={"search": to_number})
    assert any(item.id == row.pk for item in (seen.response.data or [])), \
        f"{label}: the viewer must see the row by its number, or the empty search proves nothing"
    opts.client.logout()
    return secret


@th.django_unit_test("SMS login code is stored as [redacted]")
def test_sms_login_code_masked(opts):
    from mojo.apps.account.rest import sms as sms_rest

    user = _fresh_user(opts)
    sender = _RealSend()
    sms_rest._send_otp(user, None, send=sender)
    secret = _assert_masked(opts, "sms login", sender, "Your verification code is: ", PHONE)
    assert _fresh_user_secret(opts, "sms_otp_code") in (None, secret), \
        "sms login: the text must carry the account's issued code"


def _fresh_user_secret(opts, key):
    from mojo.apps.account.models import User
    return User.objects.get(pk=opts.user_id).get_secret(key)


@th.django_unit_test("phone sign-up code is stored as [redacted]")
def test_phone_register_code_masked(opts):
    from mojo.apps.account.rest import sms as sms_rest

    _fresh_user(opts)
    sender = _RealSend()
    response = sms_rest.on_phone_register_start(
        _request("/api/auth/phone/register/start", dict(phone=REG_PHONE)), send=sender)
    assert response.status_code == 200, \
        f"phone register start must answer 200, got {response.status_code}"
    _assert_masked(opts, "phone sign-up", sender, "Your verification code is: ", REG_PHONE)


@th.django_unit_test("password reset code is stored as [redacted]")
def test_password_reset_code_masked(opts):
    from mojo.apps.account.rest import user as user_rest

    _fresh_user(opts)
    sender = _RealSend()
    response = user_rest.on_user_forgot(
        _request("/api/auth/forgot", dict(username=USER, method="code", channel="sms")),
        send_sms=sender, send_email=lambda *args, **kwargs: True)
    assert response.status_code == 200, f"forgot must answer 200, got {response.status_code}"
    secret = _assert_masked(opts, "password reset", sender, "Your password reset code is: ", PHONE)
    assert secret == _fresh_user_secret(opts, "password_reset_code"), \
        "password reset: the text must carry the account's issued code"


@th.django_unit_test("login link is stored as [redacted]")
def test_magic_login_link_masked(opts):
    from mojo.apps.account.rest import user as user_rest

    _fresh_user(opts)
    sender = _RealSend()
    response = user_rest.on_magic_login_send(
        _request("/api/auth/magic/send", dict(username=USER, method="sms")), send=sender)
    assert response.status_code == 200, f"magic send must answer 200, got {response.status_code}"
    _assert_masked(opts, "login link", sender, "Your login link: ", PHONE)


@th.django_unit_test("phone change code is stored as [redacted]")
def test_phone_change_code_masked(opts):
    from mojo.apps.account.rest import user as user_rest

    user = _fresh_user(opts)
    sender = _RealSend()
    response = user_rest.on_phone_change_request(
        _request("/api/auth/phone/change/request", dict(phone_number=NEW_PHONE), user=user),
        send=sender)
    assert response.status_code == 200, \
        f"phone change request must answer 200, got {response.status_code}"
    # The second text is the notice to the old number; it carries no secret.
    sender.messages = sender.messages[:1]
    secret = _assert_masked(opts, "phone change", sender,
                            "Your phone change verification code is: ", NEW_PHONE)
    assert secret == _fresh_user_secret(opts, "phone_change_otp"), \
        "phone change: the text must carry the account's issued code"


@th.django_unit_test("phone verification code is stored as [redacted]")
def test_phone_verify_code_masked(opts):
    from mojo.apps.account.rest import verify

    user = _fresh_user(opts, is_phone_verified=False)
    sender = _RealSend()
    request = _request("/api/auth/verify/phone/send", {}, user=user)
    response = verify.on_phone_verify_send(request, send=sender)
    assert response.status_code == 200, \
        f"phone verify send must answer 200, got {response.status_code}"
    _assert_masked(opts, "phone verify", sender, "Your verification code is: ", PHONE)


@th.django_unit_test("a secret missing from a changed template stores [redacted] alone")
def test_missing_secret_fails_closed(opts):
    from mojo.apps import phonehub

    sms = phonehub.send_sms(PHONE, "Use 4242 to sign in", secret="999999",
                            metadata={"source": PREFIX})
    assert sms.body == REDACTED, f"a secret not in the body must store [redacted], got {sms.body!r}"
    plain = phonehub.send_sms(PHONE, "Your order shipped", metadata={"source": PREFIX})
    assert plain.body == "Your order shipped", "a body sent with no secret is stored unchanged"


@th.django_unit_test("sms/send with secret stores and returns a masked body")
def test_sms_send_endpoint_masks_secret(opts):
    from mojo.apps.phonehub.rest import sms as sms_rest_view

    request = _request("/api/phonehub/sms/send", dict(
        to_number=PHONE, body="Your verification code is: 271828", secret="271828",
        metadata={"source": PREFIX}))
    request.user = _fresh_user(opts)
    request.user.add_permission("send_sms")
    request.user.save()
    response = sms_rest_view.on_sms_send(request)
    data = response.data if hasattr(response, "data") else response
    rendered = response.content.decode("utf-8") if hasattr(response, "content") else str(data)
    assert "271828" not in rendered, "the relay's reply must not carry the secret"
    assert REDACTED in rendered, "the relay's reply must carry the masked body"
    from mojo.apps.phonehub.models import SMS
    row = SMS.objects.filter(to_number=PHONE).order_by("-pk").first()
    assert row.body == f"Your verification code is: {REDACTED}", \
        f"the relay's own row must be masked, got {row.body!r}"
    request.user.remove_permission("send_sms")
    request.user.save()


@th.django_unit_test("mask_sms_secrets: dry run changes nothing, --apply masks, a second --apply changes nothing")
def test_mask_command(opts):
    from io import StringIO
    from django.core.management import call_command
    from mojo.apps.phonehub.models import SMS

    SMS.objects.filter(metadata__source=PREFIX).delete()
    seeded = SMS.objects.create(
        direction="outbound", from_number="+15550000000", to_number=PHONE,
        body=SEED_BODY, status="sent",
        metadata={"source": PREFIX, "remote": {
            "id": 7, "status": "sent", "from_number": "+18005550100",
            "body": SEED_BODY, "to_number": PHONE}})
    plain = SMS.objects.create(
        direction="outbound", from_number="+15550000000", to_number=PHONE,
        body="Your order shipped", status="sent", metadata={"source": PREFIX})

    out = StringIO()
    call_command("mask_sms_secrets", stdout=out)
    seeded.refresh_from_db()
    assert seeded.body == SEED_BODY, "a dry run must change no row"
    assert "314159" not in out.getvalue(), "the command must never print a code"

    call_command("mask_sms_secrets", "--apply", stdout=StringIO())
    seeded.refresh_from_db()
    plain.refresh_from_db()
    assert seeded.body == f"Your password reset code is: {REDACTED}", \
        f"--apply must mask the code, got {seeded.body!r}"
    assert seeded.metadata["remote"] == {"id": 7, "status": "sent", "from_number": "+18005550100"}, \
        f"--apply must cut metadata['remote'] to the allowlist, got {seeded.metadata['remote']!r}"
    assert seeded.metadata.get("source") == PREFIX, "--apply must keep other metadata keys"
    assert plain.body == "Your order shipped", "--apply must leave non-auth bodies alone"

    from mojo.apps.phonehub.management.commands.mask_sms_secrets import mask_rows
    seeded_ids = set(SMS.objects.filter(metadata__source=PREFIX).values_list("pk", flat=True))
    before = {s.pk: (s.body, s.metadata) for s in SMS.objects.filter(pk__in=seeded_ids)}
    mask_rows(apply=True)
    after = {s.pk: (s.body, s.metadata) for s in SMS.objects.filter(pk__in=seeded_ids)}
    assert before == after, "a second --apply must change nothing"
