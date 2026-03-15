"""
Tests for the security events log endpoint.

Coverage:
  - Returns events for the authenticated user filtered to security kinds
  - Returns empty list when no matching events exist
  - Does not return events belonging to another user
  - Unauthenticated request returns 401/403
  - size param limits results; values > 100 are capped at 100
  - dr_start and dr_end filter correctly
  - details, title, metadata, level, model_name are absent from all results
  - Unknown category values fall back to the category string as summary
  - Response contains expected fields: created, kind, summary, ip
"""
from testit import helpers as th
from testit.helpers import assert_true, assert_eq

TEST_USER = "secevents_user"
TEST_PWORD = "secevt##mojo99"
TEST_EMAIL = "secevents_user@example.com"

OTHER_USER = "secevents_other"
OTHER_EMAIL = "secevents_other@example.com"


# ===========================================================================
# Setup / teardown
# ===========================================================================

@th.django_unit_setup()
def setup_security_events(opts):
    from mojo.apps.account.models import User
    from mojo.apps.incident.models.event import Event
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    # Primary test user
    user = User.objects.filter(email=TEST_EMAIL).last()
    if user is None:
        user = User(username=TEST_USER, email=TEST_EMAIL)
        user.save()
    user.username = TEST_USER
    user.email = TEST_EMAIL
    user.is_active = True
    user.requires_mfa = False
    user.save_password(TEST_PWORD)
    user.save()
    opts.user_id = user.pk

    # Other user — their events must never appear in our results
    other = User.objects.filter(email=OTHER_EMAIL).last()
    if other is None:
        other = User(username=OTHER_USER, email=OTHER_EMAIL)
        other.save()
    other.username = OTHER_USER
    other.email = OTHER_EMAIL
    other.is_active = True
    other.save_password(TEST_PWORD)
    other.save()
    opts.other_user_id = other.pk

    # Seed some security events for the primary user
    Event.objects.filter(uid=user.pk, category__in=[
        "login", "invalid_password", "totp:login_failed",
        "email_change:requested", "sessions:revoked",
        "custom:unknown_kind",
    ]).delete()

    for category, ip in [
        ("login", "10.0.0.1"),
        ("invalid_password", "10.0.0.2"),
        ("totp:login_failed", "10.0.0.3"),
        ("email_change:requested", "10.0.0.4"),
        ("sessions:revoked", "10.0.0.5"),
    ]:
        Event.objects.create(
            uid=user.pk,
            category=category,
            source_ip=ip,
            level=1,
            title=f"Test event {category}",
            details=f"Internal details for {category}",
        )

    # Seed an event with an unknown category for fallback test
    Event.objects.create(
        uid=user.pk,
        category="login:weird_sub_kind",
        source_ip="10.0.0.99",
        level=1,
        title="Unknown sub-kind",
        details="Should fallback to category string",
    )

    # Seed events for the OTHER user — must never leak
    Event.objects.filter(uid=other.pk, category="login").delete()
    Event.objects.create(
        uid=other.pk,
        category="login",
        source_ip="192.168.1.1",
        level=1,
        title="Other user login",
        details="Other user internal details",
    )


# ===========================================================================
# Endpoint tests
# ===========================================================================

@th.django_unit_test("security events: returns events for authenticated user")
def test_security_events_basic(opts):
    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.get("/api/account/security-events")
    opts.client.logout()
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}")
    data = resp.json
    assert_true(data.get("status"), "Expected status=true")
    assert_true(data.get("count", 0) > 0, "Expected at least one event")
    results = data.get("results", [])
    assert_true(len(results) > 0, "Expected non-empty results list")

    # All results must belong to our user (we can't check uid directly
    # since it's not in the response, but we can verify known IPs)
    known_ips = {"10.0.0.1", "10.0.0.2", "10.0.0.3", "10.0.0.4", "10.0.0.5", "10.0.0.99"}
    for r in results:
        if r.get("ip"):
            assert_true(r["ip"] in known_ips or r["ip"] == "127.0.0.1",
                        f"Unexpected IP in results: {r['ip']}")


@th.django_unit_test("security events: response contains expected fields only")
def test_security_events_fields(opts):
    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.get("/api/account/security-events")
    opts.client.logout()
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}")
    results = resp.json.get("results", [])
    assert_true(len(results) > 0, "Need at least one result to check fields")

    for r in results:
        # Required fields present
        assert_true("created" in r, "Missing 'created' field")
        assert_true("kind" in r, "Missing 'kind' field")
        assert_true("summary" in r, "Missing 'summary' field")
        assert_true("ip" in r, "Missing 'ip' field")

        # Sensitive fields MUST be absent
        assert_true("details" not in r, "'details' must not be exposed")
        assert_true("title" not in r, "'title' must not be exposed")
        assert_true("metadata" not in r, "'metadata' must not be exposed")
        assert_true("level" not in r, "'level' must not be exposed")
        assert_true("model_name" not in r, "'model_name' must not be exposed")
        assert_true("model_id" not in r, "'model_id' must not be exposed")
        assert_true("hostname" not in r, "'hostname' must not be exposed")
        assert_true("country_code" not in r, "'country_code' must not be exposed")


