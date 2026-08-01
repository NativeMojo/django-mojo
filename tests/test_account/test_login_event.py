"""
Tests for UserLoginEvent model — login geo tracking, anomaly flags, and metrics.
"""
from testit import helpers as th


@th.django_unit_setup()
def setup_login_event(opts):
    from mojo.apps.account.models.user import User
    from mojo.apps.account.models.geolocated_ip import GeoLocatedIP
    from mojo.apps.account.models.login_event import UserLoginEvent

    # Clean up any leftover data from previous runs (long-lived databases)
    User.objects.filter(email="geotest@example.com").delete()
    GeoLocatedIP.objects.filter(ip_address__in=["203.0.113.45", "198.51.100.10", "203.0.113.99"]).delete()

    # Create test user
    opts.user = User.objects.create_user(
        username="geo_test_user",
        email="geotest@example.com",
        password="testpass123",
    )

    # Clean up any login events for this user from previous runs
    UserLoginEvent.objects.filter(user=opts.user).delete()

    # Create a GeoLocatedIP record so track() can find it
    opts.geo_ip = GeoLocatedIP.objects.create(
        ip_address="203.0.113.45",
        country_code="US",
        region="California",
        region_code="US-CA",
        city="San Francisco",
        latitude=37.7749,
        longitude=-122.4194,
        provider="test",
    )

    # Second geo for new-country testing
    opts.geo_ip_br = GeoLocatedIP.objects.create(
        ip_address="198.51.100.10",
        country_code="BR",
        region="Sao Paulo",
        city="Sao Paulo",
        latitude=-23.5505,
        longitude=-46.6333,
        provider="test",
    )

    # IP with no geo data
    opts.unknown_ip = "192.0.2.99"


@th.django_unit_test()
def test_track_creates_event_with_geo(opts):
    """Login from a known IP creates an event with denormalized geo data."""
    from mojo.apps.account.models.login_event import UserLoginEvent
    from objict import objict

    request = objict(ip="203.0.113.45", user_agent="Mozilla/5.0", device=None)
    event = UserLoginEvent.track(request, opts.user, source="password")

    assert event is not None, "track() should return an event"
    assert event.user_id == opts.user.id, f"Expected user {opts.user.id}, got {event.user_id}"
    assert event.ip_address == "203.0.113.45", f"Expected IP 203.0.113.45, got {event.ip_address}"
    assert event.country_code == "US", f"Expected country_code US, got {event.country_code}"
    assert event.region == "California", f"Expected region California, got {event.region}"
    assert event.region_code == "US-CA", f"Expected region_code US-CA, got {event.region_code}"
    assert event.city == "San Francisco", f"Expected city San Francisco, got {event.city}"
    assert event.latitude == 37.7749, f"Expected latitude 37.7749, got {event.latitude}"
    assert event.longitude == -122.4194, f"Expected longitude -122.4194, got {event.longitude}"
    assert event.source == "password", f"Expected source password, got {event.source}"
    assert isinstance(event.user_agent_info, dict), f"Expected dict, got {type(event.user_agent_info)}"
    opts.first_event = event


@th.django_unit_test()
def test_multiple_logins_create_separate_rows(opts):
    """Multiple logins from the same IP should NOT be deduplicated."""
    from mojo.apps.account.models.login_event import UserLoginEvent
    from objict import objict

    request = objict(ip="203.0.113.45", user_agent="Mozilla/5.0", device=None)
    event2 = UserLoginEvent.track(request, opts.user, source="password")

    assert event2.id != opts.first_event.id, (
        f"Expected different event IDs, both are {event2.id}"
    )
    count = UserLoginEvent.objects.filter(user=opts.user, ip_address="203.0.113.45").count()
    assert count >= 2, f"Expected at least 2 events for same IP, got {count}"


@th.django_unit_test()
def test_new_country_flag(opts):
    """First login from a new country should set is_new_country=True."""
    from mojo.apps.account.models.login_event import UserLoginEvent
    from objict import objict

    request = objict(ip="198.51.100.10", user_agent="Mozilla/5.0", device=None)
    event = UserLoginEvent.track(request, opts.user, source="password")

    assert event.is_new_country is True, (
        f"Expected is_new_country=True for first BR login, got {event.is_new_country}"
    )
    assert event.country_code == "BR", f"Expected BR, got {event.country_code}"


@th.django_unit_test()
def test_new_country_flag_not_set_on_repeat(opts):
    """Second login from the same country should NOT set is_new_country."""
    from mojo.apps.account.models.login_event import UserLoginEvent
    from objict import objict

    request = objict(ip="198.51.100.10", user_agent="Mozilla/5.0", device=None)
    event = UserLoginEvent.track(request, opts.user, source="password")

    assert event.is_new_country is False, (
        f"Expected is_new_country=False for repeat BR login, got {event.is_new_country}"
    )


@th.django_unit_test()
def test_new_region_flag(opts):
    """First login from US (existing country) but new region should flag is_new_region."""
    from mojo.apps.account.models.login_event import UserLoginEvent
    from mojo.apps.account.models.geolocated_ip import GeoLocatedIP
    from objict import objict

    # Create a geo record for a new US region
    GeoLocatedIP.objects.create(
        ip_address="203.0.113.99",
        country_code="US",
        region="New York",
        city="New York",
        latitude=40.7128,
        longitude=-74.006,
        provider="test",
    )

    request = objict(ip="203.0.113.99", user_agent="Mozilla/5.0", device=None)
    event = UserLoginEvent.track(request, opts.user, source="password")

    assert event.is_new_country is False, (
        f"US is not new, expected is_new_country=False, got {event.is_new_country}"
    )
    assert event.is_new_region is True, (
        f"New York is new, expected is_new_region=True, got {event.is_new_region}"
    )


