"""Admin Text messages (SMS) management — maestro #2189.

Covers the four security-bearing contracts of the Admin Messaging page:

* the SYSTEM PhoneConfig row (group=None) — the row every OTP/MFA/reset SMS
  routes through — is writable only by a literal superuser, even for callers
  holding manage_phone_config + manage_groups + comms (D1);
* connection-test results never carry raw provider exception text, and
  test_mode is a distinct third state, never reported as OK (D4);
* the Settings-page editor contract is byte-identical after the D5 refactor,
  and both editors bump ADMIN_PROVIDER_SETUP_REVISION so a stale session's
  expected_revision fails instead of silently clobbering.

The D3 contract (the Twilio connection test validates the same credential pair
the send path would use) lives in
tests/test_phonehub_extended_serial/admin_sms.py (maestro item #1839) — it
overrides django.conf.settings in-process, which is unsafe under the parallel
default tier.

Every provider contact is either short-circuited (clear_api_key, test_mode,
+1555 test numbers) or mocked — no network calls are made.
"""

TESTIT_TIER = "admin"
from unittest import mock

from testit import helpers as th


SUPER_EMAIL = "sms_admin_super@test.com"
SUPER_PASSWORD = "Sms_admin_super_pw_99"
MANAGER_EMAIL = "sms_admin_manager@test.com"
MANAGER_PASSWORD = "Sms_admin_manager_pw_99"

GROUP_NAME = "test_admin_sms_group"
SYSTEM_NAME = "test_admin_sms_system"
GROUP_CFG_NAME = "test_admin_sms_groupcfg"
INACTIVE_CFG_NAME = "test_admin_sms_inactivecfg"
SYSTEM_URL = "https://sms-admin-test.example.com"
SYSTEM_KEY = "smsadmin-secret-key-1234567890"

SUMMARY_URL = "/api/account/admin/messaging-sms/summary"
MUTATE_URL = "/api/account/admin/messaging-sms"


@th.django_unit_setup()
def setup_admin_sms(opts):
    from mojo.apps.account.models import Group, User
    from mojo.apps.phonehub.models import PhoneConfig, SMS

    # Tests run on long-lived databases — clean prior runs first.
    SMS.objects.filter(metadata__source="admin_sms_test").delete()
    PhoneConfig.objects.filter(name__startswith="test_admin_sms_").delete()
    Group.objects.filter(name=GROUP_NAME).delete()
    User.objects.filter(email__in=[SUPER_EMAIL, MANAGER_EMAIL]).delete()

    superuser = User.objects.create_user(
        username=SUPER_EMAIL, email=SUPER_EMAIL, password=SUPER_PASSWORD)
    superuser.is_active = True
    superuser.is_email_verified = True
    superuser.requires_mfa = False
    superuser.is_superuser = True
    superuser.save()
    opts.superuser_id = superuser.pk

    manager = User.objects.create_user(
        username=MANAGER_EMAIL, email=MANAGER_EMAIL, password=MANAGER_PASSWORD)
    manager.is_active = True
    manager.is_email_verified = True
    manager.requires_mfa = False
    manager.add_permission("manage_phone_config")
    manager.add_permission("manage_groups")
    manager.add_permission("comms")
    manager.save()
    opts.manager_id = manager.pk

    opts.group = Group.objects.create(name=GROUP_NAME, kind="organization")

    system = PhoneConfig(
        group=None, name=SYSTEM_NAME, provider="mojo",
        mojo_remote_url=SYSTEM_URL, is_active=True)
    system.set_mojo_api_key(SYSTEM_KEY)
    system.save()
    opts.system_id = system.pk

    group_cfg = PhoneConfig(
        group=opts.group, name=GROUP_CFG_NAME, provider="twilio",
        twilio_from_number="+18885550001", is_active=True, test_mode=True)
    group_cfg.save()
    opts.group_cfg_id = group_cfg.pk


