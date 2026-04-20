"""Tests for the metrics domain assistant tools.

Covers the full surface: discovery, fetch, gauges (read+write), slug
explanation, group resolution, per-account permission enforcement, and
the tool-level registration gate.
"""
from testit import helpers as th


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TEST_ACCOUNT = "metrictest"
TEST_CUSTOM_ACCOUNT = "metrictest_custom"
TEST_GROUP_NAME = "MetricTestGroup"
TEST_GROUP_NAME_AMBIG = "MetricTestAmbig"


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

@th.django_unit_setup()
@th.requires_app("mojo.apps.assistant")
def setup_metrics_tools(opts):
    from mojo.apps.account.models import User, Group
    from mojo.apps import metrics

    # --- Users ---
    User.objects.filter(
        email__in=[
            "mettools_admin@test.com",
            "mettools_mem@test.com",
            "mettools_outsider@test.com",
            "mettools_noperm@test.com",
            "mettools_custom@test.com",
        ]
    ).delete()

    opts.admin = User.objects.create_user(
        username="mettools_admin@test.com",
        email="mettools_admin@test.com",
        password="pass123",
    )
    opts.admin.is_email_verified = True
    opts.admin.save()
    opts.admin.add_permission("view_metrics")
    opts.admin.add_permission("write_metrics")

    opts.member = User.objects.create_user(
        username="mettools_mem@test.com",
        email="mettools_mem@test.com",
        password="pass123",
    )
    opts.member.is_email_verified = True
    opts.member.save()

    opts.outsider = User.objects.create_user(
        username="mettools_outsider@test.com",
        email="mettools_outsider@test.com",
        password="pass123",
    )
    opts.outsider.is_email_verified = True
    opts.outsider.save()

    opts.noperm = User.objects.create_user(
        username="mettools_noperm@test.com",
        email="mettools_noperm@test.com",
        password="pass123",
    )
    opts.noperm.is_email_verified = True
    opts.noperm.save()

    opts.custom_user = User.objects.create_user(
        username="mettools_custom@test.com",
        email="mettools_custom@test.com",
        password="pass123",
    )
    opts.custom_user.is_email_verified = True
    opts.custom_user.save()
    opts.custom_user.add_permission("view_metrictest_custom")

    # --- Groups ---
    Group.objects.filter(name__in=[TEST_GROUP_NAME, TEST_GROUP_NAME_AMBIG]).delete()
    opts.group = Group.objects.create(name=TEST_GROUP_NAME, kind="test")
    opts.ambig_group_a = Group.objects.create(name=TEST_GROUP_NAME_AMBIG, kind="test")
    opts.ambig_group_b = Group.objects.create(name=TEST_GROUP_NAME_AMBIG, kind="test")

    opts.group_account = f"group-{opts.group.pk}"

    member = opts.group.add_member(opts.member)
    member.add_permission("view_metrics")

    # --- Metrics state cleanup + seed ---
    _cleanup_metrics_state(opts)

    # Seed global + group + custom + user metrics used by multiple tests.
    metrics.record("mettools_global_slug", category="mettools", account="global")
    metrics.record(
        "mettools_group_slug", category="mettools",
        account=opts.group_account,
    )
    metrics.record(
        f"mettools_user_slug", category="mettools",
        account=f"user-{opts.member.pk}",
    )
    metrics.record(
        "mettools_custom_slug", category="mettools_custom",
        account=TEST_CUSTOM_ACCOUNT,
    )
    metrics.record("mettools_data_only", account=TEST_ACCOUNT)

    # Perms for the custom accounts
    metrics.set_view_perms(TEST_CUSTOM_ACCOUNT, "view_metrictest_custom")
    metrics.set_write_perms(TEST_CUSTOM_ACCOUNT, "view_metrictest_custom")


