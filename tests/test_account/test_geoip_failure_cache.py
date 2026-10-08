"""Regression (#6564): a failed GeoIP lookup is remembered, not retried per event.

Before the fix, refresh() returned early on failure without setting
`expires_at`, so the record stayed expired and every geolocate() call for the
address re-ran the whole provider chain — timeouts and log lines included.

The provider chain is replaced in-process through the keyword-only `locator=`
seam on geolocate()/refresh(), so nothing here patches the shared
mojo.helpers.geoip modules. Every address sits in its own 2001:db8:6564::/48
documentation prefix, so no other package's rows (or subnet lookups) touch
these.
"""
from datetime import timedelta
from testit import helpers as th

TESTIT_TIER = "bug"


class _Locator(object):
    """Stands in for geoip.geolocate_ip and counts the calls it receives."""

    def __init__(self, result=None):
        self.result = result
        self.calls = []

    def __call__(self, ip_address, check_threats=False):
        self.calls.append(ip_address)
        return self.result


def _fresh(ip_address):
    from mojo.apps.account.models.geolocated_ip import GeoLocatedIP
    GeoLocatedIP.objects.filter(ip_address=ip_address).delete()
    return GeoLocatedIP


def _expire(geo):
    """Move a record's expiry into the past — the TTL having elapsed."""
    from mojo.helpers import dates
    type(geo).objects.filter(pk=geo.pk).update(
        expires_at=dates.utcnow() - timedelta(seconds=1))


@th.django_unit_test()
def test_failed_lookup_is_not_retried_within_ttl(opts):
    from mojo.apps.account.models import geolocated_ip
    from mojo.helpers import dates

    ip = "2001:db8:6564::11"
    GeoLocatedIP = _fresh(ip)
    failing = _Locator(None)

    before = dates.utcnow()
    first = GeoLocatedIP.geolocate(ip, subdomain_only=False, locator=failing)
    after = dates.utcnow()
    assert len(failing.calls) == 1, (
        f"first lookup must call the provider chain once, got {len(failing.calls)}")

    first.refresh_from_db()
    assert first.provider == GeoLocatedIP.FAILED_PROVIDER, (
        f"an unresolved failed lookup must be marked provider="
        f"{GeoLocatedIP.FAILED_PROVIDER!r}, got {first.provider!r}")
    ttl = timedelta(seconds=geolocated_ip.GEOIP_FAILURE_TTL)
    assert first.expires_at is not None and before + ttl <= first.expires_at <= after + ttl, (
        f"a failed lookup must expire GEOIP_FAILURE_TTL "
        f"({geolocated_ip.GEOIP_FAILURE_TTL}s) from now, got {first.expires_at!r}")
    assert not first.is_expired, "a freshly failed lookup must not read as expired"

    second = GeoLocatedIP.geolocate(ip, subdomain_only=False, locator=failing)
    assert len(failing.calls) == 1, (
        f"a second geolocate() inside the failure TTL must not call any "
        f"provider; the chain ran {len(failing.calls)} times")
    assert second.pk == first.pk, "the cached failed record must be returned"
    assert second.provider == GeoLocatedIP.FAILED_PROVIDER, (
        f"the cached record must still carry the failed marker, got {second.provider!r}")


@th.django_unit_test()
def test_failed_lookup_is_retried_after_ttl(opts):
    ip = "2001:db8:6564::12"
    GeoLocatedIP = _fresh(ip)
    failing = _Locator(None)

    geo = GeoLocatedIP.geolocate(ip, subdomain_only=False, locator=failing)
    assert len(failing.calls) == 1, "fixture: the first lookup must run the chain"

    _expire(geo)
    GeoLocatedIP.geolocate(ip, subdomain_only=False, locator=failing)
    assert len(failing.calls) == 2, (
        f"once the failure TTL has elapsed the provider chain must run again; "
        f"it ran {len(failing.calls)} times in total")

    geo.refresh_from_db()
    assert not geo.is_expired, (
        "a retry that fails again must start a new failure TTL")


