"""Tests for the mojo SMS provider.

Covers SMS.send() dispatch when PhoneConfig.provider='mojo', including
success, HTTP errors, timeouts, missing config, the +1555 test-number
short-circuit, secrets round-trip / graph non-exposure, the twilio
regression path, and PhoneConfig.test_connection() for the mojo branch.

All tests patch the `requests` module inside the provider service so no
network calls are made.
"""
from unittest import mock
import requests as _requests
from testit import helpers as th


TEST_GROUP_NAME = "test_sms_mojo_group"
REMOTE_URL = "https://sms-hub.example.com"
API_KEY = "test-apikey-token"


def _mock_post_response(status_code=200, body=None, raise_json=False):
    """Build a fake requests.Response-like mock."""
    resp = mock.MagicMock()
    resp.status_code = status_code
    if raise_json:
        resp.json.side_effect = ValueError("not JSON")
    else:
        resp.json.return_value = body if body is not None else {}
    resp.text = "" if body is None else str(body)
    return resp


@th.django_unit_setup()
def setup_sms_mojo_testing(opts):
    """Create a fresh group + PhoneConfig for the mojo provider tests."""
    from mojo.apps.account.models import Group
    from mojo.apps.phonehub.models import PhoneConfig, SMS

    # Clean prior runs first — tests run on long-lived databases.
    PhoneConfig.objects.filter(name__startswith="test_sms_mojo_").delete()
    Group.objects.filter(name=TEST_GROUP_NAME).delete()
    SMS.objects.filter(body__startswith="test_sms_mojo:").delete()

    opts.group = Group.objects.create(name=TEST_GROUP_NAME, kind="organization")

    # Per-group mojo config (system default config, if any, is irrelevant here)
    cfg = PhoneConfig(
        group=opts.group,
        name="test_sms_mojo_cfg",
        provider="mojo",
        mojo_remote_url=REMOTE_URL,
    )
    cfg.set_mojo_api_key(API_KEY)
    cfg.save()
    opts.config_id = cfg.id


@th.django_unit_test()
def test_send_mojo_provider_success(opts):
    """Success path: POST returns 2xx with status=true, row marked sent, remote payload captured."""
    from mojo.apps.account.models import Group
    from mojo.apps.phonehub import send_sms

    group = Group.objects.get(pk=opts.group.pk)
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None, allow_redirects=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        captured["timeout"] = timeout
        captured["allow_redirects"] = allow_redirects
        return _mock_post_response(
            status_code=200,
            body={
                "status": True,
                "data": {
                    "id": 9876,
                    "provider_message_id": "SM_remote_xyz",
                    "status": "sent",
                    "from_number": "+18005550100",
                },
            },
        )

    with mock.patch(
        "mojo.apps.phonehub.services.mojo_provider.requests.post",
        side_effect=fake_post,
    ):
        sms = send_sms("+14155551234", "test_sms_mojo:hello", group=group)

    assert sms is not None, "send_sms must return an SMS instance"
    assert sms.status == "sent", f"expected status='sent', got {sms.status!r}"
    assert sms.provider == "mojo", f"provider must be 'mojo', got {sms.provider!r}"
    assert sms.provider_message_id == "SM_remote_xyz", (
        f"provider_message_id should be the remote provider_message_id, "
        f"got {sms.provider_message_id!r}"
    )
    assert sms.from_number == "+18005550100", (
        f"from_number should be populated from remote echo, got {sms.from_number!r}"
    )
    md = sms.metadata or {}
    assert "remote" in md, f"metadata['remote'] must capture remote payload, got {md!r}"
    assert md["remote"].get("id") == 9876, (
        f"remote payload should be preserved verbatim, got {md['remote']!r}"
    )

    # Verify the HTTP call was made correctly
    assert captured["url"] == f"{REMOTE_URL}/api/phonehub/sms/send", (
        f"wrong URL: {captured.get('url')!r}"
    )
    assert captured["headers"]["Authorization"] == f"apikey {API_KEY}", (
        f"wrong Authorization header: {captured['headers']!r}"
    )
    assert captured["json"]["to_number"] == "+14155551234", (
        f"to_number not forwarded correctly: {captured['json']!r}"
    )
    assert captured["json"]["body"] == "test_sms_mojo:hello", (
        f"body not forwarded correctly: {captured['json']!r}"
    )
    # SSRF mitigation: outbound POST must NOT follow redirects (a redirect to
    # an internal address could otherwise widen the SSRF surface).
    assert captured["allow_redirects"] is False, (
        f"requests.post must be called with allow_redirects=False, "
        f"got {captured['allow_redirects']!r}"
    )