@th.django_unit_test("summary exposes which secrets are set, never their values")
def test_summary_never_leaks_secret_values(opts):
    assert opts.client.login(SUPER_EMAIL, SUPER_PASSWORD), "superuser login failed"
    import json as _json
    resp = opts.client.get(SUMMARY_URL)
    assert resp.status_code == 200, f"summary failed: {resp.status_code}: {resp.response}"
    assert SYSTEM_KEY not in _json.dumps(resp.json), \
        "the stored mojo_api_key VALUE leaked into the summary payload"
    data = resp.json.get("data") or resp.json
    system = data.get("system")
    assert system is not None, f"summary omitted the system config: {data}"
    assert system["provider"] == "mojo" and system["mojo_remote_url"] == SYSTEM_URL, \
        f"summary misreports the system row: {system}"
    assert system["secrets"]["mojo_api_key"] is True, \
        f"summary does not report the stored key as set: {system['secrets']}"
    assert all(isinstance(v, bool) for v in system["secrets"].values()), \
        f"secrets must be reported as booleans only: {system['secrets']}"
    assert data["capabilities"]["system_write"] is True, \
        "a superuser must be advertised the system write capability"
    opts.client.logout()


@th.django_unit_test("test-SMS action short-circuits +1555 numbers with no provider call")
def test_send_test_reports_test_number(opts):
    assert opts.client.login(SUPER_EMAIL, SUPER_PASSWORD), "superuser login failed"
    resp = opts.client.post(
        MUTATE_URL, {"action": "send_test", "to_number": "+15550009999"})
    assert resp.status_code == 200, f"send_test failed: {resp.status_code}: {resp.response}"
    data = resp.json.get("data") or resp.json
    assert data["test_number"] is True, \
        f"a +1555 recipient must be reported as a test number: {data}"
    assert data["sms"]["status"] == "sent" and data["sms"]["is_test"] is True, \
        f"the test-number short-circuit did not mark the row: {data['sms']}"
    assert "nothing was sent" in data["message"], \
        f"the response does not say nothing was sent: {data['message']!r}"
    opts.client.logout()


@th.django_unit_test("test_connection reports test_mode as a distinct state, not OK")
def test_connection_action_reports_test_mode(opts):
    assert opts.client.login(SUPER_EMAIL, SUPER_PASSWORD), "superuser login failed"
    resp = opts.client.post(
        MUTATE_URL, {"action": "test_connection", "config_id": opts.group_cfg_id})
    assert resp.status_code == 200, f"test_connection failed: {resp.status_code}: {resp.response}"
    data = resp.json.get("data") or resp.json
    assert data["state"] == "test_mode", \
        f"test_mode must be a distinct third state, got {data}"
    assert data["state"] != "ok" and "not contacted" in data["message"], \
        f"test_mode is indistinguishable from a successful contact: {data}"
    opts.client.logout()


@th.tier("core")
@th.django_unit_test("system-row writes require a literal superuser and refuse untouched")
def test_system_row_write_requires_superuser(opts):
    from mojo.apps.phonehub.models import PhoneConfig

    payload = {
        "action": "save", "provider": "mojo",
        "remote_url": "https://sms-hijacked.example.com",
        "clear_api_key": True,
    }

    # A manager holding manage_phone_config + manage_groups + comms clears the
    # decorator tier — the body-level superuser check must still refuse.
    assert opts.client.login(MANAGER_EMAIL, MANAGER_PASSWORD), "manager login failed"
    refused = opts.client.post(MUTATE_URL, payload)
    assert refused.status_code == 403, (
        f"a non-superuser phone-config manager must get 403 from a system-row "
        f"save, got {refused.status_code}: {refused.response}")
    row = PhoneConfig.objects.get(pk=opts.system_id)
    assert row.provider == "mojo" and row.mojo_remote_url == SYSTEM_URL, \
        f"the refused save still changed the system row: {row.mojo_remote_url!r}"
    assert row.get_mojo_api_key() == SYSTEM_KEY, \
        "the refused save still touched the stored mojo_api_key"
    opts.client.logout()

    # The same payload as a superuser succeeds (clear_api_key short-circuits
    # verification exactly as the Settings editor does) and updates the row.
    assert opts.client.login(SUPER_EMAIL, SUPER_PASSWORD), "superuser login failed"
    summary = opts.client.get(SUMMARY_URL)
    assert summary.status_code == 200, summary.response
    summary_data = summary.json.get("data") or summary.json
    payload["expected_revision"] = summary_data.get("configuration_revision")
    accepted = opts.client.post(MUTATE_URL, payload)
    assert accepted.status_code == 200, (
        f"the superuser save failed: {accepted.status_code}: {accepted.response}")
    row = PhoneConfig.objects.get(pk=opts.system_id)
    assert row.mojo_remote_url == "https://sms-hijacked.example.com", \
        f"the superuser save did not update the system row: {row.mojo_remote_url!r}"
    assert row.get_mojo_api_key() == "", \
        "clear_api_key did not clear the stored key"

    # Restore the fixture row for the tests that follow.
    row.mojo_remote_url = SYSTEM_URL
    row.set_mojo_api_key(SYSTEM_KEY)
    row.save()
    opts.client.logout()