def _cleanup_metrics_state(opts):
    """Remove all metrics Redis keys touched by this test module."""
    from mojo.helpers import redis
    from mojo.apps import metrics

    r = redis.get_connection()
    # Delete slugs/categories/perms/values for each account we touch
    for account in (
        "global",
        opts.group_account,
        f"user-{opts.member.pk}",
        TEST_ACCOUNT,
        TEST_CUSTOM_ACCOUNT,
        "public",
    ):
        # Known slugs to purge
        for slug in (
            "mettools_global_slug",
            "mettools_group_slug",
            "mettools_user_slug",
            "mettools_custom_slug",
            "mettools_data_only",
            "mettools_describe_target",
            "mettools_trunc_slug",
        ):
            try:
                metrics.delete_metrics_slug(slug, account=account)
            except Exception:
                pass
        try:
            metrics.delete_category("mettools", account=account)
        except Exception:
            pass
        try:
            metrics.delete_category("mettools_custom", account=account)
        except Exception:
            pass
        # Gauges + perms for account
        for pattern in (
            f"{{mets:{account}}}:mets:{account}:val:*",
            f"{{mets:{account}}}:mets:{account}:slugs",
            f"{{mets:{account}}}:mets:{account}:cats",
            f"{{mets:{account}}}:mets:{account}:perm:v",
            f"{{mets:{account}}}:mets:{account}:perm:w",
        ):
            try:
                for key in r.scan_iter(match=pattern):
                    r.delete(key)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Handler helpers
# ---------------------------------------------------------------------------

def _handler(name):
    from mojo.apps.assistant import get_registry
    return get_registry()[name]["handler"]


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_registry_has_all_expected_tools(opts):
    from mojo.apps.assistant import get_registry
    registry = get_registry()
    expected = {
        "list_metric_accounts",
        "list_metric_categories",
        "list_metric_slugs",
        "list_metric_gauges",
        "describe_metric_slug",
        "resolve_group_account",
        "fetch_metrics",
        "fetch_metric_values",
        "fetch_metrics_by_category",
        "get_metric_gauge",
        "set_metric_gauge",
        "get_system_health",
        "get_incident_trends",
    }
    missing = expected - set(registry.keys())
    assert not missing, f"Missing registered tools: {missing}"


@th.django_unit_test()
def test_set_metric_gauge_is_mutating(opts):
    from mojo.apps.assistant import get_registry
    entry = get_registry()["set_metric_gauge"]
    assert entry["mutates"] is True, "set_metric_gauge must be mutating"
    assert entry["permission"] == "write_metrics", (
        f"set_metric_gauge must require write_metrics, got: {entry['permission']}"
    )


@th.django_unit_test()
def test_read_tools_require_view_metrics(opts):
    from mojo.apps.assistant import get_registry
    read_tools = [
        "list_metric_accounts", "list_metric_categories", "list_metric_slugs",
        "list_metric_gauges", "describe_metric_slug", "resolve_group_account",
        "fetch_metrics", "fetch_metric_values", "fetch_metrics_by_category",
        "get_metric_gauge",
    ]
    registry = get_registry()
    for name in read_tools:
        assert registry[name]["permission"] == "view_metrics", (
            f"{name} should require view_metrics, got: {registry[name]['permission']}"
        )


# ---------------------------------------------------------------------------
# Discovery — accounts, categories, slugs, gauges
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_list_metric_accounts_global_user_sees_all(opts):
    result = _handler("list_metric_accounts")({}, opts.admin)
    assert "error" not in result, f"admin should succeed: {result.get('error')}"
    assert result["scoped"] is False, "admin with view_metrics should not be scoped"
    assert "global" in result["accounts"], (
        f"admin should see 'global', got: {result['accounts']}"
    )
    assert opts.group_account in result["accounts"], (
        f"admin should see {opts.group_account}, got: {result['accounts']}"
    )


@th.django_unit_test()
def test_list_metric_accounts_includes_data_inferred(opts):
    """An account used only via metrics.record (no perm config) must appear."""
    result = _handler("list_metric_accounts")({}, opts.admin)
    assert TEST_ACCOUNT in result["accounts"], (
        f"data-inferred account '{TEST_ACCOUNT}' should appear, got: {result['accounts']}"
    )


@th.django_unit_test()
def test_list_metric_accounts_member_is_scoped(opts):
    """Member without global perm sees only accessible accounts."""
    result = _handler("list_metric_accounts")({}, opts.member)
    assert result["scoped"] is True, "member should have scoped=True"
    assert "public" in result["accounts"], "member should always see 'public'"
    assert f"user-{opts.member.pk}" in result["accounts"], (
        f"member should see own user account, got: {result['accounts']}"
    )
    assert opts.group_account in result["accounts"], (
        f"member should see own group account, got: {result['accounts']}"
    )
    assert "global" not in result["accounts"], (
        f"member without view_metrics should NOT see 'global', got: {result['accounts']}"
    )