@th.django_unit_test()
def test_success_replaces_failed_record(opts):
    from mojo.apps.account.models import geolocated_ip
    from mojo.helpers import dates

    ip = "2001:db8:6564::13"
    GeoLocatedIP = _fresh(ip)

    geo = GeoLocatedIP.geolocate(ip, subdomain_only=False, locator=_Locator(None))
    geo.refresh_from_db()
    assert geo.provider == GeoLocatedIP.FAILED_PROVIDER, "fixture: lookup must fail first"

    _expire(geo)
    succeeding = _Locator({
        "provider": "maxmind", "country_code": "US",
        "country_name": "United States", "city": "Testville",
    })
    GeoLocatedIP.geolocate(ip, subdomain_only=False, locator=succeeding)
    assert len(succeeding.calls) == 1, "the expired failed record must be retried"

    geo.refresh_from_db()
    assert geo.provider == "maxmind", (
        f"a successful lookup must replace the failed marker, got {geo.provider!r}")
    assert geo.country_code == "US" and geo.city == "Testville", (
        f"a successful lookup must store its data, got "
        f"country_code={geo.country_code!r} city={geo.city!r}")
    min_expiry = dates.utcnow() + timedelta(
        days=geolocated_ip.GEOLOCATION_CACHE_DURATION_DAYS) - timedelta(minutes=5)
    assert geo.expires_at and geo.expires_at > min_expiry, (
        f"a successful lookup must use the normal cache duration, got {geo.expires_at!r}")


@th.django_unit_test()
def test_failed_refresh_keeps_earlier_result(opts):
    """A record that resolved before keeps its provider and data on a failed refresh."""
    from mojo.helpers import dates

    ip = "2001:db8:6564::14"
    GeoLocatedIP = _fresh(ip)
    GeoLocatedIP.objects.create(
        ip_address=ip, subnet="2001:db8:6564::", provider="mojo",
        country_code="CA", city="Oldtown",
        expires_at=dates.utcnow() - timedelta(days=1),
    )
    failing = _Locator(None)

    geo = GeoLocatedIP.geolocate(ip, subdomain_only=False, locator=failing)
    assert len(failing.calls) == 1, "the stale record must be refreshed once"
    geo.refresh_from_db()
    assert geo.provider == "mojo", (
        f"a failed refresh must not overwrite an earlier provider result, got {geo.provider!r}")
    assert geo.country_code == "CA" and geo.city == "Oldtown", (
        f"a failed refresh must keep the earlier data, got "
        f"country_code={geo.country_code!r} city={geo.city!r}")
    assert not geo.is_expired, "a failed refresh must start the failure TTL"

    GeoLocatedIP.geolocate(ip, subdomain_only=False, locator=failing)
    assert len(failing.calls) == 1, (
        f"a stale record whose refresh failed must not be retried inside the "
        f"failure TTL; the chain ran {len(failing.calls)} times")


@th.django_unit_test()
def test_subnet_lookup_skips_failed_record(opts):
    """A failed neighbour has no location to lend — the new IP does its own lookup."""
    from mojo.apps.account.models.geolocated_ip import GeoLocatedIP

    GeoLocatedIP.objects.filter(subnet="2001:db8:6564:1::").delete()
    failing = _Locator(None)
    GeoLocatedIP.geolocate("2001:db8:6564:1::21", subdomain_only=False, locator=failing)

    geo = GeoLocatedIP.geolocate("2001:db8:6564:1::22", subdomain_only=True, locator=failing)
    assert len(failing.calls) == 2, (
        f"the neighbour must run its own lookup, the chain ran {len(failing.calls)} times")
    geo.refresh_from_db()
    assert geo.provider == GeoLocatedIP.FAILED_PROVIDER, (
        f"a subnet match must never copy a failed record; got provider {geo.provider!r}")