@th.django_unit_test("group overrides list only ACTIVE rows — inactive rows do not govern")
def test_group_overrides_exclude_inactive(opts):
    from mojo.apps.phonehub.models import PhoneConfig
    from mojo.apps.phonehub.services import admin_sms

    inactive = PhoneConfig.objects.create(
        group=None, name=INACTIVE_CFG_NAME, provider="twilio", is_active=False)
    # An inactive GROUP override needs its own group (OneToOneField).
    from mojo.apps.account.models import Group
    Group.objects.filter(name=f"{GROUP_NAME}_inactive").delete()
    other = Group.objects.create(name=f"{GROUP_NAME}_inactive", kind="organization")
    PhoneConfig.objects.create(
        group=other, name=f"{INACTIVE_CFG_NAME}_g", provider="twilio",
        is_active=False)

    report = admin_sms.summary()
    ids = [row["id"] for row in report["group_overrides"]["items"]]
    names = [row["name"] for row in report["group_overrides"]["items"]]
    assert opts.group_cfg_id in ids, \
        f"the active group override is missing from the summary: {names}"
    assert f"{INACTIVE_CFG_NAME}_g" not in names, (
        "an INACTIVE group override is shown as governing — get_for_group() "
        f"would never honor it: {names}")

    PhoneConfig.objects.filter(name__startswith=INACTIVE_CFG_NAME).delete()
    other.delete()
    inactive.delete()


@th.django_unit_test("connection-test results carry no raw provider exception text")
def test_connection_result_carries_no_raw_exception(opts):
    from mojo.apps.phonehub.models import PhoneConfig

    marker = "SECRET_LEAK_MARKER_basic_auth_token_9f2c"
    config = PhoneConfig(group=None, provider="twilio", name="candidate")
    config.set_secret("twilio_account_sid", "ACtest")
    config.set_secret("twilio_auth_token", "token")
    with mock.patch("twilio.rest.Client", side_effect=Exception(marker)):
        result = config._test_twilio()
    assert result["success"] is False and result["error"] == "connection_failed", \
        f"a provider exception must diagnose as connection_failed: {result}"
    assert marker not in str(result.get("message")), \
        f"raw exception text leaked into message: {result}"
    assert marker not in str(result.get("details")), \
        f"raw exception text leaked into details: {result}"

    config.provider = "aws"
    config.set_secret("aws_access_key_id", "AKIATEST")
    config.set_secret("aws_secret_access_key", "secret")
    with mock.patch("boto3.client", side_effect=Exception(marker)):
        result = config._test_aws()
    assert result["success"] is False, f"the AWS failure was not reported: {result}"
    assert marker not in str(result.get("message")), \
        f"raw AWS exception text leaked into message: {result}"
    assert marker not in str(result.get("details")), \
        f"raw AWS exception text leaked into details: {result}"


@th.django_unit_test("test_mode is never reported as OK by the service layer")
def test_test_mode_is_not_reported_as_ok(opts):
    from mojo.apps.phonehub.services import admin_sms

    result = admin_sms.test_connection(opts.group_cfg_id)
    assert result["state"] == "test_mode", \
        f"test_mode must be its own state: {result}"
    assert result["message"] == admin_sms.TEST_MODE_MESSAGE, \
        f"test_mode message drifted from the fixed copy: {result['message']!r}"


