"""Regression tests for IP-storage fields — IPv6 truncation + None-IP handling (ITEM-011).

After ITEM-009/010 the resolvers emit normalized IPv6 or None, so downstream IP fields must:
- hold a full IPv6 (CharField IP columns >= 45 chars, no truncation),
- accept a None IP instead of crashing/dropping (the two non-nullable fields),
- compute an IPv6-safe subnet (GeoLocatedIP).
Each case fails on the pre-fix schema (Postgres DataError on too-long values / IntegrityError on
None) and passes once the fields are widened / made nullable.
"""
from testit import helpers as th

# A full IPv6 — 39 chars, longer than the old varchar(16) and varchar(32).
IPV6 = "2001:0db8:85a3:0000:0000:8a2e:0370:7334"


@th.django_unit_test("Event.source_ip holds a full IPv6 (no truncation)")
def test_event_source_ip_ipv6(opts):
    from mojo.apps.incident.models.event import Event
    Event.objects.filter(category="itest_ip_ipv6").delete()
    e = Event.objects.create(category="itest_ip_ipv6", source_ip=IPV6)
    e.refresh_from_db()
    assert e.source_ip == IPV6, "Event.source_ip truncated: %r" % (e.source_ip,)
    Event.objects.filter(category="itest_ip_ipv6").delete()


@th.django_unit_test("Incident.source_ip holds a full IPv6 (no truncation)")
def test_incident_source_ip_ipv6(opts):
    from mojo.apps.incident.models.incident import Incident
    Incident.objects.filter(category="itest_ip_ipv6").delete()
    i = Incident.objects.create(category="itest_ip_ipv6", source_ip=IPV6)
    i.refresh_from_db()
    assert i.source_ip == IPV6, "Incident.source_ip truncated: %r" % (i.source_ip,)
    Incident.objects.filter(category="itest_ip_ipv6").delete()


@th.django_unit_test("Log.ip holds a full IPv6 (no truncation)")
def test_log_ip_ipv6(opts):
    from mojo.apps.logit.models.log import Log
    Log.objects.filter(ip=IPV6).delete()
    log = Log.objects.create(ip=IPV6, kind="itest_ip_ipv6")
    log.refresh_from_db()
    assert log.ip == IPV6, "Log.ip truncated: %r" % (log.ip,)
    Log.objects.filter(ip=IPV6).delete()


@th.django_unit_test("UserLoginEvent records a None IP instead of dropping it")
def test_login_event_null_ip(opts):
    from mojo.apps.account.models.user import User
    from mojo.apps.account.models.login_event import UserLoginEvent
    User.objects.filter(username="itest_ip_ipv6_user").delete()
    u = User(username="itest_ip_ipv6_user", display_name="itest", email="itest_ip_ipv6@example.com")
    u.save()
    try:
        ev = UserLoginEvent.objects.create(user=u, ip_address=None)
        assert ev.pk is not None, "UserLoginEvent with a None ip_address should persist"
        assert ev.ip_address is None, "ip_address should store as None, got %r" % (ev.ip_address,)
    finally:
        UserLoginEvent.objects.filter(user=u).delete()
        u.delete()


@th.django_unit_test("BouncerSignal accepts a None IP (no pre-auth crash)")
def test_bouncer_signal_null_ip(opts):
    from mojo.apps.account.models.bouncer_signal import BouncerSignal
    BouncerSignal.objects.filter(muid="itest_ip_ipv6").delete()
    sig = BouncerSignal.objects.create(muid="itest_ip_ipv6", ip_address=None)
    assert sig.pk is not None, "BouncerSignal with a None ip_address should persist"
    assert sig.ip_address is None, "ip_address should store as None, got %r" % (sig.ip_address,)
    BouncerSignal.objects.filter(muid="itest_ip_ipv6").delete()


@th.django_unit_test("GeoLocatedIP subnet is IPv6-safe (no garbage, no truncation)")
def test_geolocated_ip_subnet_ipv6(opts):
    from mojo.apps.account.models.geolocated_ip import GeoLocatedIP
    ip = "2001:db8::dead:beef"
    GeoLocatedIP.objects.filter(ip_address=ip).delete()
    geo = GeoLocatedIP.geolocate(ip, auto_refresh=False)
    # The /64 network of 2001:db8::dead:beef is 2001:db8::  (old code produced
    # "2001:db8::dead:bee", which also overflows the old varchar(16) -> DataError).
    assert geo.subnet == "2001:db8::", "IPv6 subnet should be the /64 prefix, got %r" % (geo.subnet,)
    GeoLocatedIP.objects.filter(ip_address=ip).delete()
