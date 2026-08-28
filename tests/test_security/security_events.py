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

Brand scoping (#3328) — the visibility matrix this file enforces:
  - No group supplied: owner-wide, exactly as before
  - ?group=<brand>: the caller's own rows attributed to that brand, PLUS their
    own null-group email_change: rows carrying security_activity_scope=account
  - Everything else stays out under ?group=: another brand's rows, unmarked
    legacy null-group rows, orphaned brand rows (Event.group is SET_NULL), and
    global login/sessions rows even when they carry the account marker
  - The group/group_uuid routing params are CONSUMED, so the generic list
    filter cannot re-apply them and drop the account-global rows
"""
from testit import helpers as th
from testit.helpers import assert_true, assert_eq

TEST_USER = "secevents_user"
TEST_PWORD = "secevt##mojo99"
TEST_EMAIL = "secevents_user@example.com"

OTHER_USER = "secevents_other"
OTHER_EMAIL = "secevents_other@example.com"

BRAND_A = "secevents_brand_a"
BRAND_B = "secevents_brand_b"

# Every seeded event carries a unique source_ip so a result row can be
# identified through the restricted `security` graph, which exposes only
# created/kind/summary/ip.
IP_BASE = ["10.0.0.1", "10.0.0.2", "10.0.0.3", "10.0.0.4", "10.0.0.5", "10.0.0.99"]
IP_BRAND_A = "10.1.0.1"          # login, attributed to brand A
IP_BRAND_B = "10.1.0.2"          # login, attributed to brand B
IP_ACCOUNT_GLOBAL = "10.1.0.3"   # email_change:confirmed, null group, account marker
IP_UNMARKED_NULL = "10.1.0.4"    # email_change:requested, null group, NO marker
IP_GLOBAL_LOGIN = "10.1.0.5"     # login, null group, account marker (prefix guard)
IP_FAIL_ATTRIBUTED = "10.1.0.6"  # email_change:send_failed, attributed to brand A
IP_FAIL_UNATTRIBUTED = "10.1.0.7"  # email_change:send_failed, brand marker, no group
IP_BRAND_SEEDED = [
    IP_BRAND_A, IP_BRAND_B, IP_ACCOUNT_GLOBAL, IP_UNMARKED_NULL,
    IP_GLOBAL_LOGIN, IP_FAIL_ATTRIBUTED, IP_FAIL_UNATTRIBUTED,
]
SEEDED_IPS = set(IP_BASE) | set(IP_BRAND_SEEDED)


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
    user.is_email_verified = True
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
    other.is_email_verified = True
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

    _setup_brand_fixtures(opts, user)


def _setup_brand_fixtures(opts, user):
    """Two brands and one row of every class the visibility matrix names."""
    from mojo.apps.account.models import Group
    from mojo.apps.incident.models.event import Event

    brand_a = Group.objects.filter(name=BRAND_A).last()
    if brand_a is None:
        brand_a = Group.objects.create(name=BRAND_A, kind="organization")
    brand_a.is_active = True
    brand_a.save()
    brand_a.add_member(user)
    opts.brand_a_id = brand_a.pk

    brand_b = Group.objects.filter(name=BRAND_B).last()
    if brand_b is None:
        brand_b = Group.objects.create(name=BRAND_B, kind="organization")
    brand_b.is_active = True
    brand_b.save()
    opts.brand_b_id = brand_b.pk

    Event.objects.filter(uid=user.pk, source_ip__in=IP_BRAND_SEEDED).delete()

    def _seed(category, ip, group, metadata):
        Event.objects.create(
            uid=user.pk,
            category=category,
            source_ip=ip,
            level=1,
            group=group,
            title=f"Test event {category} {ip}",
            details=f"Internal details for {category}",
            metadata=metadata,
        )

    _seed("login", IP_BRAND_A, brand_a,
          {"security_activity_scope": "brand", "origin_group_id": brand_a.pk})
    _seed("login", IP_BRAND_B, brand_b,
          {"security_activity_scope": "brand", "origin_group_id": brand_b.pk})
    _seed("email_change:confirmed", IP_ACCOUNT_GLOBAL, None,
          {"security_activity_scope": "account"})
    # Legacy shape: written before the marker existed.
    _seed("email_change:requested", IP_UNMARKED_NULL, None, {})
    # A global row that is NOT an email change — the category prefix is what
    # keeps it out of the account exception, not the marker.
    _seed("login", IP_GLOBAL_LOGIN, None,
          {"security_activity_scope": "account"})
    _seed("email_change:send_failed", IP_FAIL_ATTRIBUTED, brand_a,
          {"security_activity_scope": "brand", "origin_group_id": brand_a.pk,
           "failure_class": "not_sent"})
    _seed("email_change:send_failed", IP_FAIL_UNATTRIBUTED, None,
          {"security_activity_scope": "brand", "failure_class": "not_sent"})


def _ips(resp):
    return {row.get("ip") for row in resp.json.get("data", [])}


def _events(opts, path):
    """Authenticated GET against the security-events feed."""
    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.get(path)
    opts.client.logout()
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}")
    return resp


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
    results = data.get("data", [])
    assert_true(len(results) > 0, "Expected non-empty results list")

    # All results must belong to our user (we can't check uid directly
    # since it's not in the response, but we can verify known IPs)
    for r in results:
        if r.get("ip"):
            assert_true(r["ip"] in SEEDED_IPS or r["ip"] == "127.0.0.1",
                        f"Unexpected IP in results: {r['ip']}")


@th.django_unit_test("security events: response contains expected fields only")
def test_security_events_fields(opts):
    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.get("/api/account/security-events")
    opts.client.logout()
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}")
    results = resp.json.get("data", [])
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
    results = resp.json.get("data", [])

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
    results = resp.json.get("data", [])
    assert_true(len(results) <= 2, f"Expected at most 2 results, got {len(results)}")


@th.django_unit_test("security events: size > 100 capped at 100")
def test_security_events_size_capped(opts):
    opts.client.login(TEST_USER, TEST_PWORD)
    # We can't easily seed 100+ events, but we can verify the endpoint
    # doesn't crash and respects the cap by checking count <= 100
    resp = opts.client.get("/api/account/security-events?size=999")
    opts.client.logout()
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}")
    results = resp.json.get("data", [])
    assert_true(len(results) <= 100, f"Expected at most 100 results, got {len(results)}")


@th.django_unit_test("security events: dr_start filters correctly")
def test_security_events_date_filter(opts):
    opts.client.login(TEST_USER, TEST_PWORD)
    # Use a future date to get zero results
    resp = opts.client.get("/api/account/security-events?dr_start=2099-01-01")
    opts.client.logout()
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}")
    results = resp.json.get("data", [])
    assert_eq(len(results), 0, "Expected zero results for future dr_start")


@th.django_unit_test("security events: dr_end filters correctly")
def test_security_events_date_end_filter(opts):
    opts.client.login(TEST_USER, TEST_PWORD)
    # Use a past date to get zero results
    resp = opts.client.get("/api/account/security-events?dr_end=2000-01-01")
    opts.client.logout()
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}")
    results = resp.json.get("data", [])
    assert_eq(len(results), 0, "Expected zero results for past dr_end")


@th.django_unit_test("security events: known kinds have human-readable summaries")
def test_security_events_known_summaries(opts):
    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.get("/api/account/security-events")
    opts.client.logout()
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}")
    results = resp.json.get("data", [])

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
    results = resp.json.get("data", [])

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
    clean_user.is_email_verified = True
    clean_user.save_password(TEST_PWORD)
    clean_user.save()

    opts.client.login("secevents_clean", TEST_PWORD)
    # Delete any events (including the login event just created)
    Event.objects.filter(uid=clean_user.pk).delete()
    resp = opts.client.get("/api/account/security-events")
    opts.client.logout()
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}")
    data = resp.json
    assert_eq(data.get("count", -1), 0, "Expected count=0 for user with no events")
    assert_eq(len(data.get("data", [1])), 0, "Expected empty results list")


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
    results = resp.json.get("data", [])

    for r in results:
        assert_true(r.get("kind") != "some_random_system_event",
                    "Non-security category should not appear in results")

# ===========================================================================
# Brand scoping (#3328)
# ===========================================================================

@th.django_unit_test("security events: ?group= shows the caller's own rows for THAT brand")
def test_security_events_group_scoped_to_current_brand(opts):
    resp = _events(opts, f"/api/account/security-events?group={opts.brand_a_id}&size=100")
    ips = _ips(resp)
    assert_true(IP_BRAND_A in ips,
                f"a row attributed to the selected brand must be visible: {sorted(ips)}")
    assert_true(IP_FAIL_ATTRIBUTED in ips,
                f"an attributed email_change:send_failed row belongs on its brand's "
                f"page: {sorted(ips)}")


@th.django_unit_test("security events: ?group= excludes rows attributed to another brand")
def test_security_events_excludes_other_brand(opts):
    resp = _events(opts, f"/api/account/security-events?group={opts.brand_a_id}&size=100")
    ips = _ips(resp)
    assert_true(IP_BRAND_B not in ips,
                f"another brand's activity must not leak into this brand's feed: "
                f"{sorted(ips)}")


@th.django_unit_test("security events: ?group= still includes account-marked email-change rows")
def test_security_events_includes_account_marked_email_change(opts):
    resp = _events(opts, f"/api/account/security-events?group={opts.brand_a_id}&size=100")
    ips = _ips(resp)
    assert_true(IP_ACCOUNT_GLOBAL in ips,
                f"the address moved for the whole account, so email_change:confirmed "
                f"belongs on every brand's page: {sorted(ips)}")


@th.django_unit_test("security events: ?group= excludes unmarked null-group rows")
def test_security_events_excludes_unmarked_null_group_rows(opts):
    resp = _events(opts, f"/api/account/security-events?group={opts.brand_a_id}&size=100")
    ips = _ips(resp)
    assert_true(IP_UNMARKED_NULL not in ips,
                f"a null group is not proof of account origin — only the explicit "
                f"marker is: {sorted(ips)}")


@th.django_unit_test("security events: ?group= excludes global login rows even when account-marked")
def test_security_events_excludes_global_login_rows_under_group(opts):
    resp = _events(opts, f"/api/account/security-events?group={opts.brand_a_id}&size=100")
    ips = _ips(resp)
    assert_true(IP_GLOBAL_LOGIN not in ips,
                f"the account exception is scoped to email_change: by prefix — a "
                f"marked login row must still stay out: {sorted(ips)}")


@th.django_unit_test("security events: an orphaned brand row keeps its markers and stays out")
def test_security_events_excludes_orphaned_brand_rows(opts):
    """Event.group is SET_NULL, so deleting a brand nulls the FK. The row must
    not become account-global by accident."""
    from mojo.apps.account.models import Group
    from mojo.apps.incident.models.event import Event

    orphan_ip = "10.1.9.9"
    Event.objects.filter(uid=opts.user_id, source_ip=orphan_ip).delete()
    Group.objects.filter(name="secevents_brand_doomed").delete()
    doomed = Group.objects.create(name="secevents_brand_doomed", kind="organization")
    try:
        Event.objects.create(
            uid=opts.user_id,
            category="email_change:send_failed",
            source_ip=orphan_ip,
            level=6,
            group=doomed,
            title="Orphan-to-be",
            details="Internal details",
            metadata={"security_activity_scope": "brand",
                      "origin_group_id": doomed.pk,
                      "failure_class": "not_sent"},
        )
        doomed_id = doomed.pk
        doomed.delete()

        row = Event.objects.filter(uid=opts.user_id, source_ip=orphan_ip).last()
        assert_true(row is not None, "the audit row must survive the group deletion")
        assert_true(row.group_id is None,
                    f"precondition: SET_NULL must have nulled the FK, got {row.group_id!r}")
        assert_eq(row.metadata.get("security_activity_scope"), "brand",
                  f"the brand marker must survive the deletion: {row.metadata}")
        assert_eq(row.metadata.get("origin_group_id"), doomed_id,
                  f"the origin group id must survive the deletion: {row.metadata}")

        resp = _events(opts, f"/api/account/security-events?group={opts.brand_a_id}&size=100")
        assert_true(orphan_ip not in _ips(resp),
                    "an orphaned brand row must never masquerade as global history")
    finally:
        Event.objects.filter(uid=opts.user_id, source_ip=orphan_ip).delete()
        Group.objects.filter(name="secevents_brand_doomed").delete()


@th.django_unit_test("security events: group attribution requires active direct membership")
def test_security_events_group_requires_membership(opts):
    """_attributable_group is what stops a caller-supplied ?group= from filing
    activity into a brand they have nothing to do with."""
    from django.test import RequestFactory
    from objict import objict
    from mojo.apps.account.models import Group, User
    from mojo.apps.account.rest import user as user_rest

    factory = RequestFactory(REMOTE_ADDR="127.0.0.1")
    request = factory.post("/api/auth/email/change/request", {})
    request.DATA = objict.from_dict({})
    request.user = User.objects.get(pk=opts.user_id)

    request.group = None
    assert_true(user_rest._attributable_group(request) is None,
                "no group on the request means no attribution")

    request.group = Group.objects.get(pk=opts.brand_b_id)
    assert_true(user_rest._attributable_group(request) is None,
                "a brand the caller is not a member of must never be attributed — "
                "the dispatcher resolves ?group= with no membership check")

    brand_a = Group.objects.get(pk=opts.brand_a_id)
    request.group = brand_a
    attributed = user_rest._attributable_group(request)
    assert_true(attributed is not None and attributed.pk == brand_a.pk,
                f"an active direct membership must attribute the row, got {attributed!r}")


@th.django_unit_test("security events: no group supplied stays owner-wide (unchanged behavior)")
def test_security_events_no_group_stays_owner_wide(opts):
    resp = _events(opts, "/api/account/security-events?size=100")
    ips = _ips(resp)
    for ip in IP_BRAND_SEEDED:
        assert_true(ip in ips,
                    f"with no group the feed must stay owner-wide — {ip} is missing: "
                    f"{sorted(ips)}")


@th.django_unit_test("security events: brand scoping preserves size/date/sort/graph primitives")
def test_security_events_group_scope_preserves_query_primitives(opts):
    base = f"/api/account/security-events?group={opts.brand_a_id}"

    resp = _events(opts, f"{base}&size=1")
    results = resp.json.get("data", [])
    assert_true(len(results) <= 1, f"size must still cap the page, got {len(results)}")

    resp = _events(opts, f"{base}&size=999")
    assert_true(len(resp.json.get("data", [])) <= 100,
                "the 100-row cap must still apply under brand scoping")

    resp = _events(opts, f"{base}&dr_start=2099-01-01")
    assert_eq(len(resp.json.get("data", [])), 0,
              "dr_start must still filter under brand scoping")

    resp = _events(opts, f"{base}&dr_end=2000-01-01")
    assert_eq(len(resp.json.get("data", [])), 0,
              "dr_end must still filter under brand scoping")

    resp = _events(opts, f"{base}&size=100")
    data = resp.json
    assert_true(data.get("status") is True, f"the response envelope is unchanged: {data.keys()}")
    assert_true("count" in data, "the paginated envelope must still carry count")
    rows = data.get("data", [])
    assert_true(len(rows) > 0, "need at least one row to check the graph")
    for row in rows:
        assert_eq(sorted(row.keys()), ["created", "ip", "kind", "summary"],
                  f"the restricted security graph must be unchanged: {sorted(row.keys())}")

    created = [row["created"] for row in rows]
    assert_eq(created, sorted(created, reverse=True),
              "the default -created sort must survive brand scoping")


@th.django_unit_test("security events: ?group= is consumed as routing, never re-applied as a filter")
def test_security_events_group_param_not_treated_as_filter(opts):
    """If the endpoint left group/group_uuid in place, the generic list filter
    would re-apply group=<brand> and drop every account-global row."""
    resp = _events(opts, f"/api/account/security-events?group={opts.brand_a_id}&size=100")
    assert_true(IP_ACCOUNT_GLOBAL in _ips(resp),
                "the account-marked null-group row is only reachable if the routing "
                "params were consumed before the generic filter ran")

    from mojo.apps.account.models import Group
    uuid = Group.objects.get(pk=opts.brand_a_id).get_uuid()
    resp = _events(opts, f"/api/account/security-events?group_uuid={uuid}&size=100")
    ips = _ips(resp)
    assert_true(IP_ACCOUNT_GLOBAL in ips,
                f"group_uuid must route exactly like group: {sorted(ips)}")
    assert_true(IP_BRAND_B not in ips,
                f"group_uuid must scope exactly like group: {sorted(ips)}")


@th.django_unit_test("security events: send_failed visibility matrix — attributed by brand, unattributed owner-wide")
def test_send_failed_visibility_matrix(opts):
    scoped = _ips(_events(
        opts, f"/api/account/security-events?group={opts.brand_a_id}&size=100"))
    assert_true(IP_FAIL_ATTRIBUTED in scoped,
                f"a send_failed filed WITH brand attribution shows under that brand: "
                f"{sorted(scoped)}")
    assert_true(IP_FAIL_UNATTRIBUTED not in scoped,
                f"a send_failed filed WITHOUT attribution must not appear under any "
                f"brand: {sorted(scoped)}")

    wide = _ips(_events(opts, "/api/account/security-events?size=100"))
    assert_true(IP_FAIL_UNATTRIBUTED in wide,
                f"an unattributed row is still the caller's own history and stays "
                f"visible owner-wide: {sorted(wide)}")


# ===========================================================================
# Sign-out telemetry + the sign-in row (#3329)
# ===========================================================================
#
# POST /api/account/security-events/logout is AUDIT ONLY: it records that this
# browser signed out and does nothing else — no auth_key rotation, no token
# minting or revocation, no last_login write, no effect on another device.
# Every test below is either "the row reaches the feed" or "nothing else moved".

LOGOUT_PATH = "/api/account/security-events/logout"
LOGOUT_KIND = "sessions:logout"
LOGOUT_SUMMARY = "Browser sign-out requested"


def _clear_logout_limits(opts, account_id=None):
    """Flush the endpoint's own ip/muid buckets plus the global API throttle.

    ip_limit is 30/60s and muid_limit 10/300s; this module makes well over ten
    sign-out calls, so every one of them clears first.
    """
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1", key="security_logout")
    muid = opts.client.session.cookies.get("_muid")
    if muid:
        clear_rate_limits(key="security_logout", muid=muid)
    if account_id is not None:
        clear_rate_limits(user_id=account_id)


def _logout_rows(uid, category=LOGOUT_KIND):
    from mojo.apps.incident.models.event import Event
    return Event.objects.filter(uid=uid, category=category)


def _post_logout(opts, body=None, headers=None, account_id=None):
    _clear_logout_limits(opts, account_id=account_id)
    return opts.client.post(LOGOUT_PATH, body or {}, headers=headers or {})


@th.django_unit_test("logout telemetry: the POST reaches the user's own feed")
def test_logout_endpoint_reaches_the_feed(opts):
    opts.client.login(TEST_USER, TEST_PWORD)
    before = _logout_rows(opts.user_id).count()

    resp = _post_logout(opts, account_id=opts.user_id)
    assert_eq(resp.status_code, 200,
              f"the sign-out notification must always 200, got "
              f"{resp.status_code}: {opts.client.last_response.body}")
    assert_true(resp.json.get("data", {}).get("recorded") is True,
                f"a plain authenticated sign-out must record: {resp.json}")
    assert_eq(_logout_rows(opts.user_id).count(), before + 1,
              "exactly one sign-out row per call")

    feed = opts.client.get("/api/account/security-events?size=100")
    opts.client.logout()
    assert_eq(feed.status_code, 200, f"Expected 200, got {feed.status_code}")
    rows = [r for r in feed.json.get("data", []) if r.get("kind") == LOGOUT_KIND]
    assert_true(len(rows) > 0,
                f"the sign-out must be visible on the Security page — the whole "
                f"point of the endpoint: {feed.json.get('data')}")
    assert_eq(rows[0].get("summary"), LOGOUT_SUMMARY,
              f"the feed must render a human summary, not the raw category, got "
              f"{rows[0].get('summary')!r}")


@th.django_unit_test("logout telemetry: spoofed audit fields in the body are ignored")
def test_logout_ignores_spoofed_audit_fields(opts):
    from mojo.apps.incident.models.event import Event

    opts.client.login(TEST_USER, TEST_PWORD)
    other_before = Event.objects.filter(uid=opts.other_user_id).count()

    resp = _post_logout(opts, {
        "uid": opts.other_user_id,
        "category": "admin:owned",
        "title": "spoofed title",
        "details": "spoofed details",
        "source_ip": "9.9.9.9",
        "level": 9,
    }, account_id=opts.user_id)
    opts.client.logout()

    assert_eq(resp.status_code, 200,
              f"a spoofed body must not change the response, got "
              f"{resp.status_code}: {opts.client.last_response.body}")
    row = _logout_rows(opts.user_id).order_by("-id").first()
    assert_true(row is not None, "the sign-out row must have been written")
    assert_eq(row.uid, opts.user_id,
              f"the row belongs to the authenticated caller, never a body-supplied "
              f"uid, got {row.uid!r}")
    assert_eq(row.category, LOGOUT_KIND,
              f"the category is a fixed literal, got {row.category!r}")
    assert_eq(row.title, LOGOUT_SUMMARY,
              f"the title is a fixed literal, got {row.title!r}")
    assert_eq(row.source_ip, "127.0.0.1",
              f"the source IP comes from the request, got {row.source_ip!r}")
    assert_eq(row.level, 1,
              f"the level is fixed, got {row.level!r}")
    assert_eq(Event.objects.filter(uid=opts.other_user_id).count(), other_before,
              "another user's history must be untouchable from this endpoint")
    assert_eq(Event.objects.filter(
        uid=opts.user_id, category="admin:owned").count(), 0,
        "a body-supplied category must never become the row's category")


@th.django_unit_test("logout telemetry: no session side effects at all")
def test_logout_has_no_session_side_effects(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.models.login_event import UserLoginEvent

    opts.client.login(TEST_USER, TEST_PWORD)
    token = opts.client.access_token
    assert_true(bool(token), "precondition: we need a live access token")

    user = User.objects.get(pk=opts.user_id)
    auth_key_before = str(user.auth_key)
    last_login_before = user.last_login
    login_events_before = UserLoginEvent.objects.filter(user_id=opts.user_id).count()

    resp = _post_logout(opts, account_id=opts.user_id)
    assert_eq(resp.status_code, 200,
              f"Expected 200, got {resp.status_code}: {opts.client.last_response.body}")

    body = str(opts.client.last_response.body)
    for leaked in ("access_token", "refresh_token", "auth_key"):
        assert_true(leaked not in body,
                    f"an audit-only notification must return no credential "
                    f"material — found {leaked!r} in {body}")

    user.refresh_from_db()
    assert_eq(str(user.auth_key), auth_key_before,
              "a sign-out notification must NOT rotate auth_key — that would "
              "silently revoke every other device")
    assert_eq(user.last_login, last_login_before,
              "a sign-out notification must NOT touch last_login")
    assert_eq(UserLoginEvent.objects.filter(user_id=opts.user_id).count(),
              login_events_before,
              "a sign-out is not a login and must write no UserLoginEvent")

    # The token that made the call still works: this records history, it does
    # not revoke anything, on this device or any other.
    me = opts.client.get("/api/user/me")
    opts.client.logout()
    assert_eq(me.status_code, 200,
              f"the caller's own token must survive the notification, got "
              f"{me.status_code}: {opts.client.last_response.body}")


@th.django_unit_test("logout telemetry: credential matrix — JWT yes, confined bearers no")
def test_logout_credential_matrix(opts):
    from mojo.apps.account.models import ApiKey, Group, User
    from mojo.apps.account.services import group_token

    user = User.objects.get(pk=opts.user_id)
    brand_a = Group.objects.get(pk=opts.brand_a_id)

    # --- unauthenticated -------------------------------------------------
    opts.client.logout()
    before = _logout_rows(opts.user_id).count()
    resp = _post_logout(opts)
    assert_true(resp.status_code in (401, 403),
                f"an unauthenticated sign-out notification must be refused, got "
                f"{resp.status_code}: {opts.client.last_response.body}")
    assert_eq(_logout_rows(opts.user_id).count(), before,
              "a refused call must write no row")

    # --- a JWT session: the supported client ------------------------------
    opts.client.login(TEST_USER, TEST_PWORD)
    before = _logout_rows(opts.user_id).count()
    resp = _post_logout(opts, account_id=opts.user_id)
    opts.client.logout()
    assert_eq(resp.status_code, 200,
              f"a current JWT client must be accepted, got {resp.status_code}: "
              f"{opts.client.last_response.body}")
    assert_eq(_logout_rows(opts.user_id).count(), before + 1,
              "the JWT call must write exactly one row")

    # --- an ApiKey acting AS the user: refused ----------------------------
    ApiKey.objects.filter(name="secevents_logout_key").delete()
    api_key, raw_token = ApiKey.create_for_group(
        brand_a, "secevents_logout_key", permissions={}, user=user,
        override_user=True)
    try:
        before = _logout_rows(opts.user_id).count()
        resp = _post_logout(
            opts, headers={"Authorization": f"apikey {raw_token}"})
        assert_true(resp.status_code in (401, 403),
                    f"an ApiKey is a confined bearer in a config file, not a "
                    f"browser signing out — it must be refused, got "
                    f"{resp.status_code}: {opts.client.last_response.body}")
        assert_eq(_logout_rows(opts.user_id).count(), before,
                  "a refused ApiKey call must write no row, even though the key "
                  "acts as this very user")
    finally:
        api_key.delete()

    # --- a GroupScopedToken: refused --------------------------------------
    gs_token = group_token.mint(user, brand_a)
    before = _logout_rows(opts.user_id).count()
    resp = _post_logout(
        opts, headers={"Authorization": f"grouptoken {gs_token}"})
    assert_true(resp.status_code in (401, 403),
                f"a confined group token is delivered to a tenant-controlled "
                f"page and must be refused, got {resp.status_code}: "
                f"{opts.client.last_response.body}")
    assert_eq(_logout_rows(opts.user_id).count(), before,
              "a refused group-token call must write no row")


@th.django_unit_test("logout telemetry: api-scope OAuth grants keep their JWT equivalence")
def test_logout_oauth_grant_equivalence(opts):
    """The seam that decides the equivalence, asserted directly: the endpoint
    adds no identity gate beyond the shared decorator, and that decorator's
    predicate deliberately does not treat an OAuth grant as key-backed."""
    from objict import objict
    from mojo.decorators.auth import SECURITY_REGISTRY
    from mojo.helpers.request import is_key_backed_session
    from mojo.apps.account.rest import user as user_rest

    key = "mojo.apps.account.rest.user.on_account_security_events_logout"
    entry = SECURITY_REGISTRY.get(key)
    assert_true(entry is not None,
                f"the endpoint must be registered for security introspection; "
                f"registry has {len(SECURITY_REGISTRY)} entries")
    assert_true(entry.get("denies_key_backed_session") is True,
                f"the confined-bearer refusal must come from the shared "
                f"decorator: {entry}")
    assert_eq(entry.get("type"), "authentication",
              f"the only other gate is plain authentication — no permission and "
              f"no fresh-auth requirement may creep in: {entry}")
    assert_true("permissions" not in entry,
                f"a self-service sign-out needs no permission: {entry}")
    assert_true("geofence" not in entry,
                f"a user in a blocked geo must still be able to sign out of "
                f"their browser: {entry}")
    assert_true(callable(getattr(user_rest, "on_account_security_events_logout", None)),
                "the registry key must name the real view function")

    grant_only = objict(oauth_grant=objict(scopes=["api"], token_type="mcp"))
    assert_eq(is_key_backed_session(grant_only), False,
              "an OAuth grant is the person's own consented session, not a "
              "confined bearer — it must reach every endpoint their JWT reaches")


@th.django_unit_test("logout telemetry: an unusable group selection records nothing, and denies nothing")
def test_logout_invalid_group_records_nothing(opts):
    from mojo.apps.account.models import Group, GroupMember, User
    from mojo.apps.incident.models.event import Event

    opts.client.login(TEST_USER, TEST_PWORD)

    def _probe(body, why):
        denied_before = Event.objects.filter(
            uid=opts.user_id, category="user_permission_denied").count()
        before = _logout_rows(opts.user_id).count()
        resp = _post_logout(opts, body, account_id=opts.user_id)
        assert_eq(resp.status_code, 200,
                  f"{why}: a telemetry write must never look like a failed "
                  f"sign-out, got {resp.status_code}: "
                  f"{opts.client.last_response.body}")
        assert_true(resp.json.get("data", {}).get("recorded") is False,
                    f"{why}: the honest answer is recorded=false, got {resp.json}")
        assert_eq(_logout_rows(opts.user_id).count(), before,
                  f"{why}: an unusable selection must write no row")
        assert_eq(Event.objects.filter(
            uid=opts.user_id, category="user_permission_denied").count(),
            denied_before,
            f"{why}: routine membership churn must not file a published "
            f"permission-denied event on every sign-out")

    # 1. A group id that does not exist.
    ghost = (Group.objects.order_by("-pk").values_list("pk", flat=True).first() or 0) + 100000
    _probe({"group": ghost}, "a nonexistent group id")

    # 2. A real group the caller is not a member of.
    _probe({"group": opts.brand_b_id}, "a non-member group")

    # 3. A group whose membership exists but is inactive.
    member = GroupMember.objects.filter(
        user_id=opts.user_id, group_id=opts.brand_a_id).last()
    assert_true(member is not None, "precondition: the brand A membership fixture")
    GroupMember.objects.filter(pk=member.pk).update(is_active=False)
    try:
        _probe({"group": opts.brand_a_id}, "an inactive membership")
    finally:
        GroupMember.objects.filter(pk=member.pk).update(is_active=True)

    # 4. An active child under a deactivated parent (DM-048).
    Group.objects.filter(name="secevents_dark_child").delete()
    Group.objects.filter(name="secevents_dark_parent").delete()
    dark_parent = Group.objects.create(
        name="secevents_dark_parent", kind="organization")
    dark_child = Group.objects.create(
        name="secevents_dark_child", kind="organization", parent=dark_parent)
    dark_child.add_member(User.objects.get(pk=opts.user_id))
    Group.objects.filter(pk=dark_parent.pk).update(is_active=False)
    try:
        _probe({"group": dark_child.pk}, "an active child under a dark parent")
    finally:
        Group.objects.filter(pk=dark_child.pk).delete()
        Group.objects.filter(pk=dark_parent.pk).delete()

    # 5. The control: a brand the caller really is an active direct member of
    #    records, and carries the attribution. Without this the four checks
    #    above would pass with the whole feature deleted.
    before = _logout_rows(opts.user_id).count()
    resp = _post_logout(opts, {"group": opts.brand_a_id}, account_id=opts.user_id)
    opts.client.logout()
    assert_eq(resp.status_code, 200,
              f"a valid brand selection must succeed, got {resp.status_code}: "
              f"{opts.client.last_response.body}")
    assert_true(resp.json.get("data", {}).get("recorded") is True,
                f"a valid brand selection must record: {resp.json}")
    assert_eq(_logout_rows(opts.user_id).count(), before + 1,
              "the valid selection must write exactly one row")
    row = _logout_rows(opts.user_id).order_by("-id").first()
    assert_eq(row.group_id, opts.brand_a_id,
              f"the row must be attributed to the selected brand, got "
              f"{row.group_id!r}")


@th.django_unit_test("sign-in reaches the feed, and follows the brand-visibility matrix")
def test_signin_reaches_the_feed(opts):
    from mojo.apps.incident.models.event import Event
    from mojo.decorators.limits import clear_rate_limits

    def _fresh_login(body):
        opts.client.logout()
        # Only rows this test creates carry 127.0.0.1 + kind=login once cleared.
        Event.objects.filter(
            uid=opts.user_id, category="login", source_ip="127.0.0.1").delete()
        clear_rate_limits(ip="127.0.0.1", key="login")
        muid = opts.client.session.cookies.get("_muid")
        if muid:
            clear_rate_limits(key="login", muid=muid)
        clear_rate_limits(key="login", account_id=opts.user_id)
        clear_rate_limits(user_id=opts.user_id)
        payload = {"username": TEST_USER, "password": TEST_PWORD}
        payload.update(body)
        resp = opts.client.post("/api/login", payload)
        assert_eq(resp.status_code, 200,
                  f"the sign-in must succeed, got {resp.status_code}: "
                  f"{opts.client.last_response.body}")
        opts.client.access_token = resp.response.data.access_token
        opts.client.is_authenticated = True

    def _live_login_ips(path):
        resp = opts.client.get(path)
        assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}")
        return {r.get("ip") for r in resp.json.get("data", [])
                if r.get("kind") == "login"}

    # --- attributed: visible owner-wide AND under its own brand -----------
    _fresh_login({"group": opts.brand_a_id})
    wide = _live_login_ips("/api/account/security-events?size=100")
    scoped = _live_login_ips(
        f"/api/account/security-events?group={opts.brand_a_id}&size=100")
    assert_true("127.0.0.1" in wide,
                f"a real sign-in must appear in the owner-wide feed: {sorted(wide)}")
    assert_true("127.0.0.1" in scoped,
                f"a sign-in attributed to a brand belongs on that brand's "
                f"Security page: {sorted(scoped)}")
    other = _live_login_ips(
        f"/api/account/security-events?group={opts.brand_b_id}&size=100")
    assert_true("127.0.0.1" not in other,
                f"it must NOT appear under a different brand: {sorted(other)}")

    # --- unattributed: owner-wide only (the settled visibility matrix) ----
    _fresh_login({})
    wide = _live_login_ips("/api/account/security-events?size=100")
    scoped = _live_login_ips(
        f"/api/account/security-events?group={opts.brand_a_id}&size=100")
    opts.client.logout()
    assert_true("127.0.0.1" in wide,
                f"an unattributed sign-in is still the caller's own history: "
                f"{sorted(wide)}")
    assert_true("127.0.0.1" not in scoped,
                f"a sign-in that carried no brand context stays in the owner-wide "
                f"view only: {sorted(scoped)}")
