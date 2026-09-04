"""Durable checked GeoLocatedIP firewall reconciliation regressions."""

import datetime
from unittest import mock

from testit import helpers as th


TEST_IP = "198.51.100.99"


def _verified():
    return {"status": "verified", "ok": True}


@th.django_unit_setup()
def setup_ipset_blocks(opts):
    from mojo.apps.account.models import GeoLocatedIP

    GeoLocatedIP.objects.filter(
        ip_address__in=(TEST_IP, "2001:db8::99")).delete()
    opts.geo = GeoLocatedIP.objects.create(
        ip_address=TEST_IP, country_code="US")


@th.django_unit_test("permanent checked block reconciles aggregate and absence of direct rule")
def test_permanent_block_checked(opts):
    from mojo.apps.account.models import GeoLocatedIP

    geo = GeoLocatedIP.objects.get(ip_address=TEST_IP)
    with mock.patch(
            "mojo.apps.incident.services.firewall_truth.reconcile_geolocated_ip",
            return_value=_verified()) as reconcile:
        result = geo.block_checked(reason="test", ttl=None)
    assert result["status"] == "verified", f"checked block failed: {result!r}"
    args = reconcile.call_args.args
    assert args[0] == TEST_IP and TEST_IP in args[1] and args[2] is False, \
        f"permanent desired state was not exact: {reconcile.call_args!r}"
    geo.refresh_from_db()
    assert geo.firewall_pending is False and geo.firewall_observed_at is not None, \
        "verified observation did not clear the durable pending marker"


@th.django_unit_test("TTL checked block reconciles direct presence")
def test_ttl_block_checked(opts):
    from mojo.apps.account.models import GeoLocatedIP

    geo = GeoLocatedIP.objects.get(ip_address=TEST_IP)
    with mock.patch(
            "mojo.apps.incident.services.firewall_truth.reconcile_geolocated_ip",
            return_value=_verified()) as reconcile:
        result = geo.block_checked(reason="test", ttl=600)
    assert result["status"] == "verified", f"TTL block failed: {result!r}"
    assert reconcile.call_args.args[2] is True, \
        "TTL block did not request exact direct-rule presence"


@th.django_unit_test("unblock persists an absence tombstone until verified")
def test_unblock_checked_tombstone(opts):
    from mojo.apps.account.models import GeoLocatedIP

    geo = GeoLocatedIP.objects.get(ip_address=TEST_IP)
    geo.is_blocked = True
    geo.blocked_reason = "test"
    geo.save(update_fields=["is_blocked", "blocked_reason"])
    with mock.patch(
            "mojo.apps.incident.services.firewall_truth.reconcile_geolocated_ip",
            return_value={"status": "partial", "ok": False,
                          "error": {"code": "missing_host"}}) as reconcile:
        result = geo.unblock_checked(reason="operator")
    assert result["status"] == "partial", f"partial truth was lost: {result!r}"
    assert reconcile.call_args.args[2] is False, "unblock did not reconcile direct absence"
    geo.refresh_from_db()
    assert geo.is_blocked is False and geo.firewall_pending is True, \
        "partial unblock cleared its durable absence tombstone"
    assert "missing_host" in geo.firewall_sync_error, \
        "bounded failure code was not persisted"


@th.django_unit_test("already desired blocks are re-observed without recounting")
def test_idempotent_block_reobserves(opts):
    from mojo.apps.account.models import GeoLocatedIP
    from mojo.helpers import dates

    geo = GeoLocatedIP.objects.get(ip_address=TEST_IP)
    geo.is_blocked = True
    geo.blocked_until = dates.utcnow() + datetime.timedelta(minutes=5)
    geo.block_count = 1
    geo.save(update_fields=["is_blocked", "blocked_until", "block_count"])
    with mock.patch(
            "mojo.apps.incident.services.firewall_truth.reconcile_geolocated_ip",
            return_value=_verified()) as reconcile:
        result = geo.block_checked(reason="new reason", ttl=900)
    reconcile.assert_called_once()
    geo.refresh_from_db()
    assert result["outcome"] == "pre_existing" and geo.block_count == 1, \
        "idempotent verification changed desired block ownership"


