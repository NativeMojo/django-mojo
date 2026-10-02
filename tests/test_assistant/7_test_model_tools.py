"""Tests for the models domain assistant tools (describe_model, query_model)."""
import contextlib
import json

from testit import helpers as th

SKILL_SECRET = "MODELTEST-SECRET-MARKER"
SKILL_DEFAULT_KEYS = [
    "id", "tier", "name", "description", "auto_execute", "is_active",
    "created", "modified", "user",
]


@contextlib.contextmanager
def _skill_rest_meta(graphs=None, **attrs):
    """Temporarily ADD graphs / RestMeta attributes to assistant.Skill.

    Additive only: `default` and `detail` are left exactly as shipped, so the
    only code that can see a difference is the assistant's own graph selection,
    which runs in this package's thread. These tests call the tool handlers
    in-process; the change never reaches the test server.
    """
    from mojo.apps.assistant.models import Skill

    original = Skill.RestMeta
    merged = dict(original.GRAPHS)
    merged.update(graphs or {})
    attrs["GRAPHS"] = merged
    Skill.RestMeta = type("RestMeta", (original,), attrs)
    try:
        yield Skill
    finally:
        Skill.RestMeta = original


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

    # Skills carrying a value only the wide `detail` graph (and any graph a
    # test adds) exposes — never `default`.
    from mojo.apps.assistant.models import Skill
    Skill.objects.filter(name__startswith="modeltest_skill_").delete()
    for i in range(2):
        Skill.objects.create(
            user=opts.admin, tier="user", name=f"modeltest_skill_{i}",
            description="model tools graph test",
            steps=[{"note": SKILL_SECRET}],
            metadata={"marker": SKILL_SECRET},
        )
    opts.skill_filters = {"name__startswith": "modeltest_skill_"}


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


@th.tier("bug")
@th.django_unit_test()
def test_describe_returns_serialization(opts):
    """describe_model names the ONE graph the assistant reads and its keys."""
    result = _describe({"app_name": "assistant", "model_name": "Skill"}, opts.admin)
    assert "error" not in result, f"Should succeed: {result.get('error')}"
    assert "graphs" not in result, \
        f"describe_model must not advertise graph names, got keys: {sorted(result)}"
    assert result.get("serialization") == {"graph": "default", "fields": SKILL_DEFAULT_KEYS}, \
        f"serialization should be the default graph's public keys, got: {result.get('serialization')}"
    assert "detail" not in json.dumps(result["serialization"]), \
        "the wider 'detail' graph must not be named"


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
def test_query_no_longer_accepts_csv_format(opts):
    """CSV format was removed from query_model — exports use export_data tool instead."""
    result = _query({
        "app_name": "incident", "model_name": "Event",
        "filters": {"title__startswith": "modeltest_"},
        "format": "csv",
    }, opts.admin)
    # format param is now ignored — should return JSON results
    assert "error" not in result, f"Should succeed (format param ignored): {result.get('error')}"
    assert "results" in result, "Should return JSON results, not CSV content"
    assert "content" not in result, "Should NOT have CSV content"


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

@th.tier("core")
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
    assert _is_sensitive_field("access_token") is True, "access_token should be sensitive"
    assert _is_sensitive_field("refresh_token") is True, "refresh_token should be sensitive"
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


# ---------------------------------------------------------------------------
# Security hardening — relational traversal, ordering, error sanitization
# ---------------------------------------------------------------------------

@th.tier("core")
@th.django_unit_test()
def test_query_blocks_relational_sensitive_traversal(opts):
    """user__password__icontains should be blocked even though 'user' is a valid field."""
    result = _query({
        "app_name": "incident", "model_name": "Event",
        "filters": {"uid__password__icontains": "admin"},
    }, opts.admin)
    assert "error" in result, "Relational traversal to sensitive field should be blocked"
    assert "not allowed" in result["error"], f"Error should mention not allowed: {result['error']}"


@th.tier("core")
@th.django_unit_test()
def test_query_blocks_deep_traversal_to_token(opts):
    """Multi-hop traversal to token fields should be blocked."""
    result = _query({
        "app_name": "incident", "model_name": "Event",
        "filters": {"uid__access_token__startswith": "x"},
    }, opts.admin)
    assert "error" in result, "Deep traversal to token field should be blocked"
    assert "not allowed" in result["error"], f"Error should mention not allowed: {result['error']}"


