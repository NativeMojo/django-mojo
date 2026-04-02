"""
Tests for the assistant tool registry, permission gate, and feature flag.
"""
from testit import helpers as th
from testit.helpers import assert_true, assert_eq

TEST_EMAIL_ADMIN = 'assistant-admin@example.com'
TEST_EMAIL_LIMITED = 'assistant-limited@example.com'
TEST_EMAIL_NOPERMS = 'assistant-noperms@example.com'
TEST_PASSWORD = 'TestPass1!'


@th.django_unit_setup()
@th.requires_app("mojo.apps.assistant")
def setup_users(opts):
    from mojo.apps.account.models import User

    # Clean up prior test data
    User.objects.filter(email__in=[
        TEST_EMAIL_ADMIN, TEST_EMAIL_LIMITED, TEST_EMAIL_NOPERMS
    ]).delete()

    # Admin user with all perms
    opts.admin_user = User.objects.create_user(
        username=TEST_EMAIL_ADMIN, email=TEST_EMAIL_ADMIN, password=TEST_PASSWORD,
    )
    opts.admin_user.is_email_verified = True
    opts.admin_user.save()
    for perm in ["view_admin", "view_security", "manage_security",
                 "view_jobs", "manage_jobs", "view_groups"]:
        opts.admin_user.add_permission(perm)

    # Limited user — only view_security
    opts.limited_user = User.objects.create_user(
        username=TEST_EMAIL_LIMITED, email=TEST_EMAIL_LIMITED, password=TEST_PASSWORD,
    )
    opts.limited_user.is_email_verified = True
    opts.limited_user.save()
    opts.limited_user.add_permission("view_security")

    # No-perms user
    opts.noperms_user = User.objects.create_user(
        username=TEST_EMAIL_NOPERMS, email=TEST_EMAIL_NOPERMS, password=TEST_PASSWORD,
    )
    opts.noperms_user.is_email_verified = True
    opts.noperms_user.save()


@th.django_unit_test()
def test_registry_loaded(opts):
    """Tool registry should have all built-in tools registered."""
    from mojo.apps.assistant import get_registry
    registry = get_registry()
    assert_true(len(registry) >= 25, f"Expected at least 25 tools, got {len(registry)}")

    # Verify key tools exist
    for name in ["query_incidents", "query_jobs", "query_users",
                 "query_groups", "get_system_health"]:
        assert_true(name in registry, f"Expected tool '{name}' in registry")


@th.django_unit_test()
def test_get_tools_for_admin(opts):
    """Admin user should see tools for all their permitted domains."""
    from mojo.apps.assistant import get_tools_for_user
    tools = get_tools_for_user(opts.admin_user)
    tool_names = [t["name"] for t in tools]

    assert_true(len(tools) >= 25, f"Admin should see 25+ tools, got {len(tools)}")
    assert_true("query_incidents" in tool_names, "Admin should see security tools")
    assert_true("query_jobs" in tool_names, "Admin should see job tools")
    assert_true("query_users" in tool_names, "Admin should see user tools")
    assert_true("query_groups" in tool_names, "Admin should see group tools")
    assert_true("get_system_health" in tool_names, "Admin should see metrics tools")


@th.django_unit_test()
def test_get_tools_for_limited_user(opts):
    """Limited user should only see tools matching their permissions."""
    from mojo.apps.assistant import get_tools_for_user
    tools = get_tools_for_user(opts.limited_user)
    tool_names = [t["name"] for t in tools]

    # Should see view_security tools
    assert_true("query_incidents" in tool_names,
                "Limited user should see query_incidents (view_security)")
    assert_true("query_events" in tool_names,
                "Limited user should see query_events (view_security)")

    # Should NOT see manage_security tools
    assert_true("update_incident" not in tool_names,
                "Limited user should NOT see update_incident (manage_security)")
    assert_true("block_ip" not in tool_names,
                "Limited user should NOT see block_ip (manage_security)")

    # Should NOT see other domains
    assert_true("query_users" not in tool_names,
                "Limited user should NOT see query_users (view_admin)")
    assert_true("query_jobs" not in tool_names,
                "Limited user should NOT see query_jobs (view_jobs)")


