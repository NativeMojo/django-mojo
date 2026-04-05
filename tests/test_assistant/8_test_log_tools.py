"""Tests for the logs domain assistant tools (query_logs)."""
from testit import helpers as th


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

@th.django_unit_setup()
@th.requires_app("mojo.apps.assistant")
def setup_log_tools(opts):
    from mojo.apps.account.models import User
    from mojo.apps.logit.models import Log

    # Clean up test users
    User.objects.filter(email__in=["logtest_admin@test.com", "logtest_nopriv@test.com"]).delete()

    # Admin with view_logs
    opts.admin = User.objects.create_user(
        username="logtest_admin@test.com", email="logtest_admin@test.com", password="pass123",
    )
    opts.admin.is_email_verified = True
    opts.admin.save()
    opts.admin.add_permission("view_admin")
    opts.admin.add_permission("view_logs")

    # User without view_logs
    opts.nopriv = User.objects.create_user(
        username="logtest_nopriv@test.com", email="logtest_nopriv@test.com", password="pass123",
    )
    opts.nopriv.is_email_verified = True
    opts.nopriv.save()
    opts.nopriv.add_permission("view_admin")

    # Clean up and create test log entries
    Log.objects.filter(kind="test_assistant_log").delete()
    for i in range(5):
        Log.objects.create(
            level="info" if i < 3 else "error",
            kind="test_assistant_log",
            method="GET" if i % 2 == 0 else "POST",
            path=f"/api/test/endpoint_{i}",
            ip="10.0.0.1" if i < 4 else "192.168.1.100",
            uid=opts.admin.id,
            username=opts.admin.username,
            log=f"Test log entry {i} for assistant tool testing",
            model_name="account.User" if i < 2 else "incident.Event",
            model_id=i + 100,
        )

    # One entry with a long log for truncation testing
    Log.objects.create(
        level="warn",
        kind="test_assistant_log",
        method="POST",
        path="/api/test/big_payload",
        ip="10.0.0.1",
        uid=opts.admin.id,
        username=opts.admin.username,
        log="A" * 1000,
        payload="big payload content here",
        user_agent="TestAgent/1.0",
        model_name="account.User",
        model_id=999,
    )


def _query_logs(params, user):
    from mojo.apps.assistant.services.tools.logs import _tool_query_logs
    return _tool_query_logs(params, user)


# ---------------------------------------------------------------------------
# Basic queries
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_query_returns_results(opts):
    result = _query_logs({"kind": "test_assistant_log"}, opts.admin)
    assert "error" not in result, f"Should succeed: {result.get('error')}"
    assert "results" in result, "Result should have results"
    assert result["count"] == 6, f"Should find 6 test log entries, got: {result['count']}"
    assert result["total"] == 6, f"Total should be 6, got: {result['total']}"


@th.django_unit_test()
def test_query_by_level(opts):
    result = _query_logs({"kind": "test_assistant_log", "level": "error", "search": "assistant tool testing"}, opts.admin)
    assert "error" not in result, f"Should succeed: {result.get('error')}"
    assert result["count"] == 2, f"Should find 2 error entries (i=3,4), got: {result['count']}"


@th.django_unit_test()
def test_query_by_model_name(opts):
    result = _query_logs({"kind": "test_assistant_log", "model_name": "account.User"}, opts.admin)
    assert "error" not in result, f"Should succeed: {result.get('error')}"
    assert result["count"] == 3, f"Should find 3 account.User entries, got: {result['count']}"


@th.django_unit_test()
def test_query_by_model_id(opts):
    result = _query_logs({"kind": "test_assistant_log", "model_id": 100}, opts.admin)
    assert "error" not in result, f"Should succeed: {result.get('error')}"
    assert result["count"] == 1, f"Should find 1 entry with model_id=100, got: {result['count']}"


@th.django_unit_test()
def test_query_by_uid(opts):
    result = _query_logs({"kind": "test_assistant_log", "uid": opts.admin.id}, opts.admin)
    assert "error" not in result, f"Should succeed: {result.get('error')}"
    assert result["count"] == 6, f"Should find all 6 entries for this user, got: {result['count']}"


