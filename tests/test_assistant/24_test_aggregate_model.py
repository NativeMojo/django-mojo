"""Tests for the aggregate_model assistant tool."""
from testit import helpers as th


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

@th.django_unit_setup()
@th.requires_app("mojo.apps.assistant")
def setup_aggregate(opts):
    from mojo.apps.account.models import User, Group
    from mojo.apps.incident.models import Event

    # Clean up test users
    User.objects.filter(email__in=["aggtest_admin@test.com", "aggtest_nopriv@test.com"]).delete()

    opts.admin = User.objects.create_user(
        username="aggtest_admin@test.com", email="aggtest_admin@test.com", password="pass123",
    )
    opts.admin.is_email_verified = True
    opts.admin.save()
    opts.admin.add_permission("view_admin")
    opts.admin.add_permission("view_security")

    opts.nopriv = User.objects.create_user(
        username="aggtest_nopriv@test.com", email="aggtest_nopriv@test.com", password="pass123",
    )
    opts.nopriv.is_email_verified = True
    opts.nopriv.save()
    opts.nopriv.add_permission("view_admin")

    # Two groups used for FK group_by + having tests. Cleanup first so reruns
    # against the long-lived test DB stay deterministic.
    Group.objects.filter(name__startswith="aggtest_group_").delete()
    opts.group_a = Group.objects.create(name="aggtest_group_a", kind="aggtest")
    opts.group_b = Group.objects.create(name="aggtest_group_b", kind="aggtest")

    # Baseline 6 events with no group (existing flat / non-FK tests).
    # Also wipe FK fixture rows from prior runs: Event.group uses
    # SET_NULL, so deleting the prior aggtest_group_* groups would
    # leave category=fk_test events with group_id=None, polluting
    # the column-name group_by count assertion (3 distinct groups
    # instead of 2: new_a, new_b, None).
    Event.objects.filter(title__startswith="aggtest_").delete()
    Event.objects.filter(title__startswith="aggfk_").delete()
    for i in range(6):
        Event.objects.create(
            title=f"aggtest_event_{i}",
            details=f"Aggregate test event {i}",
            category="auth" if i < 3 else "network",
            level=i + 1,
            scope="global",
        )

    # FK fixture: group_a gets 3 events, group_b gets 1. Used to exercise
    # group_by on a forward FK and `having` thresholds.
    for i in range(3):
        Event.objects.create(
            title=f"aggfk_a_{i}",
            details=f"FK group A event {i}",
            category="fk_test",
            level=2,
            scope="global",
            group=opts.group_a,
        )
    Event.objects.create(
        title="aggfk_b_0",
        details="FK group B event 0",
        category="fk_test",
        level=2,
        scope="global",
        group=opts.group_b,
    )


def _aggregate(params, user):
    from mojo.apps.assistant.services.tools.models import _tool_aggregate_model
    return _tool_aggregate_model(params, user)


# ---------------------------------------------------------------------------
# Flat aggregates
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_aggregate_count(opts):
    result = _aggregate({
        "app_name": "incident", "model_name": "Event",
        "filters": {"title__startswith": "aggtest_"},
        "aggregations": [{"field": "id", "func": "count"}],
    }, opts.admin)
    assert "error" not in result, f"Should succeed: {result.get('error')}"
    assert result["results"]["count_id"] == 6, \
        f"Expected count=6, got {result['results'].get('count_id')}"


@th.django_unit_test()
def test_aggregate_sum(opts):
    result = _aggregate({
        "app_name": "incident", "model_name": "Event",
        "filters": {"title__startswith": "aggtest_"},
        "aggregations": [{"field": "level", "func": "sum"}],
    }, opts.admin)
    assert "error" not in result, f"Should succeed: {result.get('error')}"
    expected = sum(range(1, 7))  # 1+2+3+4+5+6 = 21
    assert result["results"]["sum_level"] == expected, \
        f"Expected sum={expected}, got {result['results'].get('sum_level')}"


@th.django_unit_test()
def test_aggregate_avg(opts):
    result = _aggregate({
        "app_name": "incident", "model_name": "Event",
        "filters": {"title__startswith": "aggtest_"},
        "aggregations": [{"field": "level", "func": "avg"}],
    }, opts.admin)
    assert "error" not in result, f"Should succeed: {result.get('error')}"
    avg_val = result["results"]["avg_level"]
    assert abs(avg_val - 3.5) < 0.01, f"Expected avg=3.5, got {avg_val}"