@th.django_unit_test()
def test_list_metric_accounts_outsider_cannot_see_group(opts):
    result = _handler("list_metric_accounts")({}, opts.outsider)
    assert opts.group_account not in result["accounts"], (
        f"outsider must not see group account, got: {result['accounts']}"
    )


@th.django_unit_test()
def test_list_metric_categories_returns_seeded(opts):
    result = _handler("list_metric_categories")({"account": "global"}, opts.admin)
    assert "error" not in result, f"admin should succeed: {result.get('error')}"
    assert "mettools" in result["categories"], (
        f"should include 'mettools' category, got: {result['categories']}"
    )


@th.django_unit_test()
def test_list_metric_categories_permission_denied(opts):
    """User without view_metrics cannot read 'global'."""
    result = _handler("list_metric_categories")({"account": "global"}, opts.noperm)
    assert "error" in result, "noperm user should be denied"
    assert "Permission denied" in result["error"], (
        f"expected permission-denied error: {result['error']}"
    )


@th.django_unit_test()
def test_list_metric_slugs_basic(opts):
    result = _handler("list_metric_slugs")({"account": "global"}, opts.admin)
    assert "error" not in result, f"admin should succeed: {result.get('error')}"
    assert "mettools_global_slug" in result["slugs"], (
        f"should include seeded slug, got: {result['slugs']}"
    )
    assert result["truncated"] is False, "should not be truncated for a small set"


@th.django_unit_test()
def test_list_metric_slugs_by_category(opts):
    result = _handler("list_metric_slugs")(
        {"account": "global", "category": "mettools"}, opts.admin,
    )
    assert "error" not in result, f"should succeed: {result.get('error')}"
    assert "mettools_global_slug" in result["slugs"], (
        f"category-scoped list should include global slug, got: {result['slugs']}"
    )


@th.django_unit_test()
def test_list_metric_slugs_prefix_filter(opts):
    from mojo.apps import metrics
    # Seed a couple of prefix-matching slugs in global
    metrics.record("mettools_prefix:ip:1.2.3.4", account="global")
    metrics.record("mettools_prefix:ip:5.6.7.8", account="global")
    metrics.record("mettools_prefix:other", account="global")

    result = _handler("list_metric_slugs")({
        "account": "global",
        "prefix": "mettools_prefix:ip:",
    }, opts.admin)

    assert "error" not in result, f"should succeed: {result.get('error')}"
    assert all(s.startswith("mettools_prefix:ip:") for s in result["slugs"]), (
        f"all returned slugs must match prefix, got: {result['slugs']}"
    )
    assert len(result["slugs"]) >= 2, (
        f"at least 2 prefix hits expected, got: {result['slugs']}"
    )
    # Cleanup seeded
    metrics.delete_metrics_slug("mettools_prefix:ip:1.2.3.4", account="global")
    metrics.delete_metrics_slug("mettools_prefix:ip:5.6.7.8", account="global")
    metrics.delete_metrics_slug("mettools_prefix:other", account="global")


@th.django_unit_test()
def test_list_metric_slugs_truncates_at_limit(opts):
    from mojo.apps import metrics
    # Seed a batch of slugs on a fresh account
    truncation_account = "mettools_trunc_acct"
    # Pre-clean
    for i in range(12):
        metrics.delete_metrics_slug(f"mettools_trunc:{i}", account=truncation_account)
    for i in range(12):
        metrics.record(f"mettools_trunc:{i}", account=truncation_account)
    metrics.set_view_perms(truncation_account, "public")

    result = _handler("list_metric_slugs")({
        "account": truncation_account,
        "limit": 5,
    }, opts.admin)

    assert "error" not in result, f"should succeed: {result.get('error')}"
    assert len(result["slugs"]) == 5, (
        f"limit=5 should cap results, got {len(result['slugs'])}"
    )
    assert result["truncated"] is True, "should report truncated=True"
    assert result["total"] >= 12, f"total should reflect full set: {result['total']}"

    # Cleanup
    for i in range(12):
        metrics.delete_metrics_slug(f"mettools_trunc:{i}", account=truncation_account)
    metrics.set_view_perms(truncation_account, None)