@th.django_unit_test("Settings editor contract is unchanged and every save bumps the revision")
def test_settings_editor_contract_unchanged(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.services import provider_setup
    from mojo.apps.phonehub.models import PhoneConfig
    from mojo import errors as me

    actor = User.objects.get(pk=opts.superuser_id)
    token_before = provider_setup.sms_revision_token()

    # The Settings page's apply(): forces provider=mojo under its fixed name,
    # deactivates other active system rows, and changes the revision.
    other = PhoneConfig.objects.create(
        group=None, name="test_admin_sms_other", provider="twilio",
        is_active=True)
    result = provider_setup.apply(actor, "sms", {
        "sms": {"remote_url": SYSTEM_URL, "clear_api_key": True},
        "expected_revision": token_before,
    })
    assert result["topic"] == "sms", f"apply misreported its topic: {result}"
    rows = list(PhoneConfig.objects.filter(group=None, is_active=True))
    assert len(rows) == 1, f"apply left an ambiguous active system default: {rows}"
    assert rows[0].provider == "mojo" and rows[0].name == "Mojo Remote SMS", \
        f"the Settings contract drifted: {rows[0].provider!r} {rows[0].name!r}"
    token_after_settings = provider_setup.sms_revision_token()
    assert token_after_settings != token_before, \
        "a Settings-page save did not change ADMIN_PROVIDER_SETUP_REVISION"

    # The Messaging editor's writer bumps it too, so a stale Settings session
    # fails its expected_revision compare instead of silently clobbering.
    result = provider_setup.save_messaging_system_config(
        actor, provider="twilio", name="test_admin_sms_messaging",
        expected_revision=token_after_settings,
        verify_result={"success": True, "code": None,
                       "message": "Connection verified"},
        twilio_from_number="+18885550003", test_mode=False)
    assert result["saved"] is True, f"the Messaging save failed: {result}"
    token_after_messaging = provider_setup.sms_revision_token()
    assert token_after_messaging != token_after_settings, \
        "a Messaging-editor save did not bump ADMIN_PROVIDER_SETUP_REVISION"

    # A session still holding the pre-Messaging token is refused.
    try:
        provider_setup.apply(actor, "sms", {
            "sms": {"remote_url": SYSTEM_URL, "clear_api_key": True},
            "expected_revision": token_after_settings,
        })
        assert False, "a stale expected_revision was accepted after a Messaging save"
    except me.ValueException:
        pass

    PhoneConfig.objects.filter(name="test_admin_sms_other").delete()


@th.django_unit_test("both endpoints degrade to a clean not-installed envelope")
def test_not_installed_envelope(opts):
    from mojo.apps.account.models import User
    from mojo.apps.phonehub.services import admin_sms

    actor = User.objects.get(pk=opts.superuser_id)
    with mock.patch.object(admin_sms, "is_installed", return_value=False):
        report = admin_sms.summary()
        assert report["installed"] is False and report["error_code"] == "not_installed", \
            f"summary did not degrade cleanly: {report}"
        result = admin_sms.test_connection()
        assert result["installed"] is False, f"test_connection did not degrade: {result}"
        result = admin_sms.send_test(actor, "+15550001111")
        assert result["installed"] is False, f"send_test did not degrade: {result}"
        result = admin_sms.save_config(actor, {"action": "save", "provider": "mojo"})
        assert result["installed"] is False, f"save_config did not degrade: {result}"


@th.django_unit_test("admin sms cleanup")
def test_cleanup(opts):
    from mojo.apps.account.models import Group, User
    from mojo.apps.phonehub.models import PhoneConfig, SMS

    SMS.objects.filter(metadata__source="admin_sms_test").delete()
    # apply() renames the effective system row to its fixed Settings name, so
    # the prefix filter alone would miss it — delete the fixture row by pk too.
    PhoneConfig.objects.filter(pk=opts.system_id).delete()
    PhoneConfig.objects.filter(name__startswith="test_admin_sms_").delete()
    Group.objects.filter(name__startswith=GROUP_NAME).delete()
    User.objects.filter(email__in=[SUPER_EMAIL, MANAGER_EMAIL]).delete()
