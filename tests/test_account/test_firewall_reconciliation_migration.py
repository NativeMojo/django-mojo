"""Durable firewall-truth migration backfill regressions."""

import importlib

from django.test import override_settings
from django.utils import timezone
from testit import helpers as th


class _Apps:
    @staticmethod
    def get_model(app_label, model_name):
        if (app_label, model_name) == ("account", "GeoLocatedIP"):
            from mojo.apps.account.models import GeoLocatedIP
            return GeoLocatedIP
        if (app_label, model_name) == ("incident", "IPSet"):
            from mojo.apps.incident.models import IPSet
            return IPSet
        raise LookupError((app_label, model_name))


@th.django_unit_test("0054 backfills every touched row and quarantines per object")
def test_firewall_truth_migration_backfill(opts):
    from mojo.apps.account.models import GeoLocatedIP
    from mojo.apps.incident.models import IPSet

    ips = ("198.51.100.201", "198.51.100.202", "2001:db8::201")
    names = ("migration_valid", "migration_ipv6", "configured_reserved")
    GeoLocatedIP.objects.filter(ip_address__in=ips).delete()
    IPSet.objects.filter(name__in=names).delete()
    touched = GeoLocatedIP.objects.create(
        ip_address=ips[0], blocked_at=timezone.now(), is_blocked=False)
    whitelisted = GeoLocatedIP.objects.create(
        ip_address=ips[1], is_whitelisted=True,
        whitelisted_reason="historical allow")
    unsupported = GeoLocatedIP.objects.create(
        ip_address=ips[2], is_whitelisted=True)
    IPSet.objects.bulk_create([
        IPSet(name=names[0], kind="custom", data="192.0.2.0/24",
              is_enabled=True, last_synced=timezone.now()),
        IPSet(name=names[1], kind="custom", data="2001:db8::/32",
              is_enabled=True, last_synced=timezone.now()),
        IPSet(name=names[2], kind="custom", data="198.51.100.0/24",
              is_enabled=True, last_synced=timezone.now()),
    ])
    migration = importlib.import_module(
        "mojo.apps.account.migrations.0054_geolocatedip_firewall_reconciliation")
    with override_settings(FIREWALL_BLOCKED_IPSET_NAME=names[2]):
        migration.mark_existing_firewall_state_pending(_Apps(), None)

    touched.refresh_from_db()
    whitelisted.refresh_from_db()
    unsupported.refresh_from_db()
    assert touched.firewall_pending and "absence" in touched.firewall_sync_error
    assert whitelisted.firewall_pending and "absence" in whitelisted.firewall_sync_error
    assert unsupported.firewall_pending and \
        "unsupported_family" in unsupported.firewall_sync_error
    valid = IPSet.objects.get(name=names[0])
    ipv6 = IPSet.objects.get(name=names[1])
    collision = IPSet.objects.get(name=names[2])
    assert valid.is_enabled and valid.last_synced is None and \
        valid.sync_error.startswith("pending checked")
    assert not ipv6.is_enabled and "unsupported_family" in ipv6.sync_error
    assert not collision.is_enabled and \
        "configured_name_collision" in collision.sync_error


@th.unit_test("0054 depends on the historical IPSet schema")
def test_firewall_truth_migration_dependency(opts):
    migration = importlib.import_module(
        "mojo.apps.account.migrations.0054_geolocatedip_firewall_reconciliation")
    assert ("incident", "0043_incidentllmattempt") in migration.Migration.dependencies
