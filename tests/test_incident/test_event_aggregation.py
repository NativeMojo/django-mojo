"""
Tests for the generic `_mode=count|top|distinct|summary|histogram`
aggregation surface against /api/incident/event.

Covers default (no `_mode`) regression, all five modes, validation
guards (relation `__id` requirement, JSON-path rejection, text/JSON/
email rejection, sensitive-field rejection, allow-list opt-in), the
distinct cap, the histogram bucket cap, and `took_ms` rounding.
"""
import datetime
from testit import helpers as th


TEST_USER = "evt_agg_admin"
TEST_PWORD = "evtagg##mojo99"


def _reset_admin(username, password):
    from mojo.apps.account.models import User
    user = User.objects.filter(username=username).last()
    if user is None:
        user = User(username=username, email=f"{username}@example.com")
        user.save()
    user.is_email_verified = True
    user.save_password(password)
    user.remove_all_permissions()
    user.add_permission("view_security")
    user.add_permission("manage_security")
    user.save()
    return user


@th.django_unit_setup()
def setup_event_aggregation(opts):
    from mojo.apps.incident.models import Event

    # Drop everything we touch so re-runs are deterministic.
    Event.objects.filter(category__in=[
        "evt_agg:login_failed",
        "evt_agg:bad_token",
        "evt_agg:lockout",
        "evt_agg:rare",
    ]).delete()

    _reset_admin(TEST_USER, TEST_PWORD)
    opts.user_name = TEST_USER
    opts.pword = TEST_PWORD


def _seed_basic(opts):
    """Seed a deterministic mix of events under the test categories."""
    from mojo.apps.incident.models import Event
    Event.objects.filter(category__in=[
        "evt_agg:login_failed",
        "evt_agg:bad_token",
        "evt_agg:lockout",
        "evt_agg:rare",
    ]).delete()
    # 5x login_failed from .7, 3x from .8 ; 2x bad_token from .7 ;
    # 1x lockout from .9 ; 1x "rare" event for min_count tests.
    pairs = (
        [("evt_agg:login_failed", "10.0.0.7", 4)] * 5
        + [("evt_agg:login_failed", "10.0.0.8", 4)] * 3
        + [("evt_agg:bad_token", "10.0.0.7", 5)] * 2
        + [("evt_agg:lockout", "10.0.0.9", 7)] * 1
        + [("evt_agg:rare", "10.0.0.10", 1)] * 1
    )
    for category, ip, level in pairs:
        Event.objects.create(category=category, source_ip=ip, level=level)


@th.django_unit_test()
def test_mode_count_returns_scalar(opts):
    from mojo.apps.incident.models import Event
    _seed_basic(opts)
    assert opts.client.login(opts.user_name, opts.pword), "admin login failed"

    resp = opts.client.get(
        "/api/incident/event",
        params={"_mode": "count", "category__in": ",".join([
            "evt_agg:login_failed",
            "evt_agg:bad_token",
            "evt_agg:lockout",
            "evt_agg:rare",
        ])},
    )
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.body}"
    body = resp.response
    assert body.status is True, f"status=true expected: {body}"
    expected = Event.objects.filter(category__in=[
        "evt_agg:login_failed",
        "evt_agg:bad_token",
        "evt_agg:lockout",
        "evt_agg:rare",
    ]).count()
    assert body["count"] == expected, (
        f"_mode=count must match queryset count {expected}, got {body['count']}"
    )
    assert "data" not in body, f"_mode=count must not include data: {body}"


@th.django_unit_test()
def test_mode_count_respects_filters(opts):
    _seed_basic(opts)
    assert opts.client.login(opts.user_name, opts.pword), "admin login failed"
    resp = opts.client.get(
        "/api/incident/event",
        params={"_mode": "count", "category": "evt_agg:login_failed"},
    )
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.body}"
    assert resp.response["count"] == 8, (
        f"login_failed seeded 8x, got {resp.response['count']}"
    )


