"""Regression: POST /api/phonehub/sms/send honors a caller-supplied from_number.

The on_sms_send handler used to read only to_number, body, and metadata
from the request — the documented `from_number` field was dropped, so
SMS.send always fell back to twilio.get_from_number(). A caller that
owns multiple sender numbers could not choose which one to send from.

The +1555 to_number short-circuits inside SMS.send (is_test=True, no
network call), so the SMS row is created with whatever from_number the
dispatcher resolved — that is what this test inspects.
"""
from testit import helpers as th


GROUP_NAME = "test_sms_fromnum_group"
KEY_NAME = "test_sms_fromnum_key"
BODY_PREFIX = "test_sms_fromnum:"
CALLER_FROM = "+18005551234"
TEST_NUMBER = "+15550008888"


@th.django_unit_setup()
def setup_sms_from_number(opts):
    """Create a fresh group + API key (with send_sms) for the send path."""
    from mojo.apps.account.models import Group, ApiKey
    from mojo.apps.phonehub.models import SMS

    # Tests run on long-lived databases — clean prior runs first.
    SMS.objects.filter(body__startswith=BODY_PREFIX).delete()
    ApiKey.objects.filter(name=KEY_NAME).delete()
    Group.objects.filter(name=GROUP_NAME).delete()

    group = Group.objects.create(name=GROUP_NAME, kind="organization")
    api_key, raw_token = ApiKey.create_for_group(
        group=group,
        name=KEY_NAME,
        permissions={"send_sms": True},
    )
    opts.group_id = group.id
    opts.raw_token = raw_token


@th.django_unit_test("sms send honors a caller-supplied from_number")
def test_sms_send_from_number(opts):
    from mojo.apps.phonehub.models import SMS

    opts.client.logout()
    opts.client.bearer = "apikey"
    opts.client.access_token = opts.raw_token
    opts.client.is_authenticated = True

    body = f"{BODY_PREFIX}pick-sender"
    resp = opts.client.post(
        "/api/phonehub/sms/send",
        {"to_number": TEST_NUMBER, "body": body, "from_number": CALLER_FROM},
    )
    assert resp.status_code == 200, (
        f"SMS send must succeed, got {resp.status_code}: {resp.response}"
    )

    sms = SMS.objects.filter(body=body).last()
    assert sms is not None, "an SMS row must be created"
    # Before the fix, on_sms_send never read from_number, so SMS.send fell
    # back to twilio.get_from_number() and this assertion failed.
    assert sms.from_number == CALLER_FROM, (
        f"caller-supplied from_number must be honored, got "
        f"from_number={sms.from_number!r}, expected {CALLER_FROM!r}"
    )

    opts.client.logout()


@th.django_unit_test("sms send from_number cleanup")
def test_cleanup(opts):
    from mojo.apps.account.models import Group, ApiKey
    from mojo.apps.phonehub.models import SMS

    SMS.objects.filter(body__startswith=BODY_PREFIX).delete()
    ApiKey.objects.filter(name=KEY_NAME).delete()
    Group.objects.filter(name=GROUP_NAME).delete()