@th.django_unit_test("superseded receipt cannot clear pending or emit success")
def test_block_generation_cas_suppresses_success(opts):
    from mojo.apps.account.models import GeoLocatedIP

    geo = GeoLocatedIP.objects.get(ip_address=TEST_IP)

    def supersede(*unused_args):
        GeoLocatedIP.objects.filter(pk=geo.pk).update(
            firewall_generation=99, firewall_pending=True)
        return _verified()

    with mock.patch(
            "mojo.apps.incident.services.firewall_truth.reconcile_geolocated_ip",
            side_effect=supersede), \
            mock.patch.object(geo, "log") as success_log, \
            mock.patch("mojo.apps.account.models.geolocated_ip.metrics.record") \
            as success_metric:
        result = geo.block_checked(reason="superseded", ttl=600)
    geo.refresh_from_db()
    assert result["status"] == "partial" and result["owned"] is False, result
    assert geo.firewall_generation == 99 and geo.firewall_pending is True
    success_log.assert_not_called()
    success_metric.assert_not_called()


@th.django_unit_test("legacy broadcast false remains a bool and leaves durable pending state")
def test_legacy_block_broadcast_false(opts):
    from mojo.apps.account.models import GeoLocatedIP

    geo = GeoLocatedIP.objects.get(ip_address=TEST_IP)
    with mock.patch(
            "mojo.apps.incident.services.firewall_truth.reconcile_geolocated_ip") as reconcile:
        result = geo.block(reason="db only", ttl=None, broadcast=False)
    assert result is True, "legacy block no longer returned bool success"
    reconcile.assert_not_called()
    geo.refresh_from_db()
    assert geo.firewall_pending is True, \
        "broadcast=False desired state was not left for later reconciliation"


@th.django_unit_test("IPv6 is refused before desired-state mutation")
def test_ipv6_refused_before_desired_write(opts):
    from mojo.apps.account.models import GeoLocatedIP

    geo = GeoLocatedIP.objects.create(ip_address="2001:db8::99")
    result = geo.block_checked(reason="unsupported", ttl=600)
    geo.refresh_from_db()
    assert result["error"]["code"] == "unsupported_family", result
    assert geo.is_blocked is False and geo.firewall_generation == 0, \
        "IPv6 refusal occurred after desired state changed"


@th.django_unit_test("whitelist proves absence even when DB was unblocked")
def test_whitelist_reconciles_absence(opts):
    from mojo.apps.account.models import GeoLocatedIP

    ip = "198.51.100.89"
    GeoLocatedIP.objects.filter(ip_address=ip).delete()
    geo = GeoLocatedIP.objects.create(ip_address=ip)
    partial = {"status": "partial", "ok": False,
               "error": {"code": "missing_host"}}
    with mock.patch(
            "mojo.apps.incident.services.firewall_truth.reconcile_geolocated_ip",
            return_value=partial) as reconcile:
        result = geo.whitelist(reason="trusted")
    geo.refresh_from_db()
    reconcile.assert_called_once()
    assert result["status"] == "partial" and result["ok"] is False, result
    assert geo.is_whitelisted and geo.firewall_pending, \
        "desired whitelist or pending reconciliation was lost"


@th.django_unit_test("generic GeoLocatedIP writes cannot forge firewall truth")
def test_reconciliation_fields_are_read_only(opts):
    from mojo.apps.account.models import GeoLocatedIP

    protected = {
        "is_blocked", "blocked_until", "is_whitelisted",
        "firewall_generation", "firewall_pending", "firewall_sync_error",
        "firewall_observed_at",
    }
    assert protected <= set(GeoLocatedIP.RestMeta.NO_SAVE_FIELDS), \
        "generic REST writes can bypass checked reconciliation"


@th.django_unit_test("expired sweep only reports checked absences")
def test_expired_sweep_truthful_count(opts):
    from mojo.apps.account.models import GeoLocatedIP
    from mojo.apps.incident.asyncjobs import sweep_expired_blocks
    from mojo.helpers import dates
    from objict import objict

    geo = GeoLocatedIP.objects.get(ip_address=TEST_IP)
    geo.is_blocked = True
    geo.blocked_until = dates.utcnow() - datetime.timedelta(seconds=1)
    geo.save(update_fields=["is_blocked", "blocked_until"])
    job = objict(logs=[])
    job.add_log = job.logs.append
    with mock.patch.object(
            GeoLocatedIP, "unblock_checked",
            return_value={"status": "partial", "ok": False,
                          "error": {"code": "missing_host"}}):
        result = sweep_expired_blocks(job)
    assert result == {"verified": 0, "unverified": 1}, \
        f"partial absence was reported as swept: {result!r}"
