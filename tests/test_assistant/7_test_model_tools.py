"""Tests for the models domain assistant tools (describe_model, query_model)."""
from testit import helpers as th


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

@th.django_unit_setup()
@th.requires_app("mojo.apps.assistant")
def setup_model_tools(opts):
    from mojo.apps.account.models import User
    from mojo.apps.incident.models import Event

    # Clean up test users
    User.objects.filter(email__in=["modeltest_admin@test.com", "modeltest_nopriv@test.com"]).delete()

    # Admin user with security + view_admin perms
    opts.admin = User.objects.create_user(
        username="modeltest_admin@test.com", email="modeltest_admin@test.com", password="pass123",
    )
    opts.admin.is_email_verified = True
    opts.admin.save()
    opts.admin.add_permission("view_admin")
    opts.admin.add_permission("view_security")

    # Unprivileged user (no security perms)
    opts.nopriv = User.objects.create_user(
        username="modeltest_nopriv@test.com", email="modeltest_nopriv@test.com", password="pass123",
    )
    opts.nopriv.is_email_verified = True
    opts.nopriv.save()
    opts.nopriv.add_permission("view_admin")

    # Create some test events
    Event.objects.filter(title__startswith="modeltest_").delete()
    for i in range(5):
        Event.objects.create(
            title=f"modeltest_event_{i}",
            details=f"Test event {i} for model tools",
            category="test",
            level=i + 1,
            scope="global",
        )


def _describe(params, user):
    from mojo.apps.assistant.services.tools.models import _tool_describe_model
    return _tool_describe_model(params, user)


def _query(params, user):
    from mojo.apps.assistant.services.tools.models import _tool_query_model
    return _tool_query_model(params, user)


# ---------------------------------------------------------------------------
# describe_model — basic functionality
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_describe_returns_fields(opts):
    result = _describe({"app_name": "incident", "model_name": "Event"}, opts.admin)
    assert "error" not in result, f"Should succeed: {result.get('error')}"
    assert "fields" in result, "Result should have fields"
    assert len(result["fields"]) > 0, "Should have at least one field"
    field_names = [f["name"] for f in result["fields"]]
    assert "title" in field_names, f"Should include 'title' field, got: {field_names}"
    assert "category" in field_names, f"Should include 'category' field, got: {field_names}"


@th.django_unit_test()
def test_describe_returns_graphs(opts):
    result = _describe({"app_name": "incident", "model_name": "Event"}, opts.admin)
    assert "error" not in result, f"Should succeed: {result.get('error')}"
    assert "graphs" in result, "Result should have graphs"
    assert "default" in result["graphs"], f"Should have 'default' graph, got: {list(result['graphs'].keys())}"


@th.django_unit_test()
def test_describe_returns_permissions(opts):
    result = _describe({"app_name": "incident", "model_name": "Event"}, opts.admin)
    assert "error" not in result, f"Should succeed: {result.get('error')}"
    assert "permissions" in result, "Result should have permissions"
    assert "view" in result["permissions"], "Should have view permissions"
    assert "save" in result["permissions"], "Should have save permissions"
    assert "view_security" in result["permissions"]["view"], \
        f"Event VIEW_PERMS should include view_security, got: {result['permissions']['view']}"


@th.django_unit_test()
def test_describe_returns_search_fields(opts):
    result = _describe({"app_name": "incident", "model_name": "Event"}, opts.admin)
    assert "error" not in result, f"Should succeed: {result.get('error')}"
    assert "search_fields" in result, "Result should have search_fields"
    assert "details" in result["search_fields"], \
        f"Event SEARCH_FIELDS should include 'details', got: {result['search_fields']}"


@th.django_unit_test()
def test_describe_excludes_sensitive_fields(opts):
    # User model has password field — it should be excluded
    result = _describe({"app_name": "account", "model_name": "User"}, opts.admin)
    assert "error" not in result, f"Should succeed: {result.get('error')}"
    field_names = [f["name"] for f in result["fields"]]
    assert "password" not in field_names, f"Sensitive field 'password' should be excluded, got: {field_names}"


# ---------------------------------------------------------------------------
# describe_model — error cases
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_describe_missing_params(opts):
    result = _describe({}, opts.admin)
    assert "error" in result, "Should error when no params provided"


@th.django_unit_test()
def test_describe_bad_model(opts):
    result = _describe({"app_name": "account", "model_name": "NonExistent"}, opts.admin)
    assert "error" in result, "Should error for nonexistent model"
    assert "not found" in result["error"], f"Error should say not found: {result['error']}"


@th.django_unit_test()
def test_describe_bad_app(opts):
    result = _describe({"app_name": "nonexistent_app", "model_name": "Foo"}, opts.admin)
    assert "error" in result, "Should error for nonexistent app"


