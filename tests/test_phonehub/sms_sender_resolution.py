"""Regression: SMS.send resolves the Twilio sender and credentials atomically.

PhoneConfig.twilio_from_number used to be dead code — SMS.send() resolved the
sender from settings.TWILIO_NUMBER and never read the model field, and the
stored twilio_account_sid / twilio_auth_token secrets were never passed to the
provider call. Making the number real without making the credentials real in
the same decision would let a non-default Twilio subaccount send a foreign
number with the wrong account's keys (Twilio error 21606).

The resolution contract (D2, maestro #2189), anchored on the credential pair:

* config supplies BOTH credentials -> config owns the whole triple; the
  number must also come from the config, else config_error.
* config supplies NEITHER credential -> settings own the credentials; the
  number falls back config.twilio_from_number then settings.TWILIO_NUMBER.
* config supplies EXACTLY ONE credential -> config_error, no provider call.

All provider traffic is patched at twilio._send_sms, so no network calls are
made and the exact triple the provider would receive is inspected.
"""
from unittest import mock

from testit import helpers as th


GROUP_NAME = "test_sms_resolution_group"
BODY_PREFIX = "test_sms_resolution:"
CONFIG_NAME = "test_sms_resolution_cfg"
CONFIG_FROM = "+18885551000"
CONFIG_SID = "ACtest_config_sid"
CONFIG_TOKEN = "test_config_token"
# Must NOT start with +1555 or SMS.send short-circuits before the provider.
TO_NUMBER = "+14155551234"


def _sent_response():
    from objict import objict
    return objict(sent=True, id="SMtest", status="sent", code=None, error=None)


@th.django_unit_setup()
def setup_sender_resolution(opts):
    """Fresh group + twilio PhoneConfig; clean prior runs first."""
    from mojo.apps.account.models import Group
    from mojo.apps.phonehub.models import PhoneConfig, SMS

    SMS.objects.filter(body__startswith=BODY_PREFIX).delete()
    PhoneConfig.objects.filter(name=CONFIG_NAME).delete()
    Group.objects.filter(name=GROUP_NAME).delete()

    opts.group = Group.objects.create(name=GROUP_NAME, kind="organization")
    config = PhoneConfig(
        group=opts.group, name=CONFIG_NAME, provider="twilio",
        twilio_from_number=CONFIG_FROM, is_active=True)
    config.save()
    opts.config_id = config.id


def _reset_config(opts, from_number, account_sid, auth_token):
    from mojo.apps.phonehub.models import PhoneConfig
    config = PhoneConfig.objects.get(pk=opts.config_id)
    config.twilio_from_number = from_number
    config.set_secret("twilio_account_sid", account_sid)
    config.set_secret("twilio_auth_token", auth_token)
    config.save()
    return config


@th.django_unit_test("config with number + both credentials sends all three from the config")
def test_send_resolves_number_and_credentials_atomically(opts):
    from mojo.apps.account.models import Group
    from mojo.apps.phonehub.models import SMS

    _reset_config(opts, CONFIG_FROM, CONFIG_SID, CONFIG_TOKEN)
    group = Group.objects.get(pk=opts.group.pk)

    with mock.patch(
            "mojo.apps.phonehub.services.twilio._send_sms",
            return_value=_sent_response()) as sender:
        sms = SMS.send(f"{BODY_PREFIX}full-config", TO_NUMBER, group=group)

    assert sms.status == "sent", (
        f"send must succeed with a fully configured twilio config, got "
        f"status={sms.status!r} error={sms.error_message!r}")
    assert sender.call_count == 1, (
        f"exactly one provider call expected, got {sender.call_count}")
    args = sender.call_args[0]
    # _send_sms(body, to_number, from_number, account_sid, auth_token)
    assert args[2] == CONFIG_FROM, (
        f"from_number must come from PhoneConfig.twilio_from_number, "
        f"got {args[2]!r} expected {CONFIG_FROM!r}")
    assert args[3] == CONFIG_SID, (
        f"account_sid must come from the config's stored secret, got {args[3]!r}")
    assert args[4] == CONFIG_TOKEN, (
        f"auth_token must come from the config's stored secret, got {args[4]!r}")
    assert sms.from_number == CONFIG_FROM, (
        f"the SMS row must record the config sender, got {sms.from_number!r}")


@th.django_unit_test("config number with no credentials falls back to settings credentials")
def test_send_config_number_with_settings_credentials(opts):
    from mojo.apps.account.models import Group
    from mojo.apps.phonehub.models import SMS
    from mojo.helpers.settings import settings

    _reset_config(opts, CONFIG_FROM, None, None)
    group = Group.objects.get(pk=opts.group.pk)

    with mock.patch(
            "mojo.apps.phonehub.services.twilio._send_sms",
            return_value=_sent_response()) as sender:
        sms = SMS.send(f"{BODY_PREFIX}config-number-only", TO_NUMBER, group=group)

    assert sms.status == "sent", (
        f"send must succeed, got status={sms.status!r} error={sms.error_message!r}")
    assert sender.call_count == 1, (
        f"exactly one provider call expected, got {sender.call_count}")
    args = sender.call_args[0]
    assert args[2] == CONFIG_FROM, (
        f"from_number must come from PhoneConfig.twilio_from_number even "
        f"when settings own the credentials, got {args[2]!r}")
    assert args[3] == settings.get("TWILIO_ACCOUNT_SID"), (
        f"account_sid must fall back to settings when the config stores "
        f"neither credential, got {args[3]!r}")
    assert args[4] == settings.get("TWILIO_AUTH_TOKEN"), (
        f"auth_token must fall back to settings when the config stores "
        f"neither credential, got {args[4]!r}")


@th.django_unit_test("a half-supplied credential pair fails config_error with no provider call")
def test_send_half_credential_pair_never_mixes(opts):
    from mojo.apps.account.models import Group
    from mojo.apps.phonehub.models import SMS

    _reset_config(opts, CONFIG_FROM, CONFIG_SID, None)
    group = Group.objects.get(pk=opts.group.pk)

    with mock.patch(
            "mojo.apps.phonehub.services.twilio._send_sms",
            return_value=_sent_response()) as sender:
        sms = SMS.send(f"{BODY_PREFIX}half-pair", TO_NUMBER, group=group)

    assert sender.call_count == 0, (
        "a half-supplied credential pair must never reach the provider — "
        f"mixing accounts is the exact failure D2 forbids, got "
        f"{sender.call_count} call(s)")
    assert sms.status == "failed", (
        f"the SMS row must be marked failed, got {sms.status!r}")
    assert sms.error_code == "config_error", (
        f"error_code must be config_error, got {sms.error_code!r}")


@th.django_unit_test("sender resolution cleanup")
def test_cleanup(opts):
    from mojo.apps.account.models import Group
    from mojo.apps.phonehub.models import PhoneConfig, SMS

    SMS.objects.filter(body__startswith=BODY_PREFIX).delete()
    PhoneConfig.objects.filter(name=CONFIG_NAME).delete()
    Group.objects.filter(name=GROUP_NAME).delete()