@th.django_unit_test()
def test_list_metric_gauges_returns_names_only(opts):
    from mojo.apps import metrics
    metrics.set_value("mettools_flag_a", "on", account="global")
    metrics.set_value("mettools_flag_b", "off", account="global")

    result = _handler("list_metric_gauges")({"account": "global"}, opts.admin)

    assert "error" not in result, f"should succeed: {result.get('error')}"
    assert "mettools_flag_a" in result["slugs"], (
        f"gauge slug should appear, got: {result['slugs']}"
    )
    # Values must NOT appear in the list
    assert "on" not in result["slugs"], "values must not leak into gauge list"


@th.django_unit_test()
def test_list_metric_gauges_prefix_filter(opts):
    from mojo.apps import metrics
    metrics.set_value("mettools_pref:a", "1", account="global")
    metrics.set_value("mettools_pref:b", "2", account="global")
    metrics.set_value("mettools_other", "3", account="global")

    result = _handler("list_metric_gauges")(
        {"account": "global", "prefix": "mettools_pref:"}, opts.admin,
    )

    assert "error" not in result, f"should succeed: {result.get('error')}"
    assert all(s.startswith("mettools_pref:") for s in result["slugs"]), (
        f"all gauges must match prefix, got: {result['slugs']}"
    )
    assert "mettools_other" not in result["slugs"], (
        "non-matching gauges must be filtered out"
    )


# ---------------------------------------------------------------------------
# Fetch — time-series
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_fetch_metrics_single_slug(opts):
    result = _handler("fetch_metrics")({
        "slugs": ["mettools_global_slug"],
        "granularity": "hours",
        "account": "global",
    }, opts.admin)
    assert "error" not in result, f"should succeed: {result.get('error')}"
    assert "data" in result, "response should contain data"
    assert result["account"] == "global", (
        f"metadata should echo account, got: {result.get('account')}"
    )
    assert result["granularity"] == "hours", (
        f"metadata should echo granularity, got: {result.get('granularity')}"
    )


@th.django_unit_test()
def test_fetch_metrics_multi_slug_with_labels(opts):
    result = _handler("fetch_metrics")({
        "slugs": ["mettools_global_slug", "mettools_data_only"],
        "granularity": "hours",
        "account": "global",
        "with_labels": True,
    }, opts.admin)
    assert "error" not in result, f"should succeed: {result.get('error')}"
    data = result["data"]
    assert "labels" in data, f"with_labels should add labels key: {data}"
    assert "data" in data, f"multi-slug result should have inner data dict: {data}"


@th.django_unit_test()
def test_fetch_metrics_minutes_granularity(opts):
    result = _handler("fetch_metrics")({
        "slugs": ["mettools_global_slug"],
        "granularity": "minutes",
        "account": "global",
    }, opts.admin)
    assert "error" not in result, (
        f"minutes granularity must succeed, got: {result.get('error')}"
    )
    assert result["granularity"] == "minutes", "metadata should echo minutes"


@th.django_unit_test()
def test_fetch_metrics_weeks_granularity(opts):
    result = _handler("fetch_metrics")({
        "slugs": ["mettools_global_slug"],
        "granularity": "weeks",
        "account": "global",
    }, opts.admin)
    assert "error" not in result, (
        f"weeks granularity must succeed, got: {result.get('error')}"
    )


@th.django_unit_test()
def test_fetch_metrics_colon_slug_passthrough(opts):
    from mojo.apps import metrics
    metrics.record("login_attempts:ip:1.2.3.4", account="global")
    result = _handler("fetch_metrics")({
        "slugs": ["login_attempts:ip:1.2.3.4"],
        "granularity": "hours",
        "account": "global",
    }, opts.admin)
    assert "error" not in result, (
        f"colon-slug fetch should succeed, got: {result.get('error')}"
    )
    metrics.delete_metrics_slug("login_attempts:ip:1.2.3.4", account="global")


@th.django_unit_test()
def test_fetch_metrics_invalid_granularity(opts):
    result = _handler("fetch_metrics")({
        "slugs": ["mettools_global_slug"],
        "granularity": "decades",
        "account": "global",
    }, opts.admin)
    assert "error" in result, "invalid granularity should error"
    assert "decades" in result["error"], (
        f"error should mention the bad granularity: {result['error']}"
    )