@th.django_unit_test()
def test_describe_no_rest_model(opts):
    result = _describe({"app_name": "assistant", "model_name": "Message"}, opts.admin)
    assert "error" in result, "Should error for NO_REST model"
    assert "not available" in result["error"], f"Error should mention not available: {result['error']}"


# ---------------------------------------------------------------------------
# query_model — basic functionality
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_query_returns_results(opts):
    result = _query({"app_name": "incident", "model_name": "Event", "filters": {"title__startswith": "modeltest_"}}, opts.admin)
    assert "error" not in result, f"Should succeed: {result.get('error')}"
    assert "results" in result, "Result should have results"
    assert result["count"] == 5, f"Should find 5 test events, got: {result['count']}"
    assert result["total"] == 5, f"Total should be 5, got: {result['total']}"


@th.django_unit_test()
def test_query_with_filters(opts):
    result = _query({
        "app_name": "incident", "model_name": "Event",
        "filters": {"title__startswith": "modeltest_", "level__gte": 3},
    }, opts.admin)
    assert "error" not in result, f"Should succeed: {result.get('error')}"
    assert result["count"] == 3, f"Should find 3 events with level >= 3, got: {result['count']}"


@th.django_unit_test()
def test_query_with_ordering(opts):
    result = _query({
        "app_name": "incident", "model_name": "Event",
        "filters": {"title__startswith": "modeltest_"},
        "ordering": "level",
    }, opts.admin)
    assert "error" not in result, f"Should succeed: {result.get('error')}"
    levels = [r.get("level") for r in result["results"]]
    assert levels == sorted(levels), f"Results should be ordered by level ascending, got: {levels}"


@th.django_unit_test()
def test_query_with_descending_ordering(opts):
    result = _query({
        "app_name": "incident", "model_name": "Event",
        "filters": {"title__startswith": "modeltest_"},
        "ordering": "-level",
    }, opts.admin)
    assert "error" not in result, f"Should succeed: {result.get('error')}"
    levels = [r.get("level") for r in result["results"]]
    assert levels == sorted(levels, reverse=True), f"Results should be ordered by level descending, got: {levels}"


@th.django_unit_test()
def test_query_count_only(opts):
    result = _query({
        "app_name": "incident", "model_name": "Event",
        "filters": {"title__startswith": "modeltest_"},
        "count_only": True,
    }, opts.admin)
    assert "error" not in result, f"Should succeed: {result.get('error')}"
    assert "count" in result, "Result should have count"
    assert result["count"] == 5, f"Count should be 5, got: {result['count']}"
    assert "results" not in result, "count_only should not include results"


@th.django_unit_test()
def test_query_csv_format(opts):
    result = _query({
        "app_name": "incident", "model_name": "Event",
        "filters": {"title__startswith": "modeltest_"},
        "format": "csv",
    }, opts.admin)
    assert "error" not in result, f"Should succeed: {result.get('error')}"
    assert result["format"] == "csv", f"Format should be csv, got: {result.get('format')}"
    assert "content" in result, "CSV result should have content"
    assert result["count"] == 5, f"Count should be 5, got: {result['count']}"


@th.django_unit_test()
def test_query_limit(opts):
    result = _query({
        "app_name": "incident", "model_name": "Event",
        "filters": {"title__startswith": "modeltest_"},
        "limit": 2,
    }, opts.admin)
    assert "error" not in result, f"Should succeed: {result.get('error')}"
    assert result["count"] == 2, f"Should return 2 results with limit=2, got: {result['count']}"
    assert result["total"] == 5, f"Total should still be 5, got: {result['total']}"


@th.django_unit_test()
def test_query_limit_cap(opts):
    from mojo.apps.assistant.services.tools.models import MAX_LIMIT
    result = _query({
        "app_name": "incident", "model_name": "Event",
        "filters": {"title__startswith": "modeltest_"},
        "limit": 9999,
    }, opts.admin)
    assert "error" not in result, f"Should succeed: {result.get('error')}"
    # The limit should be capped at MAX_LIMIT, but we only have 5 events
    # Just verify no error — the cap is enforced internally
    assert result["count"] <= MAX_LIMIT, f"Results should not exceed MAX_LIMIT={MAX_LIMIT}"


# ---------------------------------------------------------------------------
# query_model — permission denied
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_query_permission_denied(opts):
    """User without view_security cannot query Event model."""
    result = _query({
        "app_name": "incident", "model_name": "Event",
    }, opts.nopriv)
    assert "error" in result, "Should be denied without view_security permission"
    assert "Permission denied" in result["error"], f"Error should mention permission denied: {result['error']}"