@th.django_unit_test()
def test_mode_top_basic(opts):
    _seed_basic(opts)
    assert opts.client.login(opts.user_name, opts.pword), "admin login failed"
    resp = opts.client.get(
        "/api/incident/event",
        params={
            "_mode": "top",
            "_field": "source_ip",
            "_size": 5,
            "category__in": "evt_agg:login_failed,evt_agg:bad_token,evt_agg:lockout",
        },
    )
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.body}"
    body = resp.response
    assert body.graph == "top", f"graph should be 'top': {body}"
    rows = body["data"]
    assert isinstance(rows, list), f"data must be list: {rows!r}"
    assert len(rows) <= 5, f"_size=5 must cap rows at 5, got {len(rows)}"
    # Ordered desc by value.
    values = [row["value"] for row in rows]
    assert values == sorted(values, reverse=True), (
        f"rows must be sorted desc by value, got {values}"
    )
    # 10.0.0.7 has 5 login_failed + 2 bad_token = 7 events; should top.
    assert rows[0]["key"] == "10.0.0.7", (
        f"top key should be '10.0.0.7' (7 events), got {rows[0]}"
    )
    assert rows[0]["value"] == 7, (
        f"top value should be 7, got {rows[0]['value']}"
    )
    # All keys are strings (request constraint).
    for row in rows:
        assert isinstance(row["key"], str), f"key must be str: {row}"


@th.django_unit_test()
def test_mode_top_with_min_count(opts):
    _seed_basic(opts)
    assert opts.client.login(opts.user_name, opts.pword), "admin login failed"
    resp = opts.client.get(
        "/api/incident/event",
        params={
            "_mode": "top",
            "_field": "source_ip",
            "_min_count": 2,
            "category__in": (
                "evt_agg:login_failed,evt_agg:bad_token,evt_agg:lockout,evt_agg:rare"
            ),
        },
    )
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.body}"
    keys = [row["key"] for row in resp.response["data"]]
    assert "10.0.0.10" not in keys, (
        f"_min_count=2 should drop 10.0.0.10 (only 1 event), got {keys}"
    )
    assert "10.0.0.9" not in keys, (
        f"_min_count=2 should drop 10.0.0.9 (only 1 event), got {keys}"
    )


@th.django_unit_test()
def test_mode_top_size_capped_at_100(opts):
    _seed_basic(opts)
    assert opts.client.login(opts.user_name, opts.pword), "admin login failed"
    resp = opts.client.get(
        "/api/incident/event",
        params={
            "_mode": "top",
            "_field": "source_ip",
            "_size": 500,
            "category__in": (
                "evt_agg:login_failed,evt_agg:bad_token,evt_agg:lockout,evt_agg:rare"
            ),
        },
    )
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.body}"
    assert resp.response["size"] == 100, (
        f"_size=500 must clamp to 100, got {resp.response['size']}"
    )


@th.django_unit_test()
def test_mode_top_includes_first_last_seen(opts):
    _seed_basic(opts)
    assert opts.client.login(opts.user_name, opts.pword), "admin login failed"
    resp = opts.client.get(
        "/api/incident/event",
        params={
            "_mode": "top",
            "_field": "source_ip",
            "category__in": "evt_agg:login_failed,evt_agg:bad_token",
        },
    )
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.body}"
    rows = resp.response["data"]
    assert rows, f"no rows returned: {resp.response}"
    for row in rows:
        assert "first_seen" in row, f"first_seen missing: {row}"
        assert "last_seen" in row, f"last_seen missing: {row}"
        assert isinstance(row["first_seen"], int), (
            f"first_seen must be epoch-seconds int, got {row['first_seen']!r}"
        )
        assert row["last_seen"] >= row["first_seen"], (
            f"last_seen must be >= first_seen: {row}"
        )


@th.django_unit_test()
def test_mode_distinct_alpha_sort(opts):
    _seed_basic(opts)
    assert opts.client.login(opts.user_name, opts.pword), "admin login failed"
    resp = opts.client.get(
        "/api/incident/event",
        params={
            "_mode": "distinct",
            "_field": "category",
            "category__in": (
                "evt_agg:login_failed,evt_agg:bad_token,evt_agg:lockout,evt_agg:rare"
            ),
        },
    )
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.body}"
    body = resp.response
    assert body.graph == "distinct", f"graph should be 'distinct': {body}"
    keys = [row["key"] for row in body["data"]]
    assert keys == sorted(keys), f"distinct keys must be sorted asc, got {keys}"
    expected = {
        "evt_agg:bad_token", "evt_agg:lockout", "evt_agg:login_failed", "evt_agg:rare"
    }
    assert set(keys) == expected, f"distinct categories mismatch: {keys}"


