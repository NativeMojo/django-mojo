"""Admin Text messages (SMS) — Twilio credential-resolution contract (D3),
maestro #2189.

Moved out of tests/test_phonehub/admin_sms.py (maestro item #1839): this test
overrides django.conf.settings in-process via setattr/delattr, which is unsafe
under the parallel default tier. The rest of the Admin Messaging coverage
stays in the source module.

Every provider contact is mocked — no network calls are made.
"""
from contextlib import contextmanager
from unittest import mock

from testit import helpers as th


@contextmanager
def _override_setting(name, value):
    """In-process Django settings override (th.server_settings only affects
    the separate server process; override_settings is banned)."""
    import django.conf
    sentinel = object()
    original = getattr(django.conf.settings, name, sentinel)
    setattr(django.conf.settings, name, value)
    try:
        yield
    finally:
        if original is sentinel:
            delattr(django.conf.settings, name)
        else:
            setattr(django.conf.settings, name, original)


@th.django_unit_test("the Twilio connection test validates the pair send() would use")
def test_twilio_connection_test_matches_send_resolution(opts):
    from mojo.apps.phonehub.models import PhoneConfig
    from mojo.apps.phonehub.services import twilio as twilio_service

    def _client_capture(captured):
        client = mock.MagicMock()
        def factory(sid, token):
            captured["sid"], captured["token"] = sid, token
            return client
        return factory

    # Both credentials on the config: the test validates the CONFIG pair —
    # the same pair resolve_credentials hands the send path.
    config = PhoneConfig(group=None, provider="twilio", name="candidate",
                         twilio_from_number="+18885550002")
    config.set_secret("twilio_account_sid", "ACconfig")
    config.set_secret("twilio_auth_token", "config-token")
    captured = {}
    with mock.patch("twilio.rest.Client", side_effect=_client_capture(captured)):
        result = config._test_twilio()
    assert result["success"] is True, f"the config pair failed testing: {result}"
    assert captured["sid"] == "ACconfig" and captured["token"] == "config-token", \
        f"the test validated a different pair than the config stores: {captured}"
    resolution = twilio_service.resolve_credentials(config)
    assert (resolution.account_sid, resolution.auth_token) == \
        (captured["sid"], captured["token"]), (
        "the pair the test validated is not the pair send() would use: "
        f"{resolution!r} vs {captured}")

    # NO credentials on the config: settings own the pair, and the test no
    # longer answers missing_credentials for a perfectly sendable setup.
    bare = PhoneConfig(group=None, provider="twilio", name="candidate")
    captured = {}
    with _override_setting("TWILIO_ACCOUNT_SID", "ACsettings"), \
            _override_setting("TWILIO_AUTH_TOKEN", "settings-token"), \
            mock.patch("twilio.rest.Client", side_effect=_client_capture(captured)):
        result = bare._test_twilio()
    assert result["success"] is True and result.get("error") != "missing_credentials", \
        f"settings-only credentials must not be missing_credentials: {result}"
    assert captured["sid"] == "ACsettings" and captured["token"] == "settings-token", \
        f"the test did not validate the settings pair send() would use: {captured}"

    # Exactly one credential: refused without contacting anyone — the same
    # config_error send() raises.
    half = PhoneConfig(group=None, provider="twilio", name="candidate")
    half.set_secret("twilio_account_sid", "AConly")
    with mock.patch("twilio.rest.Client") as client:
        result = half._test_twilio()
    assert result["success"] is False and result["error"] == "missing_credentials", \
        f"a half pair must be missing_credentials: {result}"
    assert client.call_count == 0, \
        "a half-supplied pair must never be sent to the provider"