@th.django_unit_test()
def test_send_mojo_provider_http_401(opts):
    """HTTP 401 from remote → row marked failed with error_code='http_401'."""
    from mojo.apps.account.models import Group
    from mojo.apps.phonehub import send_sms

    group = Group.objects.get(pk=opts.group.pk)

    fake_resp = _mock_post_response(status_code=401)
    fake_resp.text = '{"error": "invalid api key"}'
    with mock.patch(
        "mojo.apps.phonehub.services.mojo_provider.requests.post",
        return_value=fake_resp,
    ):
        sms = send_sms("+14155551235", "test_sms_mojo:401", group=group)

    assert sms.status == "failed", f"expected failed on 401, got {sms.status!r}"
    assert sms.error_code == "http_401", (
        f"expected error_code='http_401', got {sms.error_code!r}"
    )
    assert sms.provider == "mojo", f"provider must be 'mojo', got {sms.provider!r}"
    assert sms.error_message, "error_message must be populated on failure"


@th.django_unit_test()
def test_send_mojo_provider_timeout(opts):
    """requests.Timeout → row marked failed with error_code='timeout'."""
    from mojo.apps.account.models import Group
    from mojo.apps.phonehub import send_sms

    group = Group.objects.get(pk=opts.group.pk)

    with mock.patch(
        "mojo.apps.phonehub.services.mojo_provider.requests.post",
        side_effect=_requests.Timeout("timed out"),
    ):
        sms = send_sms("+14155551236", "test_sms_mojo:timeout", group=group)

    assert sms.status == "failed", f"expected failed on timeout, got {sms.status!r}"
    assert sms.error_code == "timeout", (
        f"expected error_code='timeout', got {sms.error_code!r}"
    )
    assert sms.provider == "mojo", f"provider must be 'mojo', got {sms.provider!r}"


@th.django_unit_test()
def test_send_mojo_provider_config_error(opts):
    """Missing mojo_api_key → row marked failed with error_code='config_error' and NO HTTP call."""
    from mojo.apps.account.models import Group
    from mojo.apps.phonehub.models import PhoneConfig
    from mojo.apps.phonehub import send_sms

    group = Group.objects.get(pk=opts.group.pk)

    # Wipe the api key on the config to simulate misconfiguration
    cfg = PhoneConfig.objects.get(pk=opts.config_id)
    cfg.clear_secrets()
    cfg.save()

    with mock.patch(
        "mojo.apps.phonehub.services.mojo_provider.requests.post",
    ) as m_post:
        sms = send_sms("+14155551237", "test_sms_mojo:noapikey", group=group)

    assert sms.status == "failed", (
        f"expected failed when api key missing, got {sms.status!r}"
    )
    assert sms.error_code == "config_error", (
        f"expected error_code='config_error', got {sms.error_code!r}"
    )
    assert sms.provider == "mojo", f"provider must be 'mojo', got {sms.provider!r}"
    assert not m_post.called, (
        "requests.post MUST NOT be called when config is incomplete — "
        "config errors must short-circuit before any HTTP work"
    )

    # Restore for any later tests in the same run
    cfg.set_mojo_api_key(API_KEY)
    cfg.save()


@th.django_unit_test()
def test_send_twilio_path_unchanged(opts):
    """Regression: no PhoneConfig for group → existing twilio path runs, mojo_provider NOT called."""
    from mojo.apps.account.models import Group
    from mojo.apps.phonehub.models import PhoneConfig
    from mojo.apps.phonehub import send_sms
    from objict import objict

    # Create a separate group with NO PhoneConfig so SMS.send falls through
    # to the legacy twilio path.
    other_name = "test_sms_mojo_other_group"
    Group.objects.filter(name=other_name).delete()
    PhoneConfig.objects.filter(group__name=other_name).delete()
    # Also wipe any system-default PhoneConfig that would otherwise be picked up.
    sys_defaults_before = list(
        PhoneConfig.objects.filter(group__isnull=True, is_active=True).values_list("id", flat=True)
    )
    PhoneConfig.objects.filter(group__isnull=True).update(is_active=False)

    try:
        other = Group.objects.create(name=other_name, kind="organization")

        twilio_resp = objict({
            "sent": True, "id": "SM_twilio_regression",
            "status": "queued", "code": None, "error": None,
        })

        with mock.patch(
            "mojo.apps.phonehub.services.twilio.send_sms",
            return_value=twilio_resp,
        ) as m_twilio, \
             mock.patch(
                "mojo.apps.phonehub.services.twilio.get_from_number",
                return_value="+18005559999",
             ), \
             mock.patch(
                "mojo.apps.phonehub.services.mojo_provider.requests.post",
             ) as m_mojo:
            sms = send_sms("+14155551238", "test_sms_mojo:twilio_regression", group=other)

        assert sms.status == "sent", (
            f"twilio path should succeed when mocked, got {sms.status!r} / "
            f"{sms.error_code!r} / {sms.error_message!r}"
        )
        assert sms.provider == "twilio", (
            f"provider must be 'twilio' on the regression path, got {sms.provider!r}"
        )
        assert sms.provider_message_id == "SM_twilio_regression", (
            f"expected twilio mock id, got {sms.provider_message_id!r}"
        )
        assert m_twilio.called, "twilio.send_sms must be called when no mojo config exists"
        assert not m_mojo.called, (
            "mojo_provider.requests.post must NOT be called when provider is not 'mojo'"
        )
    finally:
        # Restore system-default PhoneConfig active flags
        if sys_defaults_before:
            PhoneConfig.objects.filter(pk__in=sys_defaults_before).update(is_active=True)