@th.tier("core")
@th.django_unit_test()
def test_query_blocks_relational_secret_traversal(opts):
    """Traversal containing 'secret' in any segment should be blocked."""
    result = _query({
        "app_name": "incident", "model_name": "Event",
        "filters": {"uid__secret_key__exact": "x"},
    }, opts.admin)
    assert "error" in result, "Secret traversal should be blocked"
    assert "not allowed" in result["error"], f"Error should mention not allowed: {result['error']}"


@th.tier("core")
@th.django_unit_test()
def test_query_ordering_rejects_sensitive_field(opts):
    """Ordering by a sensitive field name should be rejected."""
    result = _query({
        "app_name": "incident", "model_name": "Event",
        "ordering": "-password",
    }, opts.admin)
    assert "error" in result, "Ordering by sensitive field should be rejected"
    assert "not allowed" in result["error"], f"Error should mention not allowed: {result['error']}"


@th.tier("core")
@th.django_unit_test()
def test_query_ordering_rejects_relational_traversal(opts):
    """Ordering with __ traversal should be rejected."""
    result = _query({
        "app_name": "incident", "model_name": "Event",
        "ordering": "uid__date_joined",
    }, opts.admin)
    assert "error" in result, "Relational ordering should be rejected"
    assert "not supported" in result["error"], f"Error should mention not supported: {result['error']}"


@th.django_unit_test()
def test_query_error_no_internal_leak(opts):
    """ORM errors should not leak internal details."""
    result = _query({
        "app_name": "incident", "model_name": "Event",
        "filters": {"level": "not_a_number"},
    }, opts.admin)
    assert "error" in result, "Bad filter value should return error"
    assert "Invalid filter parameters" in result["error"], \
        f"Error should be generic, not leak internals: {result['error']}"


# ---------------------------------------------------------------------------
# Serialization graph — chosen by the server, never by the caller
# ---------------------------------------------------------------------------

def _skill_query(opts, user=None, **extra):
    params = {"app_name": "assistant", "model_name": "Skill", "filters": opts.skill_filters}
    params.update(extra)
    return _query(params, user or opts.admin)


@th.tier("bug")
@th.django_unit_test()
def test_model_tool_schemas_do_not_offer_graph(opts):
    """No model tool advertises a `graph` (or override) input."""
    from mojo.apps.assistant import get_registry
    registry = get_registry()
    for name in ("describe_model", "query_model", "aggregate_model", "export_data"):
        props = registry[name]["definition"]["input_schema"]["properties"]
        assert "graph" not in props, f"{name} must not advertise a 'graph' input, got: {sorted(props)}"
        assert "graph_override" not in props, f"{name} must not advertise 'graph_override'"
        assert "available graphs" not in registry[name]["definition"]["description"], \
            f"{name} description must not invite choosing among graphs"


@th.tier("bug")
@th.django_unit_test()
def test_describe_refuses_caller_graph(opts):
    result = _describe({"app_name": "assistant", "model_name": "Skill", "graph": "default"}, opts.admin)
    assert "error" in result, f"describe_model must refuse a 'graph' key, got: {result}"
    assert "'graph' parameter is not supported" in result["error"], f"Error should name the parameter: {result['error']}"


@th.tier("bug")
@th.django_unit_test()
def test_query_refuses_caller_graph(opts):
    """A `graph` key is refused outright — even the name the server would pick."""
    for name in ("default", "detail", "ai", "", None):
        result = _skill_query(opts, graph=name)
        assert "error" in result, f"query_model must refuse graph={name!r}, got keys: {sorted(result)}"
        assert "'graph' parameter is not supported" in result["error"], \
            f"Error should name the parameter for graph={name!r}: {result['error']}"
        assert "results" not in result, f"No rows may be returned with graph={name!r}"


@th.tier("bug")
@th.django_unit_test()
def test_query_uses_default_without_ai(opts):
    """With no `ai` graph the rows come through `default` — not the wider `detail`."""
    result = _skill_query(opts)
    assert "error" not in result, f"Should succeed: {result.get('error')}"
    assert result["count"] == 2, f"Expected the 2 test skills, got: {result['count']}"
    for row in result["results"]:
        assert list(row) == SKILL_DEFAULT_KEYS, f"Row should have exactly the default keys, got: {list(row)}"
    assert SKILL_SECRET not in json.dumps(result), "A detail-only value reached the default serialization"