@th.django_unit_test()
def test_aggregate_min_max(opts):
    result = _aggregate({
        "app_name": "incident", "model_name": "Event",
        "filters": {"title__startswith": "aggtest_"},
        "aggregations": [
            {"field": "level", "func": "min"},
            {"field": "level", "func": "max"},
        ],
    }, opts.admin)
    assert "error" not in result, f"Should succeed: {result.get('error')}"
    assert result["results"]["min_level"] == 1, \
        f"Expected min=1, got {result['results'].get('min_level')}"
    assert result["results"]["max_level"] == 6, \
        f"Expected max=6, got {result['results'].get('max_level')}"


@th.django_unit_test()
def test_aggregate_count_distinct(opts):
    result = _aggregate({
        "app_name": "incident", "model_name": "Event",
        "filters": {"title__startswith": "aggtest_"},
        "aggregations": [{"field": "category", "func": "count_distinct"}],
    }, opts.admin)
    assert "error" not in result, f"Should succeed: {result.get('error')}"
    assert result["results"]["count_distinct_category"] == 2, \
        f"Expected 2 distinct categories, got {result['results'].get('count_distinct_category')}"


@th.django_unit_test()
def test_aggregate_custom_alias(opts):
    result = _aggregate({
        "app_name": "incident", "model_name": "Event",
        "filters": {"title__startswith": "aggtest_"},
        "aggregations": [{"field": "id", "func": "count", "alias": "total"}],
    }, opts.admin)
    assert "error" not in result, f"Should succeed: {result.get('error')}"
    assert "total" in result["results"], \
        f"Expected custom alias 'total' in results, got keys: {list(result['results'].keys())}"
    assert result["results"]["total"] == 6, \
        f"Expected total=6, got {result['results']['total']}"


# ---------------------------------------------------------------------------
# Group by
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_aggregate_group_by(opts):
    result = _aggregate({
        "app_name": "incident", "model_name": "Event",
        "filters": {"title__startswith": "aggtest_"},
        "aggregations": [{"field": "id", "func": "count", "alias": "total"}],
        "group_by": ["category"],
    }, opts.admin)
    assert "error" not in result, f"Should succeed: {result.get('error')}"
    assert result["group_by"] == ["category"], \
        f"Expected group_by=['category'], got {result.get('group_by')}"
    assert result["count"] == 2, f"Expected 2 groups, got {result['count']}"
    rows = result["results"]
    categories = {r["category"]: r["total"] for r in rows}
    assert categories.get("auth") == 3, f"Expected auth=3, got {categories}"
    assert categories.get("network") == 3, f"Expected network=3, got {categories}"


@th.django_unit_test()
def test_aggregate_group_by_with_ordering(opts):
    result = _aggregate({
        "app_name": "incident", "model_name": "Event",
        "filters": {"title__startswith": "aggtest_"},
        "aggregations": [{"field": "id", "func": "count", "alias": "total"}],
        "group_by": ["category"],
        "ordering": "-total",
    }, opts.admin)
    assert "error" not in result, f"Should succeed: {result.get('error')}"
    rows = result["results"]
    # Both groups have 3, so just verify no error and results returned
    assert len(rows) == 2, f"Expected 2 groups, got {len(rows)}"


# ---------------------------------------------------------------------------
# Validation and security
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_aggregate_permission_denied(opts):
    result = _aggregate({
        "app_name": "incident", "model_name": "Event",
        "aggregations": [{"field": "id", "func": "count"}],
    }, opts.nopriv)
    assert "error" in result, "Should be denied without view_security"
    assert "Permission denied" in result["error"], f"Error: {result['error']}"


@th.django_unit_test()
def test_aggregate_rejects_sensitive_field(opts):
    # Use incident.Event which opts.admin has access to, and inject a sensitive field name
    result = _aggregate({
        "app_name": "incident", "model_name": "Event",
        "aggregations": [{"field": "secret_token", "func": "count"}],
    }, opts.admin)
    assert "error" in result, "Should reject sensitive field in aggregation"
    assert "not allowed" in result["error"], f"Error: {result['error']}"


