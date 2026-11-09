"""Relay sends keep no sign-in code in the SMS log (#8632).

Moved beside tests/test_phonehub/sms_secret_storage.py: it patches
`requests.post` inside the mojo provider, as tests/test_phonehub/
sms_mojo_provider.py does, and a new patch of production code is not accepted
in the default-tier package. No network call is made.
"""

from unittest import mock

from testit import helpers as th

PREFIX = "sms_secret_relay"
GROUP_NAME = f"{PREFIX}_group"
REMOTE_URL = "https://sms-relay.example.com"
API_KEY = "sms-secret-relay-apikey"


@th.django_unit_setup()
def setup_sms_secret_relay(opts):
    from mojo.apps.account.models import Group
    from mojo.apps.phonehub.models import PhoneConfig, SMS

    # Clean first: the suite runs on long-lived databases.
    SMS.objects.filter(metadata__source=PREFIX).delete()
    PhoneConfig.objects.filter(name__startswith=PREFIX).delete()
    Group.objects.filter(name=GROUP_NAME).delete()

    opts.group = Group.objects.create(name=GROUP_NAME, kind="organization")
    cfg = PhoneConfig(group=opts.group, name=f"{PREFIX}_cfg", provider="mojo",
                      mojo_remote_url=REMOTE_URL)
    cfg.set_mojo_api_key(API_KEY)
    cfg.save()


@th.django_unit_test("relay send passes the secret and keeps only id, status and from_number")
def test_relay_send_masks_and_allowlists_remote(opts):
    from mojo.apps.account.models import Group
    from mojo.apps.phonehub import send_sms

    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None, allow_redirects=None):
        captured["json"] = json
        resp = mock.MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"status": True, "data": {
            "id": 4242, "status": "sent", "from_number": "+18005550100",
            "provider_message_id": "SM_relay", "to_number": "+14155550199",
            "body": "Your verification code is: 161803"}}
        return resp

    group = Group.objects.get(pk=opts.group.pk)
    with mock.patch("mojo.apps.phonehub.services.mojo_provider.requests.post",
                    side_effect=fake_post):
        sms = send_sms("+14155550199", "Your verification code is: 161803",
                       group=group, secret="161803", metadata={"source": PREFIX})

    assert captured["json"].get("secret") == "161803", "the relay payload must carry the secret"
    assert captured["json"]["body"] == "Your verification code is: 161803", \
        "the relay must be sent the full text"
    assert sms.metadata["remote"] == {"id": 4242, "status": "sent", "from_number": "+18005550100"}, \
        f"metadata['remote'] must keep only id, status and from_number, got {sms.metadata['remote']!r}"
    assert "161803" not in sms.body and "161803" not in str(sms.metadata), \
        "neither the body nor the metadata may keep the code"