@th.django_unit_test()
def test_get_tools_for_noperms_user(opts):
    """User with no assistant-relevant perms should see no tools."""
    from mojo.apps.assistant import get_tools_for_user
    tools = get_tools_for_user(opts.noperms_user)
    assert_eq(len(tools), 0, f"No-perms user should see 0 tools, got {len(tools)}")


@th.django_unit_test()
def test_permission_gate_direct(opts):
    """Permission gate should block unauthorized tool execution."""
    from mojo.apps.assistant import get_registry

    registry = get_registry()

    # Admin can execute query_incidents
    entry = registry["query_incidents"]
    assert_true(
        opts.admin_user.has_permission(entry["permission"]),
        "Admin should have view_security permission"
    )

    # Limited user can execute query_incidents (has view_security)
    assert_true(
        opts.limited_user.has_permission(entry["permission"]),
        "Limited user should have view_security permission"
    )

    # Limited user CANNOT execute update_incident (needs manage_security)
    entry = registry["update_incident"]
    assert_true(
        not opts.limited_user.has_permission(entry["permission"]),
        "Limited user should NOT have manage_security permission"
    )

    # No-perms user cannot execute any tool
    entry = registry["query_incidents"]
    assert_true(
        not opts.noperms_user.has_permission(entry["permission"]),
        "No-perms user should NOT have view_security permission"
    )


@th.django_unit_test()
def test_tool_handlers_return_bounded_results(opts):
    """Security tools should return bounded (capped) results, not unbounded querysets."""
    from mojo.apps.assistant.services.tools.security import _tool_query_incidents

    # Should not raise and should return a list
    result = _tool_query_incidents({"minutes": 1, "limit": 5}, opts.admin_user)
    assert_true(isinstance(result, list), f"Expected list, got {type(result).__name__}")
    assert_true(len(result) <= 5, f"Expected at most 5 results, got {len(result)}")


@th.django_unit_test()
def test_user_tool_excludes_sensitive_fields(opts):
    """User tools should never include password, auth_key, or onetime_code."""
    from mojo.apps.assistant.services.tools.users import _safe_user_dict

    user_dict = _safe_user_dict(opts.admin_user)
    sensitive = {"password", "auth_key", "onetime_code"}
    found = sensitive & set(user_dict.keys())
    assert_eq(len(found), 0, f"Sensitive fields exposed in user dict: {found}")


@th.django_unit_test()
def test_register_tool_duplicate_raises(opts):
    """Registering a tool with a duplicate name should raise ValueError."""
    from mojo.apps.assistant import register_tool

    raised = False
    try:
        register_tool(
            name="query_incidents",
            description="duplicate",
            input_schema={"type": "object", "properties": {}},
            handler=lambda p, u: {},
            permission="view_security",
        )
    except ValueError:
        raised = True

    assert_true(raised, "Expected ValueError for duplicate tool registration")


@th.django_unit_test()
def test_feature_disabled_returns_error(opts):
    """run_assistant should return error when LLM_ADMIN_ENABLED is False."""
    from mojo.apps.assistant.services.agent import run_assistant

    # Feature is disabled by default in test settings
    result = run_assistant(opts.admin_user, "hello")
    assert_true("error" in result, f"Expected error key in result: {result}")
    assert_true("not enabled" in result["error"].lower(),
                f"Expected 'not enabled' in error: {result['error']}")


@th.django_unit_test()
def test_assistant_endpoint_requires_auth(opts):
    """POST /api/assistant should require view_admin permission."""
    # No login — should fail
    opts.client.logout()
    resp = opts.client.post('/api/assistant', {'message': 'hello'})
    assert_true(resp.status_code in (401, 403),
                f"Expected 401/403 without auth, got {resp.status_code}")


@th.django_unit_test()
def test_assistant_endpoint_requires_perms(opts):
    """POST /api/assistant should require view_admin permission."""
    opts.client.login(TEST_EMAIL_NOPERMS, TEST_PASSWORD)
    resp = opts.client.post('/api/assistant', {'message': 'hello'})
    assert_true(resp.status_code in (401, 403),
                f"Expected 401/403 for user without view_admin, got {resp.status_code}")