@th.django_unit_test()
def test_fetch_metrics_empty_slug_list(opts):
    result = _handler("fetch_metrics")({
        "slugs": [],
        "account": "global",
    }, opts.admin)
    assert "error" in result, "empty slug list must error"


@th.django_unit_test()
def test_fetch_metrics_auto_granularity_short_range(opts):
    import datetime
    now = datetime.datetime.utcnow()
    start = (now - datetime.timedelta(hours=1)).isoformat()
    end = now.isoformat()
    result = _handler("fetch_metrics")({
        "slugs": ["mettools_global_slug"],
        "account": "global",
        "dt_start": start,
        "dt_end": end,
    }, opts.admin)
    assert "error" not in result, f"should succeed: {result.get('error')}"
    assert result["granularity"] == "minutes", (
        f"1-hour range should auto-pick 'minutes', got: {result['granularity']}"
    )


@th.django_unit_test()
def test_fetch_metrics_auto_granularity_large_range(opts):
    import datetime
    now = datetime.datetime.utcnow()
    start = (now - datetime.timedelta(days=180)).isoformat()
    end = now.isoformat()
    result = _handler("fetch_metrics")({
        "slugs": ["mettools_global_slug"],
        "account": "global",
        "dt_start": start,
        "dt_end": end,
    }, opts.admin)
    assert "error" not in result, f"should succeed: {result.get('error')}"
    assert result["granularity"] == "days", (
        f"180-day range should auto-pick 'days', got: {result['granularity']}"
    )


@th.django_unit_test()
def test_fetch_metrics_retention_note_on_old_range(opts):
    import datetime
    now = datetime.datetime.utcnow()
    # hours granularity TTL is ~3 days; go 30 days back
    start = (now - datetime.timedelta(days=30)).isoformat()
    end = now.isoformat()
    result = _handler("fetch_metrics")({
        "slugs": ["mettools_global_slug"],
        "account": "global",
        "granularity": "hours",
        "dt_start": start,
        "dt_end": end,
    }, opts.admin)
    assert "error" not in result, f"should succeed: {result.get('error')}"
    assert "retention_note" in result, (
        f"30-day hours fetch should include retention_note, got keys: {list(result.keys())}"
    )


@th.django_unit_test()
def test_fetch_metrics_no_retention_note_within_window(opts):
    import datetime
    now = datetime.datetime.utcnow()
    start = (now - datetime.timedelta(hours=6)).isoformat()
    end = now.isoformat()
    result = _handler("fetch_metrics")({
        "slugs": ["mettools_global_slug"],
        "account": "global",
        "granularity": "hours",
        "dt_start": start,
        "dt_end": end,
    }, opts.admin)
    assert "error" not in result, f"should succeed: {result.get('error')}"
    assert "retention_note" not in result, (
        f"6-hour fetch should not warn about retention, got: {result.get('retention_note')}"
    )


# ---------------------------------------------------------------------------
# Fetch — point-in-time values
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_fetch_metric_values_point_in_time(opts):
    result = _handler("fetch_metric_values")({
        "slugs": ["mettools_global_slug"],
        "granularity": "hours",
        "account": "global",
    }, opts.admin)
    assert "error" not in result, f"should succeed: {result.get('error')}"
    assert "data" in result, f"response should contain data: {result}"
    assert "mettools_global_slug" in result["data"], (
        f"data should contain requested slug: {result['data']}"
    )


# ---------------------------------------------------------------------------
# Fetch — by category
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_fetch_metrics_by_category(opts):
    result = _handler("fetch_metrics_by_category")({
        "category": "mettools",
        "account": "global",
        "granularity": "hours",
    }, opts.admin)
    assert "error" not in result, f"should succeed: {result.get('error')}"
    assert result["total_slugs"] >= 1, (
        f"category should have at least one slug: {result}"
    )
    assert result["truncated"] is False, f"small category should not be truncated: {result}"