@th.tier("bug")
@th.django_unit_test()
def test_query_uses_ai_when_present(opts):
    """A model that declares `ai` is read through it, and describe says so."""
    with _skill_rest_meta(graphs={"ai": {"fields": ["id", "name"]}}):
        result = _skill_query(opts)
        described = _describe({"app_name": "assistant", "model_name": "Skill"}, opts.admin)
    assert "error" not in result, f"Should succeed: {result.get('error')}"
    for row in result["results"]:
        assert list(row) == ["id", "name"], f"Row should have exactly the ai keys, got: {list(row)}"
    assert described.get("serialization") == {"graph": "ai", "fields": ["id", "name"]}, \
        f"describe_model should report the ai graph, got: {described.get('serialization')}"


@th.tier("bug")
@th.django_unit_test()
def test_secret_graph_unreachable_without_graph_permissions(opts):
    """A secret-bearing third graph with NO GRAPH_PERMISSIONS entry cannot be reached."""
    from mojo.apps.assistant.models import Skill

    leak = {"fields": ["id", "name", "steps", "metadata"]}
    with _skill_rest_meta(graphs={"leak": leak}):
        assert not getattr(Skill.RestMeta, "GRAPH_PERMISSIONS", None), \
            "precondition: the model declares no GRAPH_PERMISSIONS"
        assert SKILL_SECRET in json.dumps(Skill.objects.filter(**opts.skill_filters).first().to_dict("leak")), \
            "precondition: the third graph really carries the secret value"
        asked = _skill_query(opts, graph="leak")
        plain = _skill_query(opts)
        described = _describe({"app_name": "assistant", "model_name": "Skill"}, opts.admin)
    assert "error" in asked and "results" not in asked, f"Naming the graph must be refused, got: {asked}"
    assert "error" not in plain, f"A plain query should succeed: {plain.get('error')}"
    assert SKILL_SECRET not in json.dumps(plain), "The secret graph's value reached query_model"
    assert "leak" not in json.dumps(described), f"describe_model must not name the third graph: {described}"


@th.tier("bug")
@th.django_unit_test()
def test_malformed_ai_graph_is_refused_not_bypassed(opts):
    """A present-but-malformed `ai` is an error — never a fall back to `default`."""
    for bad in (None, "id", ["id", "name"], 7):
        with _skill_rest_meta(graphs={"ai": bad}):
            result = _skill_query(opts)
            described = _describe({"app_name": "assistant", "model_name": "Skill"}, opts.admin)
            counted = _skill_query(opts, count_only=True)
        assert "error" in result and "results" not in result, f"ai={bad!r} must be refused, got: {result}"
        assert "malformed 'ai' graph" in result["error"], f"Error should say why for ai={bad!r}: {result['error']}"
        assert "error" in described, f"describe_model must refuse ai={bad!r} too, got: {described}"
        assert counted.get("count") == 2, f"count_only does not serialize and must still work, got: {counted}"


@th.tier("bug")
@th.django_unit_test()
def test_empty_ai_graph_means_all_fields(opts):
    """An explicit `{}` is valid: the framework's all-fields graph."""
    from mojo.apps.assistant.models import Skill

    with _skill_rest_meta(graphs={"ai": {}}):
        result = _skill_query(opts)
        described = _describe({"app_name": "assistant", "model_name": "Skill"}, opts.admin)
    assert "error" not in result, f"An explicit empty graph must serialize: {result.get('error')}"
    all_fields = [f.name for f in Skill._meta.fields]
    for row in result["results"]:
        assert list(row) == all_fields, f"{{}} should mean every model field, got: {list(row)}"
    assert described["serialization"] == {"graph": "ai", "fields": all_fields}, \
        f"describe_model should list every field for {{}}, got: {described['serialization']}"


@th.tier("bug")
@th.django_unit_test()
def test_graph_selection_fails_closed_on_missing_or_malformed_default(opts):
    """No `ai` and no usable `default` is an error, on throwaway classes (no shared state)."""
    from mojo.apps.assistant.services.model_serialization import (
        AssistantGraphError, select_graph)

    def probe(graphs):
        return type("Probe", (), {"RestMeta": type("RestMeta", (), {"GRAPHS": graphs})})

    for graphs in (None, {}, {"detail": {"fields": ["id"]}}, {"default": None},
                   {"default": ["id"]}, {"default": "id"}, ["default"]):
        try:
            name = select_graph(probe(graphs))
        except AssistantGraphError:
            continue
        assert False, f"GRAPHS={graphs!r} must be refused, selected {name!r}"

    no_meta = type("Bare", (), {})
    try:
        select_graph(no_meta)
        assert False, "a class with no RestMeta must be refused"
    except AssistantGraphError:
        pass

    assert select_graph(probe({"default": {}})) == "default", "an explicit {} default is valid"
    assert select_graph(probe({"default": {}, "ai": {}})) == "ai", "ai wins when present"
    assert select_graph(probe({"default": {"fields": ["id"]}, "detail": {}})) == "default", \
        "a wider graph is never selected"