@th.django_unit_test()
def test_send_mojo_provider_test_number_short_circuit(opts):
    """+1555… number with provider='mojo' → handled locally, no HTTP call."""
    from mojo.apps.account.models import Group
    from mojo.apps.phonehub import send_sms

    group = Group.objects.get(pk=opts.group.pk)

    with mock.patch(
        "mojo.apps.phonehub.services.mojo_provider.requests.post",
    ) as m_post:
        sms = send_sms("+15551234567", "test_sms_mojo:fakenum", group=group)

    assert sms.status == "sent", (
        f"+1555… should be marked sent locally, got {sms.status!r}"
    )
    assert sms.is_test is True, (
        f"+1555… SMS must have is_test=True, got is_test={sms.is_test}"
    )
    assert not m_post.called, (
        "Test-number short-circuit must NOT make any HTTP call to the remote"
    )


@th.django_unit_test()
def test_mojo_api_key_secret_roundtrip(opts):
    """set/get round-trips the api key; secret never appears in default/full graph output."""
    from mojo.apps.phonehub.models import PhoneConfig

    cfg = PhoneConfig.objects.get(pk=opts.config_id)
    assert cfg.get_mojo_api_key() == API_KEY, (
        f"set/get round-trip failed: got {cfg.get_mojo_api_key()!r}"
    )

    # Default and full graphs already exclude mojo_secrets — confirm the new
    # secret rides on that exclusion and never leaks into serialized output.
    for graph_name in ("default", "full"):
        d = cfg.to_dict(graph=graph_name)
        flat = repr(d)
        assert API_KEY not in flat, (
            f"API key MUST NOT appear in '{graph_name}' graph serialization. "
            f"Output: {flat[:300]}..."
        )
        assert "mojo_secrets" not in d, (
            f"'mojo_secrets' field must not appear in '{graph_name}' graph: {d}"
        )


@th.django_unit_test()
def test_phone_config_test_connection_mojo(opts):
    """PhoneConfig.test_connection() mojo branch — success on 200, invalid_credentials on 401."""
    from mojo.apps.phonehub.models import PhoneConfig

    cfg = PhoneConfig.objects.get(pk=opts.config_id)

    # Success path
    fake_ok = mock.MagicMock()
    fake_ok.status_code = 200
    with mock.patch(
        "mojo.apps.phonehub.models.config.requests.get",
        return_value=fake_ok,
    ):
        result = cfg.test_connection()
    assert result["success"] is True, (
        f"test_connection should succeed on 200, got {result!r}"
    )

    # 401 / invalid credentials
    fake_unauthed = mock.MagicMock()
    fake_unauthed.status_code = 401
    with mock.patch(
        "mojo.apps.phonehub.models.config.requests.get",
        return_value=fake_unauthed,
    ):
        result = cfg.test_connection()
    assert result["success"] is False, (
        f"test_connection should fail on 401, got {result!r}"
    )
    assert result["error"] == "invalid_credentials", (
        f"expected error='invalid_credentials' on 401, got {result.get('error')!r}"
    )

    # Missing credentials short-circuit (no HTTP call)
    cfg2 = PhoneConfig(
        group=None, name="test_sms_mojo_noapikey",
        provider="mojo", mojo_remote_url=REMOTE_URL,
    )
    # Note: not saving — we only need test_connection() to read instance state.
    with mock.patch(
        "mojo.apps.phonehub.models.config.requests.get",
    ) as m_get:
        result = cfg2.test_connection()
    assert result["success"] is False, (
        f"test_connection must fail when api key missing, got {result!r}"
    )
    assert result["error"] == "missing_credentials", (
        f"expected error='missing_credentials', got {result.get('error')!r}"
    )
    assert not m_get.called, (
        "test_connection must NOT call requests.get when credentials are missing"
    )