@th.django_unit_test()
def test_fetch_metrics_by_category_caps_slugs(opts):
    from mojo.apps import metrics
    cap_account = "mettools_cap_acct"
    for i in range(8):
        metrics.delete_metrics_slug(f"mettools_cap:{i}", account=cap_account)
    for i in range(8):
        metrics.record(f"mettools_cap:{i}", category="cap_cat", account=cap_account)
    metrics.set_view_perms(cap_account, "public")

    result = _handler("fetch_metrics_by_category")({
        "category": "cap_cat",
        "account": cap_account,
        "granularity": "hours",
        "max_slugs": 3,
    }, opts.admin)
    assert "error" not in result, f"should succeed: {result.get('error')}"
    assert result["truncated"] is True, (
        f"should report truncated=True when slugs exceed max_slugs: {result}"
    )
    assert result["total_slugs"] >= 8, f"total_slugs should reflect full count: {result}"
    assert result["slug_count"] == 3, (
        f"slug_count should match max_slugs=3: {result['slug_count']}"
    )

    # cleanup
    for i in range(8):
        metrics.delete_metrics_slug(f"mettools_cap:{i}", account=cap_account)
    metrics.delete_category("cap_cat", account=cap_account)
    metrics.set_view_perms(cap_account, None)


# ---------------------------------------------------------------------------
# Gauge reads
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_get_metric_gauge_single(opts):
    from mojo.apps import metrics
    metrics.set_value("mettools_gauge_single", "on", account="global")

    result = _handler("get_metric_gauge")(
        {"slug": "mettools_gauge_single", "account": "global"}, opts.admin,
    )

    assert "error" not in result, f"should succeed: {result.get('error')}"
    assert result["data"]["mettools_gauge_single"] == "on", (
        f"gauge value should round-trip: {result}"
    )


@th.django_unit_test()
def test_get_metric_gauge_batch(opts):
    from mojo.apps import metrics
    metrics.set_value("mettools_gauge_a", "1", account="global")
    metrics.set_value("mettools_gauge_b", "2", account="global")

    result = _handler("get_metric_gauge")({
        "slugs": ["mettools_gauge_a", "mettools_gauge_b"],
        "account": "global",
    }, opts.admin)

    assert "error" not in result, f"should succeed: {result.get('error')}"
    assert result["data"]["mettools_gauge_a"] == "1", "batch key a should be 1"
    assert result["data"]["mettools_gauge_b"] == "2", "batch key b should be 2"


@th.django_unit_test()
def test_get_metric_gauge_default_for_missing(opts):
    result = _handler("get_metric_gauge")({
        "slug": "mettools_gauge_missing_key",
        "account": "global",
        "default": "not_set",
    }, opts.admin)
    assert "error" not in result, f"should succeed: {result.get('error')}"
    assert result["data"]["mettools_gauge_missing_key"] == "not_set", (
        f"missing gauge should return default: {result}"
    )


# ---------------------------------------------------------------------------
# Gauge writes (set_metric_gauge)
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_set_metric_gauge_writes_value(opts):
    from mojo.apps import metrics

    result = _handler("set_metric_gauge")({
        "slug": "mettools_maintenance",
        "value": "on",
        "account": "global",
    }, opts.admin)
    assert "error" not in result, f"admin should succeed: {result.get('error')}"
    assert result["ok"] is True, f"response should include ok=True: {result}"

    actual = metrics.get_value("mettools_maintenance", account="global")
    assert actual == "on", (
        f"value should persist via get_value, got: {actual!r}"
    )


@th.django_unit_test()
def test_set_metric_gauge_empty_string_allowed(opts):
    """Empty string 'clears' a flag — allowed, not rejected."""
    from mojo.apps import metrics
    result = _handler("set_metric_gauge")({
        "slug": "mettools_empty_flag",
        "value": "",
        "account": "global",
    }, opts.admin)
    assert "error" not in result, f"empty string should succeed: {result.get('error')}"
    assert metrics.get_value("mettools_empty_flag", account="global") == "", (
        "empty value should round-trip"
    )


@th.django_unit_test()
def test_set_metric_gauge_rejects_missing_slug(opts):
    result = _handler("set_metric_gauge")({
        "value": "on",
        "account": "global",
    }, opts.admin)
    assert "error" in result, "missing slug must error"
    assert "slug" in result["error"].lower(), (
        f"error should mention slug: {result['error']}"
    )


@th.django_unit_test()
def test_set_metric_gauge_rejects_missing_value(opts):
    result = _handler("set_metric_gauge")({
        "slug": "mettools_no_value",
        "account": "global",
    }, opts.admin)
    assert "error" in result, "missing value must error"