@th.tier("bug")
@th.django_unit_test()
def test_graph_permissions_enforced_on_selected_graph(opts):
    """GRAPH_PERMISSIONS on the server-selected name is still checked."""
    from mojo.apps.incident.models import Event

    gate = {"ai": ["modeltest_ai_reader"]}
    before = Event.objects.filter(category="assistant_permission_denied").count()
    with _skill_rest_meta(graphs={"ai": {"fields": ["id", "name"]}}, GRAPH_PERMISSIONS=gate):
        denied = _skill_query(opts)
        opts.admin.add_permission("modeltest_ai_reader")
        try:
            allowed = _skill_query(opts)
        finally:
            opts.admin.remove_permission("modeltest_ai_reader")
    assert "error" in denied and "results" not in denied, f"The gated graph must be refused, got: {denied}"
    assert "modeltest_ai_reader" in denied["error"], f"Error should name the permission: {denied['error']}"
    after = Event.objects.filter(category="assistant_permission_denied").count()
    assert after > before, "A refused graph should raise an assistant_permission_denied event"
    assert "error" not in allowed, f"With the permission the query should succeed: {allowed.get('error')}"
    assert [list(r) for r in allowed["results"]] == [["id", "name"]] * 2, \
        f"Rows should come through the gated ai graph, got: {allowed['results']}"


@th.tier("bug")
@th.django_unit_test()
def test_selected_default_name_reaches_the_permission_choke_point(opts):
    """With no `ai`, the name handed to rest_resolve_graph_or_raise is `default`."""
    from mojo.apps.assistant.services import model_serialization

    calls = []

    class Probe:
        class RestMeta:
            GRAPHS = {"default": {"fields": ["id"]}, "detail": {"fields": ["id", "secret"]}}

        @classmethod
        def rest_resolve_graph_or_raise(cls, request, requested, instance=None):
            calls.append((request, requested, instance))
            return requested

    request = type("Req", (), {"group": "tenant-a"})()
    name = model_serialization.resolve_graph(Probe, request=request)
    assert name == "default", f"default should be selected, got {name!r}"
    assert calls == [(request, "default", None)], f"the gate must see the selected name, got: {calls}"
    assert request.group == "tenant-a", "the helper must leave request.group as it found it"


@th.tier("bug")
@th.django_unit_test()
def test_graph_override_is_server_only(opts):
    """The override seam works for server code and nothing in params reaches it."""
    from mojo.apps.assistant.models import Skill
    from mojo.apps.assistant.services import model_serialization
    from mojo.apps.assistant.services.tools.models import _build_request

    skill = Skill.objects.filter(**opts.skill_filters).first()
    request = _build_request(opts.admin)

    # Server code passes it: honored, for an instance and for a queryset.
    row = model_serialization.serialize_instance(skill, request=request, graph_override="detail")
    assert "steps" in row and "metadata" in row, f"A server-passed override should be honored, got: {list(row)}"
    rows = model_serialization.serialize_queryset(
        Skill.objects.filter(**opts.skill_filters), request=request, graph_override="detail")
    assert all("steps" in r for r in rows), "A server-passed override should apply to a queryset"

    # ...and is validated like any selection.
    try:
        model_serialization.select_graph(Skill, graph_override="nope")
        assert False, "an unknown override must be refused"
    except model_serialization.AssistantGraphError:
        pass

    # Tool parameters cannot reach it, under any spelling.
    for key in ("graph_override", "override", "serialization"):
        result = _skill_query(opts, **{key: "detail"})
        assert "error" not in result, f"An unknown '{key}' key should not break the query: {result.get('error')}"
        for item in result["results"]:
            assert list(item) == SKILL_DEFAULT_KEYS, f"'{key}' in params must not change the graph, got: {list(item)}"
        assert SKILL_SECRET not in json.dumps(result), f"'{key}' in params widened the output"