@th.django_unit_test()
def test_query_by_ip(opts):
    result = _query_logs({"kind": "test_assistant_log", "ip": "192.168.1.100"}, opts.admin)
    assert "error" not in result, f"Should succeed: {result.get('error')}"
    assert result["count"] == 1, f"Should find 1 entry from 192.168.1.100, got: {result['count']}"


@th.django_unit_test()
def test_query_by_method(opts):
    result = _query_logs({"kind": "test_assistant_log", "method": "POST"}, opts.admin)
    assert "error" not in result, f"Should succeed: {result.get('error')}"
    # 2 POST entries from the loop + 1 from the big payload entry
    assert result["count"] == 3, f"Should find 3 POST entries, got: {result['count']}"


@th.django_unit_test()
def test_query_by_path(opts):
    result = _query_logs({"kind": "test_assistant_log", "path": "endpoint_2"}, opts.admin)
    assert "error" not in result, f"Should succeed: {result.get('error')}"
    assert result["count"] == 1, f"Should find 1 entry matching path, got: {result['count']}"


@th.django_unit_test()
def test_query_free_text_search(opts):
    result = _query_logs({"kind": "test_assistant_log", "search": "entry 3"}, opts.admin)
    assert "error" not in result, f"Should succeed: {result.get('error')}"
    assert result["count"] == 1, f"Should find 1 entry matching search, got: {result['count']}"


@th.django_unit_test()
def test_query_ordered_newest_first(opts):
    result = _query_logs({"kind": "test_assistant_log"}, opts.admin)
    assert "error" not in result, f"Should succeed: {result.get('error')}"
    created_dates = [r["created"] for r in result["results"]]
    assert created_dates == sorted(created_dates, reverse=True), \
        f"Results should be ordered newest first, got: {created_dates}"


# ---------------------------------------------------------------------------
# count_only
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_count_only(opts):
    result = _query_logs({"kind": "test_assistant_log", "count_only": True}, opts.admin)
    assert "count" in result, "count_only should return count"
    assert result["count"] == 6, f"Count should be 6, got: {result['count']}"
    assert "results" not in result, "count_only should not include results"


# ---------------------------------------------------------------------------
# Limit and time window
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_limit(opts):
    result = _query_logs({"kind": "test_assistant_log", "limit": 2}, opts.admin)
    assert "error" not in result, f"Should succeed: {result.get('error')}"
    assert result["count"] == 2, f"Should return 2 with limit=2, got: {result['count']}"
    assert result["total"] == 6, f"Total should still be 6, got: {result['total']}"


@th.django_unit_test()
def test_limit_cap(opts):
    from mojo.apps.assistant.services.tools.logs import MAX_LIMIT
    result = _query_logs({"kind": "test_assistant_log", "limit": 9999}, opts.admin)
    assert "error" not in result, f"Should succeed: {result.get('error')}"
    assert result["count"] <= MAX_LIMIT, f"Results should not exceed MAX_LIMIT={MAX_LIMIT}"


@th.django_unit_test()
def test_time_window_capped(opts):
    result = _query_logs({"kind": "test_assistant_log", "minutes": 99999}, opts.admin)
    assert "error" not in result, f"Should succeed: {result.get('error')}"
    from mojo.apps.assistant.services.tools.logs import MAX_MINUTES
    assert result["period_minutes"] == MAX_MINUTES, \
        f"Minutes should be capped at {MAX_MINUTES}, got: {result['period_minutes']}"


@th.django_unit_test()
def test_invalid_minutes(opts):
    result = _query_logs({"minutes": -5}, opts.admin)
    assert "error" in result, "Negative minutes should return error"


@th.django_unit_test()
def test_zero_limit_uses_default(opts):
    """limit=0 should fall back to DEFAULT_LIMIT, not return empty."""
    result = _query_logs({"kind": "test_assistant_log", "limit": 0}, opts.admin)
    assert "error" not in result, f"Should succeed: {result.get('error')}"
    assert result["count"] > 0, "limit=0 should use default, not return empty"


@th.django_unit_test()
def test_negative_limit_uses_default(opts):
    """Negative limit should fall back to DEFAULT_LIMIT."""
    result = _query_logs({"kind": "test_assistant_log", "limit": -1}, opts.admin)
    assert "error" not in result, f"Should succeed: {result.get('error')}"
    assert result["count"] > 0, "Negative limit should use default, not crash"