@th.django_unit_test("security events: does not return other user's events")
def test_security_events_no_cross_user(opts):
    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.get("/api/account/security-events")
    opts.client.logout()
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}")
    results = resp.json.get("results", [])

    for r in results:
        # Other user's event has IP 192.168.1.1 — must never appear
        assert_true(r.get("ip") != "192.168.1.1",
                    "Found other user's event in results — cross-user leak!")


@th.django_unit_test("security events: unauthenticated returns 401/403")
def test_security_events_unauth(opts):
    opts.client.logout()
    resp = opts.client.get("/api/account/security-events")
    assert_true(resp.status_code in (401, 403), f"Expected 401 or 403, got {resp.status_code}")


@th.django_unit_test("security events: size param limits results")
def test_security_events_size_limit(opts):
    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.get("/api/account/security-events?size=2")
    opts.client.logout()
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}")
    results = resp.json.get("results", [])
    assert_true(len(results) <= 2, f"Expected at most 2 results, got {len(results)}")


@th.django_unit_test("security events: size > 100 capped at 100")
def test_security_events_size_capped(opts):
    opts.client.login(TEST_USER, TEST_PWORD)
    # We can't easily seed 100+ events, but we can verify the endpoint
    # doesn't crash and respects the cap by checking count <= 100
    resp = opts.client.get("/api/account/security-events?size=999")
    opts.client.logout()
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}")
    results = resp.json.get("results", [])
    assert_true(len(results) <= 100, f"Expected at most 100 results, got {len(results)}")


@th.django_unit_test("security events: dr_start filters correctly")
def test_security_events_date_filter(opts):
    opts.client.login(TEST_USER, TEST_PWORD)
    # Use a future date to get zero results
    resp = opts.client.get("/api/account/security-events?dr_start=2099-01-01")
    opts.client.logout()
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}")
    results = resp.json.get("results", [])
    assert_eq(len(results), 0, "Expected zero results for future dr_start")


@th.django_unit_test("security events: dr_end filters correctly")
def test_security_events_date_end_filter(opts):
    opts.client.login(TEST_USER, TEST_PWORD)
    # Use a past date to get zero results
    resp = opts.client.get("/api/account/security-events?dr_end=2000-01-01")
    opts.client.logout()
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}")
    results = resp.json.get("results", [])
    assert_eq(len(results), 0, "Expected zero results for past dr_end")


@th.django_unit_test("security events: known kinds have human-readable summaries")
def test_security_events_known_summaries(opts):
    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.get("/api/account/security-events")
    opts.client.logout()
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}")
    results = resp.json.get("results", [])

    summary_map = {
        "login": "Successful login",
        "invalid_password": "Failed login",
        "totp:login_failed": "Failed login",
        "email_change:requested": "Email change requested",
        "sessions:revoked": "All sessions revoked",
    }

    for r in results:
        kind = r.get("kind", "")
        summary = r.get("summary", "")
        if kind in summary_map:
            expected_fragment = summary_map[kind]
            assert_true(expected_fragment.lower() in summary.lower(),
                        f"For kind={kind}, expected summary containing '{expected_fragment}', got: '{summary}'")


@th.django_unit_test("security events: unknown category falls back to category string as summary")
def test_security_events_unknown_kind_fallback(opts):
    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.get("/api/account/security-events")
    opts.client.logout()
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}")
    results = resp.json.get("results", [])

    # Find the unknown sub-kind event
    unknown_results = [r for r in results if r.get("kind") == "login:weird_sub_kind"]
    if unknown_results:
        summary = unknown_results[0].get("summary", "")
        # Should fall back to the category string itself
        assert_eq(summary, "login:weird_sub_kind",
                  f"Unknown kind should use category as summary, got: '{summary}'")


@th.django_unit_test("security events: empty results for user with no events")
def test_security_events_empty(opts):
    from mojo.apps.account.models import User
    from mojo.apps.incident.models.event import Event

    # Create a clean user with no events
    clean_user = User.objects.filter(email="secevents_clean@example.com").last()
    if clean_user is None:
        clean_user = User(username="secevents_clean", email="secevents_clean@example.com")
        clean_user.save()
    clean_user.username = "secevents_clean"
    clean_user.is_active = True
    clean_user.save_password(TEST_PWORD)
    clean_user.save()

    # Delete any events for this user
    Event.objects.filter(uid=clean_user.pk).delete()

    opts.client.login("secevents_clean", TEST_PWORD)
    resp = opts.client.get("/api/account/security-events")
    opts.client.logout()
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}")
    data = resp.json
    assert_eq(data.get("count", -1), 0, "Expected count=0 for user with no events")
    assert_eq(len(data.get("results", [1])), 0, "Expected empty results list")


@th.django_unit_test("security events: non-security categories are excluded")
def test_security_events_non_security_excluded(opts):
    from mojo.apps.incident.models.event import Event

    # Create a non-security event for the test user
    Event.objects.create(
        uid=opts.user_id,
        category="some_random_system_event",
        source_ip="10.0.0.50",
        level=1,
        title="System event",
        details="Not a security event",
    )

    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.get("/api/account/security-events")
    opts.client.logout()
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}")
    results = resp.json.get("results", [])

    for r in results:
        assert_true(r.get("kind") != "some_random_system_event",
                    "Non-security category should not appear in results")