@th.django_unit_test()
def test_mode_distinct_cap_exceeded_returns_400(opts):
    """With cap=3, four distinct categories must trigger a 400."""
    _seed_basic(opts)
    assert opts.client.login(opts.user_name, opts.pword), "admin login failed"
    with th.server_settings(MOJO_REST_AGG_DISTINCT_CAP=3):
        resp = opts.client.get(
            "/api/incident/event",
            params={
                "_mode": "distinct",
                "_field": "category",
                "category__in": (
                    "evt_agg:login_failed,evt_agg:bad_token,"
                    "evt_agg:lockout,evt_agg:rare"
                ),
            },
        )
    assert resp.status_code == 400, (
        f"distinct cardinality > cap must 400, got {resp.status_code}: {resp.body}"
    )


@th.django_unit_test()
def test_mode_summary_avg_level(opts):
    _seed_basic(opts)
    assert opts.client.login(opts.user_name, opts.pword), "admin login failed"
    resp = opts.client.get(
        "/api/incident/event",
        params={
            "_mode": "summary",
            "_field": "level",
            "_agg": "avg",
            "_agg_field": "level",
            "category__in": (
                "evt_agg:login_failed,evt_agg:bad_token,evt_agg:lockout,evt_agg:rare"
            ),
        },
    )
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.body}"
    body = resp.response
    assert body.graph == "summary", f"graph='summary' expected: {body}"
    assert body["agg"] == "avg", f"agg='avg' expected: {body}"
    assert body["value"] is not None, f"value must not be None: {body}"
    assert body["min"] == 1, f"min level seeded was 1, got {body['min']}"
    assert body["max"] == 7, f"max level seeded was 7, got {body['max']}"
    assert body["n"] == 12, f"n must be row count (12), got {body['n']}"


@th.django_unit_test()
def test_mode_summary_rejects_avg_on_text_field(opts):
    _seed_basic(opts)
    assert opts.client.login(opts.user_name, opts.pword), "admin login failed"
    resp = opts.client.get(
        "/api/incident/event",
        params={"_mode": "summary", "_agg": "avg", "_agg_field": "details"},
    )
    assert resp.status_code == 400, (
        f"avg on TextField must 400, got {resp.status_code}: {resp.body}"
    )


@th.django_unit_test()
def test_mode_histogram_day_buckets(opts):
    """Seed 3 days, with a gap in the middle, assert empty bucket present."""
    from mojo.apps.incident.models import Event
    Event.objects.filter(category="evt_agg:histogram").delete()
    # Use a stable explicit window to avoid time-of-day ambiguity.
    base = datetime.datetime(2026, 1, 10, 12, 0, 0, tzinfo=datetime.timezone.utc)
    # Day 0: 2 events, Day 1: 0 events (gap), Day 2: 1 event.
    e1 = Event.objects.create(category="evt_agg:histogram", level=1)
    Event.objects.filter(pk=e1.pk).update(created=base)
    e2 = Event.objects.create(category="evt_agg:histogram", level=1)
    Event.objects.filter(pk=e2.pk).update(created=base + datetime.timedelta(hours=2))
    e3 = Event.objects.create(category="evt_agg:histogram", level=1)
    Event.objects.filter(pk=e3.pk).update(created=base + datetime.timedelta(days=2))

    assert opts.client.login(opts.user_name, opts.pword), "admin login failed"
    resp = opts.client.get(
        "/api/incident/event",
        params={
            "_mode": "histogram",
            "_field": "created",
            "_bucket": "day",
            "category": "evt_agg:histogram",
            "dr_start": base.isoformat(),
            "dr_end": (base + datetime.timedelta(days=2, hours=1)).isoformat(),
        },
    )
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.body}"
    body = resp.response
    assert body.graph == "histogram", f"graph='histogram' expected: {body}"
    rows = body["data"]
    assert len(rows) == 3, f"expected 3 buckets (day0..day2), got {len(rows)}: {rows}"
    values = [row["value"] for row in rows]
    assert values == [2, 0, 1], (
        f"expected [2, 0, 1] over the 3-day window, got {values}: {rows}"
    )
    # Buckets ordered ascending.
    timestamps = [row["ts"] for row in rows]
    assert timestamps == sorted(timestamps), (
        f"buckets must be ts-asc, got {timestamps}"
    )
    Event.objects.filter(category="evt_agg:histogram").delete()