@th.django_unit_test()
def test_aggregate_rejects_sensitive_group_by(opts):
    result = _aggregate({
        "app_name": "incident", "model_name": "Event",
        "aggregations": [{"field": "id", "func": "count"}],
        "group_by": ["password"],
    }, opts.admin)
    assert "error" in result, "Should reject sensitive field in group_by"
    assert "sensitive" in result["error"].lower(), f"Error: {result['error']}"


@th.django_unit_test()
def test_aggregate_rejects_unknown_field(opts):
    result = _aggregate({
        "app_name": "incident", "model_name": "Event",
        "aggregations": [{"field": "nonexistent", "func": "count"}],
    }, opts.admin)
    assert "error" in result, "Should reject unknown field"
    assert "Unknown field" in result["error"], f"Error: {result['error']}"


@th.django_unit_test()
def test_aggregate_sum_on_string_field(opts):
    result = _aggregate({
        "app_name": "incident", "model_name": "Event",
        "aggregations": [{"field": "title", "func": "sum"}],
    }, opts.admin)
    assert "error" in result, "Should reject sum on non-numeric field"
    assert "non-numeric" in result["error"], f"Error: {result['error']}"


@th.django_unit_test()
def test_aggregate_empty_aggregations(opts):
    result = _aggregate({
        "app_name": "incident", "model_name": "Event",
        "aggregations": [],
    }, opts.admin)
    assert "error" in result, "Should reject empty aggregations"


@th.django_unit_test()
def test_aggregate_invalid_func(opts):
    result = _aggregate({
        "app_name": "incident", "model_name": "Event",
        "aggregations": [{"field": "id", "func": "median"}],
    }, opts.admin)
    assert "error" in result, "Should reject invalid aggregate function"


@th.django_unit_test()
def test_aggregate_bad_model(opts):
    result = _aggregate({
        "app_name": "fake", "model_name": "FakeModel",
        "aggregations": [{"field": "id", "func": "count"}],
    }, opts.admin)
    assert "error" in result, "Should error for nonexistent model"


# ---------------------------------------------------------------------------
# FK group_by + having
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_aggregate_group_by_fk_relation_name(opts):
    """group_by with the FK relation name resolves to the column attname."""
    result = _aggregate({
        "app_name": "incident", "model_name": "Event",
        "filters": {"category": "fk_test"},
        "aggregations": [{"field": "id", "func": "count", "alias": "total"}],
        "group_by": ["group"],
    }, opts.admin)
    assert "error" not in result, f"FK group_by should succeed: {result.get('error')}"
    assert result["group_by"] == ["group_id"], \
        f"Resolved group_by should be column name, got {result.get('group_by')}"
    by_group = {r["group_id"]: r["total"] for r in result["results"]}
    assert by_group.get(opts.group_a.id) == 3, \
        f"Expected group_a count=3, got {by_group}"
    assert by_group.get(opts.group_b.id) == 1, \
        f"Expected group_b count=1, got {by_group}"


@th.django_unit_test()
def test_aggregate_group_by_fk_column_name(opts):
    """group_by accepts the column name (e.g. 'group_id') directly."""
    result = _aggregate({
        "app_name": "incident", "model_name": "Event",
        "filters": {"category": "fk_test"},
        "aggregations": [{"field": "id", "func": "count", "alias": "total"}],
        "group_by": ["group_id"],
    }, opts.admin)
    assert "error" not in result, f"FK column group_by should succeed: {result.get('error')}"
    assert result["group_by"] == ["group_id"], \
        f"Resolved group_by should be column name, got {result.get('group_by')}"
    assert result["count"] == 2, f"Expected 2 groups, got {result.get('count')}"


@th.django_unit_test()
def test_aggregate_count_distinct_on_fk(opts):
    """count_distinct on a FK field counts distinct related pks."""
    result = _aggregate({
        "app_name": "incident", "model_name": "Event",
        "filters": {"category": "fk_test"},
        "aggregations": [{"field": "group", "func": "count_distinct", "alias": "groups"}],
    }, opts.admin)
    assert "error" not in result, f"count_distinct on FK should succeed: {result.get('error')}"
    assert result["results"]["groups"] == 2, \
        f"Expected 2 distinct groups, got {result['results'].get('groups')}"


