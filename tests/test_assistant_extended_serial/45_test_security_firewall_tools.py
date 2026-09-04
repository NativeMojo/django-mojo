"""Assistant firewall projections that replace a shared reconciler.

These tests are opt-in and serial because their exact success/error paths
replace the incident reconciliation service around in-process model calls.
"""
from unittest import mock

from testit import helpers as th
from testit.helpers import assert_eq, assert_true


TEST_EMAIL_ADMIN = "asst-firewall-tools-admin@example.com"
TEST_PASSWORD = "TestPass1!"
TEST_IP = "198.51.100.42"
TEST_IP_2 = "198.51.100.43"


@th.django_unit_setup()
@th.requires_app("mojo.apps.assistant")
def setup_security_firewall_tools(opts):
    from mojo.apps.account.models import GeoLocatedIP, User

    User.objects.filter(email=TEST_EMAIL_ADMIN).delete()
    GeoLocatedIP.objects.filter(
        ip_address__in=[TEST_IP, TEST_IP_2]).delete()
    opts.admin = User.objects.create_user(
        username=TEST_EMAIL_ADMIN, email=TEST_EMAIL_ADMIN,
        password=TEST_PASSWORD)
    opts.admin.is_email_verified = True
    opts.admin.save()
    opts.admin.add_permission("manage_security")
    opts.geo_ip = GeoLocatedIP.objects.create(ip_address=TEST_IP)


@th.django_unit_test()
def test_unblock_ip(opts):
    """unblock_ip should unblock a blocked IP."""
    from mojo.apps.assistant.services.tools.security import _tool_unblock_ip

    geo = opts.geo_ip
    geo.is_blocked = True
    geo.blocked_reason = "test block"
    geo.save(update_fields=["is_blocked", "blocked_reason"])
    with mock.patch(
            "mojo.apps.incident.services.firewall_truth.reconcile_geolocated_ip",
            return_value={"status": "verified", "ok": True}):
        result = _tool_unblock_ip(
            {"ip": TEST_IP, "reason": "test unblock"}, opts.admin)
    assert_true(result.get("ok"), f"Should succeed, got {result}")
    assert_eq(result["is_blocked"], False, "Should report unblocked")
    geo.refresh_from_db()
    assert_true(not geo.is_blocked, "IP should be unblocked in DB")


@th.django_unit_test("partial firewall result fails the approval-shaped contract")
def test_block_ip_partial_error_contract(opts):
    from mojo.apps.assistant.services.tools.security import _tool_block_ip

    with mock.patch(
            "mojo.apps.incident.services.firewall_truth.reconcile_geolocated_ip",
            return_value={"status": "partial", "ok": False,
                          "error": {"code": "missing_host"}}):
        result = _tool_block_ip(
            {"ip": TEST_IP_2, "reason": "test partial", "ttl": 600},
            opts.admin)
    assert_eq(result.get("error_code"), "missing_host",
              f"approval failure code was not preserved: {result!r}")
    assert_true("error" in result and result.get("ok") is False,
                "partial result could complete an Assistant approval")


@th.django_unit_test()
def test_whitelist_ip(opts):
    """whitelist_ip should whitelist an IP."""
    from mojo.apps.account.models import GeoLocatedIP
    from mojo.apps.assistant.services.tools.security import _tool_whitelist_ip

    with mock.patch(
            "mojo.apps.incident.services.firewall_truth.reconcile_geolocated_ip",
            return_value={"status": "verified", "ok": True}):
        result = _tool_whitelist_ip(
            {"ip": TEST_IP_2, "reason": "trusted office"}, opts.admin)
    assert_true(result.get("ok"), f"Should succeed, got {result}")
    assert_true(result["is_whitelisted"], "Should report whitelisted")
    geo = GeoLocatedIP.objects.get(ip_address=TEST_IP_2)
    assert_true(geo.is_whitelisted, "IP should be whitelisted in DB")


@th.django_unit_test()
def test_unwhitelist_ip(opts):
    """unwhitelist_ip should remove whitelist status."""
    from mojo.apps.account.models import GeoLocatedIP
    from mojo.apps.assistant.services.tools.security import _tool_unwhitelist_ip

    geo, _ = GeoLocatedIP.objects.get_or_create(ip_address=TEST_IP_2)
    geo.is_whitelisted = True
    geo.save(update_fields=["is_whitelisted"])
    with mock.patch(
            "mojo.apps.incident.services.firewall_truth.reconcile_geolocated_ip",
            return_value={"status": "verified", "ok": True}):
        result = _tool_unwhitelist_ip({"ip": TEST_IP_2}, opts.admin)
    assert_true(result.get("ok"), f"Should succeed, got {result}")
    assert_eq(result["is_whitelisted"], False, "Should report not whitelisted")
    geo.refresh_from_db()
    assert_true(not geo.is_whitelisted, "IP should not be whitelisted in DB")


@th.django_unit_test("Assistant unwhitelist refuses partial firewall truth")
def test_unwhitelist_ip_partial_result(opts):
    from mojo.apps.account.models import GeoLocatedIP
    from mojo.apps.assistant.services.tools.security import _tool_unwhitelist_ip

    geo, _ = GeoLocatedIP.objects.get_or_create(ip_address=TEST_IP_2)
    geo.is_whitelisted = True
    geo.save(update_fields=["is_whitelisted"])
    with mock.patch(
            "mojo.apps.incident.services.firewall_truth.reconcile_geolocated_ip",
            return_value={"status": "partial", "ok": False,
                          "error": {"code": "missing_host"}}):
        result = _tool_unwhitelist_ip({"ip": TEST_IP_2}, opts.admin)
    assert_true(result.get("ok") is False, result)
    assert_eq(result.get("error_code"), "missing_host", result)
    assert_eq(result.get("enforcement_status"), "partial", result)