@th.django_unit_test()
def test_unknown_ip_creates_event_with_null_geo(opts):
    """Login from an IP with no GeoLocatedIP record should create event with null geo fields."""
    from mojo.apps.account.models.login_event import UserLoginEvent
    from objict import objict

    request = objict(ip=opts.unknown_ip, user_agent="Mozilla/5.0", device=None)
    event = UserLoginEvent.track(request, opts.user, source="password")

    assert event is not None, "track() should still create an event for unknown IP"
    assert event.ip_address == opts.unknown_ip, f"Expected {opts.unknown_ip}, got {event.ip_address}"
    assert event.country_code is None, f"Expected None country_code, got {event.country_code}"
    assert event.region is None, f"Expected None region, got {event.region}"
    assert event.region_code is None, f"Expected None region_code, got {event.region_code}"
    assert event.is_new_country is False, "No country means no new-country flag"
    assert event.is_new_region is False, "No region means no new-region flag"


@th.django_unit_test()
def test_tracking_disabled(opts):
    """When LOGIN_EVENT_TRACKING_ENABLED is False, track() should return None."""
    from mojo.apps.account.models import login_event as le_module
    from mojo.apps.account.models.login_event import UserLoginEvent
    from objict import objict

    original = le_module.LOGIN_EVENT_TRACKING_ENABLED
    le_module.LOGIN_EVENT_TRACKING_ENABLED = False
    try:
        count_before = UserLoginEvent.objects.filter(user=opts.user).count()
        request = objict(ip="203.0.113.45", user_agent="Mozilla/5.0", device=None)
        result = UserLoginEvent.track(request, opts.user, source="password")

        assert result is None, f"Expected None when tracking disabled, got {result}"
        count_after = UserLoginEvent.objects.filter(user=opts.user).count()
        assert count_after == count_before, (
            f"Expected no new events, count went from {count_before} to {count_after}"
        )
    finally:
        le_module.LOGIN_EVENT_TRACKING_ENABLED = original


@th.django_unit_test()
def test_source_field_recorded(opts):
    """Different login sources should be recorded correctly."""
    from mojo.apps.account.models.login_event import UserLoginEvent
    from objict import objict

    for src in ["magic", "sms", "totp", "oauth"]:
        request = objict(ip="203.0.113.45", user_agent="Mozilla/5.0", device=None)
        event = UserLoginEvent.track(request, opts.user, source=src)
        assert event.source == src, f"Expected source {src}, got {event.source}"


@th.django_unit_test()
def test_no_client_ip_files_one_global_incident(opts):
    """Item 1100: a login with no resolved client IP still creates the login row
    (the report is filed AFTER the row), and files exactly ONE suppressed
    incident that is GLOBAL, not per-user (a per-user key would flood the plane
    when an ingress strips the IP from every request)."""
    from mojo.apps.account.models.login_event import UserLoginEvent
    from mojo.apps.incident.models import Event
    from mojo.apps.incident import notice_key
    from mojo.helpers.redis import get_connection
    from objict import objict

    category = "account:login_no_client_ip"
    notice = "account:login_event:no_ip_alerted"
    Event.objects.filter(category=category).delete()
    try:
        get_connection().delete(notice_key(category, notice))
    except Exception:
        pass

    # report_event reads path/method/META/user off the request; supply them so
    # the report path (request=the real request) exercises cleanly.
    no_ip = objict(
        ip=None, user_agent="Mozilla/5.0", device=None,
        path="/api/login", method="POST",
        META=objict(), user=objict(is_authenticated=False),
    )

    before = UserLoginEvent.objects.filter(user=opts.user).count()
    first_login = None
    for _ in range(3):
        event = UserLoginEvent.track(no_ip, opts.user, source="password")
        assert event is not None, "track() must still create the login row with no IP"
        assert event.ip_address is None, f"expected null ip, got {event.ip_address!r}"
        if first_login is None:
            first_login = event
    after = UserLoginEvent.objects.filter(user=opts.user).count()
    assert after - before == 3, (
        f"three no-IP logins must create three rows — the report never costs the "
        f"row — got {after - before}"
    )

    events = list(Event.objects.filter(category=category))
    assert len(events) == 1, (
        f"three no-IP logins must file exactly ONE incident (the key is global, "
        f"not per-user), got {len(events)}: {[e.title for e in events]}"
    )
    assert events[0].level == 5, (
        f"a client-IP-stripping ingress is a level-5 incident, got {events[0].level}"
    )
    details = events[0].details or ""
    assert "NOT user-specific" in details, (
        f"the incident must state it is NOT user-specific: {details!r}"
    )
    assert str(opts.user.id) in details and str(first_login.id) in details, (
        f"the incident must name user id and the (first) login row id as "
        f"EXAMPLES — proving it reports after the row: {details!r}"
    )

    # A login WITH an IP takes the non-report path — no new incident.
    real = objict(ip="203.0.113.45", user_agent="Mozilla/5.0", device=None)
    UserLoginEvent.track(real, opts.user, source="password")
    events = list(Event.objects.filter(category=category))
    assert len(events) == 1, (
        f"a login WITH an IP must not file a no-IP incident, still expected 1, "
        f"got {len(events)}"
    )
    Event.objects.filter(category=category).delete()