@th.django_unit_test()
def test_aggregate_having_filters_below_threshold(opts):
    """having with __gte filters out groups below the threshold (HAVING semantics)."""
    result = _aggregate({
        "app_name": "incident", "model_name": "Event",
        "filters": {"category": "fk_test"},
        "aggregations": [{"field": "id", "func": "count", "alias": "total"}],
        "group_by": ["group"],
        "having": {"total__gte": 2},
    }, opts.admin)
    assert "error" not in result, f"having should succeed: {result.get('error')}"
    assert result["count"] == 1, \
        f"having total__gte=2 should leave 1 group, got {result.get('count')}"
    row = result["results"][0]
    assert row["group_id"] == opts.group_a.id, \
        f"Surviving row should be group_a, got {row}"
    assert row["total"] == 3, f"Surviving row total should be 3, got {row}"


@th.django_unit_test()
def test_aggregate_having_requires_group_by(opts):
    """having without group_by is rejected."""
    result = _aggregate({
        "app_name": "incident", "model_name": "Event",
        "filters": {"category": "fk_test"},
        "aggregations": [{"field": "id", "func": "count", "alias": "total"}],
        "having": {"total__gte": 1},
    }, opts.admin)
    assert "error" in result, "having without group_by should be rejected"
    assert "group_by" in result["error"], f"Error should mention group_by: {result['error']}"


@th.django_unit_test()
def test_aggregate_having_unknown_alias(opts):
    """having key that does not match any aggregation alias is rejected."""
    result = _aggregate({
        "app_name": "incident", "model_name": "Event",
        "filters": {"category": "fk_test"},
        "aggregations": [{"field": "id", "func": "count", "alias": "total"}],
        "group_by": ["group"],
        "having": {"bogus_alias__gte": 2},
    }, opts.admin)
    assert "error" in result, "having with unknown alias should be rejected"
    assert "Unknown having key" in result["error"], f"Error: {result['error']}"


@th.django_unit_test()
def test_aggregate_having_invalid_lookup(opts):
    """having with a non-scalar lookup suffix is rejected."""
    result = _aggregate({
        "app_name": "incident", "model_name": "Event",
        "filters": {"category": "fk_test"},
        "aggregations": [{"field": "id", "func": "count", "alias": "total"}],
        "group_by": ["group"],
        "having": {"total__icontains": "bad"},
    }, opts.admin)
    assert "error" in result, "having with invalid lookup should be rejected"
    assert "Invalid having lookup" in result["error"], f"Error: {result['error']}"


@th.django_unit_test()
def test_aggregate_group_by_rejects_reverse_relation(opts):
    """group_by on a reverse FK accessor is rejected."""
    # Incident has events = ForeignKey(Incident, related_name='events') on Event.
    # The reverse accessor 'events' on Incident is not a valid group_by target.
    result = _aggregate({
        "app_name": "incident", "model_name": "Incident",
        "aggregations": [{"field": "id", "func": "count"}],
        "group_by": ["events"],
    }, opts.admin)
    assert "error" in result, "Reverse relation group_by should be rejected"
    assert "Unknown group_by field" in result["error"], f"Error: {result['error']}"


@th.django_unit_test()
def test_aggregate_ordering_validates_against_known_keys(opts):
    """Ordering by a field that is neither a group_by column nor an alias is rejected."""
    result = _aggregate({
        "app_name": "incident", "model_name": "Event",
        "filters": {"category": "fk_test"},
        "aggregations": [{"field": "id", "func": "count", "alias": "total"}],
        "group_by": ["group"],
        "ordering": "-bogus",
    }, opts.admin)
    assert "error" in result, "Bogus ordering field should be rejected"
    assert "must match" in result["error"], f"Error: {result['error']}"


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_aggregate_model_registered(opts):
    from mojo.apps.assistant import get_registry
    registry = get_registry()
    assert "aggregate_model" in registry, "aggregate_model should be registered"
    entry = registry["aggregate_model"]
    assert entry["permission"] == "view_admin", f"Permission: {entry['permission']}"
    assert entry["core"] is True, "Should be core tool"
    assert entry["domain"] == "models", f"Domain: {entry['domain']}"
