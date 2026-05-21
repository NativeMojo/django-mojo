"""Regression: POST /api/phonehub/sms/send under API-key authentication.

When a request authenticates with `Authorization: apikey <token>`,
`request.user` is the ApiKey instance, not a User. The on_sms_send
handler used to forward it straight into SMS.send(user=...), which
assigns it to the SMS.user ForeignKey (account.User) and raises:

    ValueError: Cannot assign "<ApiKey: ...>":
    "SMS.user" must be a "User" instance.

The handler must attach a real User only, leaving SMS.user null for
API-key callers (the group still identifies the caller). This is the
surface the `mojo` SMS provider relay uses, so it must not 500.
"""
from testit import helpers as th


GROUP_NAME = "test_sms_apikey_group"
KEY_NAME = "test_sms_apikey_key"
BODY_PREFIX = "test_sms_apikey:"
TEST_NUMBER = "+15550009999"


@th.django_unit_setup()
def setup_sms_apikey(opts):
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


@th.django_unit_test("sms send via api key does not 500 and leaves user null")
def test_sms_send_via_api_key(opts):
    from mojo.apps.phonehub.models import SMS

    opts.client.logout()
    opts.client.bearer = "apikey"
    opts.client.access_token = opts.raw_token
    opts.client.is_authenticated = True

    body = f"{BODY_PREFIX}hello"
    resp = opts.client.post(
        "/api/phonehub/sms/send",
        {"to_number": TEST_NUMBER, "body": body},
    )

    # Before the fix, SMS.send raised ValueError inside the handler and the
    # request failed with a 500 — no SMS row was ever created.
    assert resp.status_code == 200, (
        f"API-key SMS send must not error, got {resp.status_code}: "
        f"{resp.response}"
    )

    sms = SMS.objects.filter(body=body).last()
    assert sms is not None, (
        "an SMS row must be created for an API-key-authenticated send"
    )
    assert sms.user_id is None, (
        f"SMS.user must be null for an API-key caller (an ApiKey is not a "
        f"User), got user_id={sms.user_id!r}"
    )
    assert sms.group_id == opts.group_id, (
        f"SMS.group must be the API key's group, got "
        f"group_id={sms.group_id!r}, expected {opts.group_id!r}"
    )
    assert sms.direction == "outbound", (
        f"a sent SMS must be outbound, got direction={sms.direction!r}"
    )

    opts.client.logout()


@th.django_unit_test("sms send cleanup")
def test_cleanup(opts):
    from mojo.apps.account.models import Group, ApiKey
    from mojo.apps.phonehub.models import SMS

    SMS.objects.filter(body__startswith=BODY_PREFIX).delete()
    ApiKey.objects.filter(name=KEY_NAME).delete()
    Group.objects.filter(name=GROUP_NAME).delete()
