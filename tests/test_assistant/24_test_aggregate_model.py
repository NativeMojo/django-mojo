"""Tests for the aggregate_model assistant tool."""
from testit import helpers as th


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

@th.django_unit_setup()
@th.requires_app("mojo.apps.assistant")
def setup_aggregate(opts):
    from mojo.apps.account.models import User
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

    # Create test events with known levels and categories
    Event.objects.filter(title__startswith="aggtest_").delete()
    for i in range(6):
        Event.objects.create(
            title=f"aggtest_event_{i}",
            details=f"Aggregate test event {i}",
            category="auth" if i < 3 else "network",
            level=i + 1,
            scope="global",
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