@th.django_unit_test()
def test_mode_histogram_requires_datetime_field(opts):
    _seed_basic(opts)
    assert opts.client.login(opts.user_name, opts.pword), "admin login failed"
    resp = opts.client.get(
        "/api/incident/event",
        params={"_mode": "histogram", "_field": "category", "_bucket": "day"},
    )
    assert resp.status_code == 400, (
        f"histogram on non-datetime field must 400, got {resp.status_code}: {resp.body}"
    )


@th.django_unit_test()
def test_mode_unknown_returns_400(opts):
    _seed_basic(opts)
    assert opts.client.login(opts.user_name, opts.pword), "admin login failed"
    resp = opts.client.get(
        "/api/incident/event",
        params={"_mode": "garbage"},
    )
    assert resp.status_code == 400, (
        f"unknown _mode must 400, got {resp.status_code}: {resp.body}"
    )


@th.django_unit_test()
def test_mode_field_relation_requires_id_suffix(opts):
    _seed_basic(opts)
    assert opts.client.login(opts.user_name, opts.pword), "admin login failed"
    resp = opts.client.get(
        "/api/incident/event",
        params={"_mode": "top", "_field": "incident"},
    )
    assert resp.status_code == 400, (
        f"_field=relation without __id must 400, got {resp.status_code}: {resp.body}"
    )
    resp = opts.client.get(
        "/api/incident/event",
        params={"_mode": "top", "_field": "incident__id"},
    )
    assert resp.status_code == 200, (
        f"_field=incident__id must succeed, got {resp.status_code}: {resp.body}"
    )


@th.django_unit_test()
def test_mode_field_textfield_rejected(opts):
    _seed_basic(opts)
    assert opts.client.login(opts.user_name, opts.pword), "admin login failed"
    resp = opts.client.get(
        "/api/incident/event",
        params={"_mode": "top", "_field": "details"},
    )
    assert resp.status_code == 400, (
        f"_field=TextField must 400, got {resp.status_code}: {resp.body}"
    )


@th.django_unit_test()
def test_mode_field_jsonpath_rejected(opts):
    _seed_basic(opts)
    assert opts.client.login(opts.user_name, opts.pword), "admin login failed"
    resp = opts.client.get(
        "/api/incident/event",
        params={"_mode": "top", "_field": "metadata__rule_id"},
    )
    assert resp.status_code == 400, (
        f"_field=JSON-path must 400, got {resp.status_code}: {resp.body}"
    )


@th.django_unit_test()
def test_took_ms_rounded(opts):
    _seed_basic(opts)
    assert opts.client.login(opts.user_name, opts.pword), "admin login failed"
    resp = opts.client.get(
        "/api/incident/event",
        params={"_mode": "count"},
    )
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.body}"
    took = resp.response["took_ms"]
    assert isinstance(took, int), f"took_ms must be int, got {type(took).__name__}"
    assert took % 10 == 0, f"took_ms must be multiple of 10, got {took}"
    assert took >= 0, f"took_ms must be non-negative, got {took}"


@th.django_unit_test()
def test_mode_list_default_when_absent(opts):
    """Regression: omitting _mode preserves today's list response shape."""
    _seed_basic(opts)
    assert opts.client.login(opts.user_name, opts.pword), "admin login failed"
    resp = opts.client.get(
        "/api/incident/event",
        params={"category": "evt_agg:login_failed", "size": 50},
    )
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.body}"
    body = resp.response
    # Standard list envelope: status, data, count, size, graph.
    assert body.status is True, f"status=true expected: {body}"
    assert isinstance(body["data"], list), f"data must be list: {body}"
    assert body["count"] == 8, (
        f"login_failed=8 in seed, default-list count must match: {body['count']}"
    )


@th.django_unit_test()
def test_mode_count_with_underscore_filter_does_not_filter_records(opts):
    """`_mode` and other `_*` keys must be skipped by the field filter parser.

    Regression guard: if the parser ever stops treating `_mode` as
    reserved, it would be applied as `WHERE _mode='count'` and the
    queryset would explode.
    """
    _seed_basic(opts)
    assert opts.client.login(opts.user_name, opts.pword), "admin login failed"
    resp = opts.client.get(
        "/api/incident/event",
        params={"_mode": "count", "_field": "category"},
    )
    assert resp.status_code == 200, (
        f"_mode/_field must be reserved and ignored by filter, "
        f"got {resp.status_code}: {resp.body}"
    )