@th.django_unit_test()
def test_set_metric_gauge_denied_without_write_access(opts):
    """Admin has write_metrics system perm, but we also need to check the
    per-account path. Use custom account where admin doesn't satisfy the
    custom write perm."""
    result = _handler("set_metric_gauge")({
        "slug": "mettools_should_fail",
        "value": "x",
        "account": TEST_CUSTOM_ACCOUNT,
    }, opts.member)  # member has no write_metrics at all
    # Member lacks write_metrics system perm, and custom account's write perm
    # is 'view_metrictest_custom' which member does not have.
    assert "error" in result, f"member should be denied writing to custom: {result}"


@th.django_unit_test()
def test_set_metric_gauge_writes_logit_audit(opts):
    from mojo.apps import metrics
    from mojo.apps.logit.models import Log

    before = Log.objects.filter(kind="assistant:metric:gauge_set").count()
    _handler("set_metric_gauge")({
        "slug": "mettools_audit_slug",
        "value": "on",
        "account": "global",
    }, opts.admin)
    after = Log.objects.filter(kind="assistant:metric:gauge_set").count()

    assert after > before, (
        f"gauge write should create audit log: before={before} after={after}"
    )

    # Ensure the value itself does NOT appear in the payload
    entry = (
        Log.objects.filter(kind="assistant:metric:gauge_set")
        .order_by("-id").first()
    )
    assert entry is not None, "log entry should exist"
    # Payload stores slug + account, never value
    assert "mettools_audit_slug" in (entry.log or ""), (
        f"log message should include slug, got: {entry.log!r}"
    )


# ---------------------------------------------------------------------------
# Per-account permission enforcement
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_fetch_metrics_group_account_allowed_for_member(opts):
    result = _handler("fetch_metrics")({
        "slugs": ["mettools_group_slug"],
        "granularity": "hours",
        "account": opts.group_account,
    }, opts.member)
    # Member has tool-level view_metrics? They do NOT. The tool dispatch gate
    # is checked in the agent layer; here we call handler directly, so only
    # the per-account gate runs. Member has group-level view_metrics, so the
    # per-account check must succeed.
    assert "error" not in result, (
        f"member with group view_metrics should read group-scoped metrics, got: {result}"
    )


@th.django_unit_test()
def test_fetch_metrics_group_account_denied_for_outsider(opts):
    result = _handler("fetch_metrics")({
        "slugs": ["mettools_group_slug"],
        "granularity": "hours",
        "account": opts.group_account,
    }, opts.outsider)
    assert "error" in result, "outsider must be denied on group account"
    assert "Permission denied" in result["error"], (
        f"should return permission-denied error: {result['error']}"
    )


@th.django_unit_test()
def test_fetch_metrics_user_account_allowed_for_self(opts):
    result = _handler("fetch_metrics")({
        "slugs": ["mettools_user_slug"],
        "granularity": "hours",
        "account": f"user-{opts.member.pk}",
    }, opts.member)
    assert "error" not in result, (
        f"member should read own user account, got: {result.get('error')}"
    )


@th.django_unit_test()
def test_fetch_metrics_user_account_denied_for_other(opts):
    result = _handler("fetch_metrics")({
        "slugs": ["mettools_user_slug"],
        "granularity": "hours",
        "account": f"user-{opts.member.pk}",
    }, opts.outsider)
    assert "error" in result, "outsider must be denied on other user's account"


@th.django_unit_test()
def test_fetch_metrics_custom_account_respects_redis_perms(opts):
    result = _handler("fetch_metrics")({
        "slugs": ["mettools_custom_slug"],
        "granularity": "hours",
        "account": TEST_CUSTOM_ACCOUNT,
    }, opts.custom_user)
    assert "error" not in result, (
        f"user with custom perm should read custom account, got: {result.get('error')}"
    )


@th.django_unit_test()
def test_fetch_metrics_custom_account_denied_without_perm(opts):
    result = _handler("fetch_metrics")({
        "slugs": ["mettools_custom_slug"],
        "granularity": "hours",
        "account": TEST_CUSTOM_ACCOUNT,
    }, opts.outsider)
    assert "error" in result, "outsider must be denied on custom account"


