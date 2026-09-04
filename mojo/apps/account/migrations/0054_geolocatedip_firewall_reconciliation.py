import ipaddress
import re

from django.conf import settings
from django.db import migrations, models
from django.db.models import Q
from django.utils import timezone


_SET_NAME = re.compile(r"^[A-Za-z0-9_-]{1,31}$")


def _geo_touched():
    return (
        Q(is_blocked=True) | Q(blocked_at__isnull=False) |
        Q(blocked_until__isnull=False) |
        (Q(blocked_reason__isnull=False) & ~Q(blocked_reason="")) |
        Q(block_count__gt=0) | Q(is_whitelisted=True) |
        (Q(whitelisted_reason__isnull=False) & ~Q(whitelisted_reason="")) |
        Q(whitelisted_until__isnull=False))


def _valid_ipv4(value):
    try:
        return ipaddress.ip_address(value).version == 4
    except ValueError:
        return False


def _ipset_quarantine(row, permanent_name):
    name = row.name
    if not isinstance(name, str) or not _SET_NAME.fullmatch(name):
        return "invalid_set_name"
    if name == permanent_name:
        return "configured_name_collision"
    if name.endswith("_tmp") or name.startswith("mojo_"):
        return "reserved_set_name"
    if row.is_enabled and len(name) + 4 > 31:
        return "invalid_set_name"
    values = []
    for line in (row.data or "").splitlines():
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        values.append(value)
    if len(values) > 250000:
        return "network_limit"
    try:
        networks = [ipaddress.ip_network(value, strict=False) for value in values]
    except ValueError:
        return "invalid_network"
    if any(network.version != 4 for network in networks):
        return "unsupported_family"
    return ""


def mark_existing_firewall_state_pending(apps, schema_editor):
    GeoLocatedIP = apps.get_model("account", "GeoLocatedIP")
    IPSet = apps.get_model("incident", "IPSet")
    now = timezone.now()
    for row in GeoLocatedIP.objects.filter(_geo_touched()).iterator(
            chunk_size=1000):
        if _valid_ipv4(row.ip_address):
            active_whitelist = bool(
                row.is_whitelisted and
                (row.whitelisted_until is None or row.whitelisted_until > now))
            active_block = bool(
                row.is_blocked and
                (row.blocked_until is None or row.blocked_until > now) and
                not active_whitelist)
            direction = "presence" if active_block else "absence"
            error = f"pending checked firewall {direction} repair"
        else:
            try:
                parsed = ipaddress.ip_address(row.ip_address)
                code = "unsupported_family" if parsed.version != 4 else "invalid_network"
            except ValueError:
                code = "invalid_network"
            error = f"quarantined: {code}"
        GeoLocatedIP.objects.filter(pk=row.pk).update(
            firewall_generation=1, firewall_pending=True,
            firewall_sync_error=error, firewall_observed_at=None)

    permanent_name = getattr(
        settings, "FIREWALL_BLOCKED_IPSET_NAME", "mojo_blocked")
    for row in IPSet.objects.all().iterator(chunk_size=1000):
        code = _ipset_quarantine(row, permanent_name)
        values = {"last_synced": None}
        if code:
            values.update(
                is_enabled=False, sync_error=f"quarantined: {code}")
        else:
            values["sync_error"] = "pending checked firewall reconciliation"
        IPSet.objects.filter(pk=row.pk).update(**values)


class Migration(migrations.Migration):
    dependencies = [
        ("account", "0053_llmcircuitbreaker_llmrequest"),
        ("incident", "0043_incidentllmattempt"),
    ]

    operations = [
        migrations.AddField(
            model_name="geolocatedip",
            name="firewall_generation",
            field=models.PositiveBigIntegerField(db_index=True, default=0),
        ),
        migrations.AddField(
            model_name="geolocatedip",
            name="firewall_pending",
            field=models.BooleanField(db_index=True, default=False),
        ),
        migrations.AddField(
            model_name="geolocatedip",
            name="firewall_sync_error",
            field=models.CharField(blank=True, default="", max_length=512),
        ),
        migrations.AddField(
            model_name="geolocatedip",
            name="firewall_observed_at",
            field=models.DateTimeField(blank=True, default=None, null=True),
        ),
        migrations.RunPython(
            mark_existing_firewall_state_pending,
            reverse_code=migrations.RunPython.noop),
    ]