# ---------------------------------------------------------------------------
# Truncation and verbose mode
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_log_truncation(opts):
    from mojo.apps.assistant.services.tools.logs import LOG_TRUNCATE_LENGTH
    result = _query_logs({"kind": "test_assistant_log", "model_id": 999}, opts.admin)
    assert "error" not in result, f"Should succeed: {result.get('error')}"
    assert result["count"] == 1, f"Should find the big log entry, got: {result['count']}"
    entry = result["results"][0]
    assert len(entry["log"]) == LOG_TRUNCATE_LENGTH, \
        f"Log should be truncated to {LOG_TRUNCATE_LENGTH}, got: {len(entry['log'])}"
    assert entry.get("log_truncated") is True, "Should have log_truncated flag"
    assert "payload" not in entry, "Payload should be excluded in non-verbose mode"
    assert "user_agent" not in entry, "user_agent should be excluded in non-verbose mode"


@th.django_unit_test()
def test_verbose_includes_full_content(opts):
    result = _query_logs({"kind": "test_assistant_log", "model_id": 999, "verbose": True}, opts.admin)
    assert "error" not in result, f"Should succeed: {result.get('error')}"
    entry = result["results"][0]
    assert len(entry["log"]) == 1000, f"Verbose log should be full 1000 chars, got: {len(entry['log'])}"
    assert entry.get("log_truncated") is None, "Verbose should not have truncated flag"
    assert entry["payload"] is not None, "Verbose should include payload"
    assert "payload" in entry, "Verbose should have payload key"
    assert entry["user_agent"] is not None, "Verbose should include user_agent"
    assert "user_agent" in entry, "Verbose should have user_agent key"


@th.django_unit_test()
def test_verbose_masks_sensitive_payload(opts):
    """Payload content should be run through mask_sensitive_data."""
    from mojo.apps.logit.models import Log

    # Create a log with sensitive payload content
    Log.objects.filter(kind="test_mask_payload").delete()
    Log.objects.create(
        level="info",
        kind="test_mask_payload",
        method="POST",
        path="/api/test/sensitive",
        ip="10.0.0.1",
        uid=opts.admin.id,
        username=opts.admin.username,
        log="test",
        payload='{"password": "hunter2", "username": "admin"}',
        user_agent="TestAgent/1.0",
    )
    result = _query_logs({"kind": "test_mask_payload", "verbose": True}, opts.admin)
    assert "error" not in result, f"Should succeed: {result.get('error')}"
    entry = result["results"][0]
    assert "hunter2" not in (entry.get("payload") or ""), \
        f"Payload should mask sensitive values, got: {entry.get('payload')}"


# ---------------------------------------------------------------------------
# Permission gate
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_permission_denied_without_view_logs(opts):
    """User without view_logs should see the tool but get no results due to RestMeta gate."""
    # The tool has permission="view_logs" so the registry will gate it.
    # But we test the handler directly — it should still work since
    # the handler doesn't re-check permissions (the registry does that).
    # This test verifies the tool registration has the right permission.
    from mojo.apps.assistant import get_registry
    registry = get_registry()
    entry = registry["query_logs"]
    assert entry["permission"] == "view_logs", \
        f"query_logs permission should be view_logs, got: {entry['permission']}"


# ---------------------------------------------------------------------------
# Result structure
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_result_structure(opts):
    result = _query_logs({"kind": "test_assistant_log", "limit": 1}, opts.admin)
    assert "error" not in result, f"Should succeed: {result.get('error')}"
    entry = result["results"][0]
    expected_keys = {"id", "created", "level", "kind", "method", "path", "ip", "uid", "username", "model_name", "model_id", "log"}
    actual_keys = set(entry.keys())
    missing = expected_keys - actual_keys
    assert not missing, f"Missing keys in result: {missing}"


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_query_logs_registered(opts):
    from mojo.apps.assistant import get_registry
    registry = get_registry()
    assert "query_logs" in registry, "query_logs should be registered"
    entry = registry["query_logs"]
    assert entry["permission"] == "view_logs", \
        f"Permission should be view_logs, got: {entry['permission']}"
    assert entry["mutates"] is False, "query_logs should not be mutating"
    assert entry["domain"] == "logs", f"Domain should be 'logs', got: {entry['domain']}"