@th.django_unit_test()
def test_query_permission_denied_creates_event(opts):
    """Permission denied should create a security event."""
    from mojo.apps.incident.models import Event

    before_count = Event.objects.filter(category="assistant_permission_denied").count()
    _query({"app_name": "incident", "model_name": "Event"}, opts.nopriv)
    after_count = Event.objects.filter(category="assistant_permission_denied").count()
    assert after_count > before_count, \
        f"Should create assistant_permission_denied event, before={before_count} after={after_count}"


# ---------------------------------------------------------------------------
# query_model — sensitive field rejection
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_query_rejects_sensitive_filter(opts):
    """Sensitive field names should be rejected even if the model is accessible."""
    result = _query({
        "app_name": "incident", "model_name": "Event",
        "filters": {"password__icontains": "admin"},
    }, opts.admin)
    assert "error" in result, "Should reject sensitive field filter"
    assert "not allowed" in result["error"], f"Error should mention not allowed: {result['error']}"


@th.django_unit_test()
def test_query_sensitive_field_creates_event(opts):
    """Sensitive field probe should create a security event."""
    from mojo.apps.incident.models import Event

    before_count = Event.objects.filter(category="assistant_sensitive_field").count()
    _query({
        "app_name": "incident", "model_name": "Event",
        "filters": {"secret__icontains": "x"},
    }, opts.admin)
    after_count = Event.objects.filter(category="assistant_sensitive_field").count()
    assert after_count > before_count, \
        f"Should create assistant_sensitive_field event, before={before_count} after={after_count}"


# ---------------------------------------------------------------------------
# query_model — validation errors
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_query_bad_model(opts):
    result = _query({"app_name": "account", "model_name": "FakeModel"}, opts.admin)
    assert "error" in result, "Should error for nonexistent model"


@th.django_unit_test()
def test_query_unknown_filter_field(opts):
    result = _query({
        "app_name": "incident", "model_name": "Event",
        "filters": {"nonexistent_field": "value"},
    }, opts.admin)
    assert "error" in result, "Should error for unknown filter field"
    assert "Unknown field" in result["error"], f"Error should mention unknown field: {result['error']}"


@th.django_unit_test()
def test_query_bad_ordering_field(opts):
    result = _query({
        "app_name": "incident", "model_name": "Event",
        "ordering": "-nonexistent",
    }, opts.admin)
    assert "error" in result, "Should error for unknown ordering field"
    assert "Unknown ordering field" in result["error"], f"Error should mention ordering: {result['error']}"


@th.django_unit_test()
def test_query_no_rest_model(opts):
    result = _query({"app_name": "assistant", "model_name": "Message"}, opts.admin)
    assert "error" in result, "Should error for NO_REST model"


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_is_sensitive_field(opts):
    from mojo.apps.assistant.services.tools.models import _is_sensitive_field
    assert _is_sensitive_field("password") is True, "password should be sensitive"
    assert _is_sensitive_field("auth_key") is True, "auth_key should be sensitive"
    assert _is_sensitive_field("onetime_code") is True, "onetime_code should be sensitive"
    assert _is_sensitive_field("secret_token") is True, "secret_token should be sensitive"
    assert _is_sensitive_field("token_secret") is True, "token_secret should be sensitive"
    assert _is_sensitive_field("email") is False, "email should not be sensitive"
    assert _is_sensitive_field("title") is False, "title should not be sensitive"


@th.django_unit_test()
def test_resolve_model_valid(opts):
    from mojo.apps.assistant.services.tools.models import _resolve_model
    model, err = _resolve_model("incident", "Event")
    assert err is None, f"Should resolve valid model: {err}"
    assert model is not None, "Model should not be None"


@th.django_unit_test()
def test_resolve_model_invalid(opts):
    from mojo.apps.assistant.services.tools.models import _resolve_model
    model, err = _resolve_model("fake_app", "FakeModel")
    assert model is None, "Should return None for invalid model"
    assert "error" in err, "Should return error dict"


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_describe_model_registered(opts):
    from mojo.apps.assistant import get_registry
    registry = get_registry()
    assert "describe_model" in registry, "describe_model should be registered"
    entry = registry["describe_model"]
    assert entry["permission"] == "view_admin", \
        f"Permission should be view_admin, got: {entry['permission']}"
    assert entry["mutates"] is False, "describe_model should not be mutating"
    assert entry["domain"] == "models", f"Domain should be 'models', got: {entry['domain']}"


@th.django_unit_test()
def test_query_model_registered(opts):
    from mojo.apps.assistant import get_registry
    registry = get_registry()
    assert "query_model" in registry, "query_model should be registered"
    entry = registry["query_model"]
    assert entry["permission"] == "view_admin", \
        f"Permission should be view_admin, got: {entry['permission']}"
    assert entry["mutates"] is False, "query_model should not be mutating"
    assert entry["domain"] == "models", f"Domain should be 'models', got: {entry['domain']}"