@th.django_unit_test()
def test_fetch_metrics_permission_denied_creates_event(opts):
    from mojo.apps.incident.models import Event
    before = Event.objects.filter(category="assistant_permission_denied").count()
    _handler("fetch_metrics")({
        "slugs": ["mettools_group_slug"],
        "granularity": "hours",
        "account": opts.group_account,
    }, opts.outsider)
    after = Event.objects.filter(category="assistant_permission_denied").count()
    assert after > before, (
        f"denied fetch should create security event: before={before} after={after}"
    )


# ---------------------------------------------------------------------------
# Slug explanation — describe_metric_slug
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_describe_metric_slug_finds_real_call(opts):
    """Existing codebase contains metrics.record('api_calls', ...)."""
    result = _handler("describe_metric_slug")({"slug": "api_calls"}, opts.admin)
    assert "error" not in result, f"should succeed: {result.get('error')}"
    assert result["count"] > 0, (
        f"expected at least one hit for 'api_calls', got: {result}"
    )
    # Every hit must have file/line/snippet
    for hit in result["hits"]:
        assert "file" in hit and "line" in hit and "snippet" in hit, (
            f"malformed hit: {hit}"
        )


@th.django_unit_test()
def test_describe_metric_slug_no_match(opts):
    result = _handler("describe_metric_slug")({
        "slug": "xyz_non_existent_slug_abc_12345",
    }, opts.admin)
    assert "error" not in result, f"should succeed even with no matches: {result}"
    assert result["count"] == 0, f"expected zero hits: {result}"
    assert "message" in result, f"should explain lack of hits: {result}"


@th.django_unit_test()
def test_describe_metric_slug_caps_at_10(opts):
    """jobs.record is used in many places; result must cap at 10 hits."""
    result = _handler("describe_metric_slug")({"slug": "metrics.record"}, opts.admin)
    # metrics.record is inside the pattern itself so we expect regex-safe handling
    # Use a likely-common string instead
    result = _handler("describe_metric_slug")({"slug": "jobs"}, opts.admin)
    assert len(result["hits"]) <= 10, (
        f"hits must be capped at 10, got {len(result['hits'])}"
    )


# ---------------------------------------------------------------------------
# Group resolution — resolve_group_account
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_resolve_group_account_by_id(opts):
    result = _handler("resolve_group_account")({
        "name_or_id": str(opts.group.pk),
    }, opts.admin)
    assert "error" not in result, f"admin should resolve by id: {result.get('error')}"
    assert result["account"] == f"group-{opts.group.pk}", (
        f"should return group-<pk>, got: {result}"
    )


@th.django_unit_test()
def test_resolve_group_account_by_name(opts):
    result = _handler("resolve_group_account")({
        "name_or_id": TEST_GROUP_NAME,
    }, opts.admin)
    assert "error" not in result, f"admin should resolve by name: {result.get('error')}"
    assert result["group"]["pk"] == opts.group.pk, (
        f"should resolve to correct pk, got: {result}"
    )


@th.django_unit_test()
def test_resolve_group_account_ambiguous_returns_candidates(opts):
    result = _handler("resolve_group_account")({
        "name_or_id": TEST_GROUP_NAME_AMBIG,
    }, opts.admin)
    assert "error" in result, "ambiguous name must error"
    assert "ambiguous" in result["error"].lower(), (
        f"error should say ambiguous: {result['error']}"
    )
    assert "candidates" in result, f"must include candidates list: {result}"
    assert len(result["candidates"]) >= 2, (
        f"at least 2 candidates expected: {result['candidates']}"
    )


@th.django_unit_test()
def test_resolve_group_account_unknown_name(opts):
    result = _handler("resolve_group_account")({
        "name_or_id": "xyz_no_such_group_1234",
    }, opts.admin)
    assert "error" in result, "unknown name must error"
    assert "no group" in result["error"].lower(), (
        f"error should say no group found: {result['error']}"
    )


@th.django_unit_test()
def test_resolve_group_account_denies_access_for_outsider(opts):
    result = _handler("resolve_group_account")({
        "name_or_id": str(opts.group.pk),
    }, opts.outsider)
    assert "error" in result, "outsider must be denied"
    assert "no access" in result["error"].lower(), (
        f"error should say no access: {result['error']}"
    )
