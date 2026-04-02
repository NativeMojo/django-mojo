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
                 "query_groups", "get_system_health",
                 "list_tools", "list_metric_categories", "list_metric_slugs",
                 "list_job_channels", "list_event_categories", "list_permissions"]:
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
def test_parse_blocks_extracts_structured_data(opts):
    """_parse_blocks should extract assistant_block fences from LLM output."""
    from mojo.apps.assistant.services.agent import _parse_blocks

    text = """Here are the failed jobs:

```assistant_block
{"type": "table", "title": "Failed Jobs", "columns": ["ID", "Error"], "rows": [["abc", "timeout"]]}
```

And here's the trend:

```assistant_block
{"type": "chart", "chart_type": "line", "title": "Events", "labels": ["Mon", "Tue"], "series": [{"name": "events", "values": [10, 20]}]}
```

That's the summary."""

    clean, blocks = _parse_blocks(text)
    assert_eq(len(blocks), 2, f"Expected 2 blocks, got {len(blocks)}")
    assert_eq(blocks[0]["type"], "table", f"First block should be table, got {blocks[0]['type']}")
    assert_eq(blocks[1]["type"], "chart", f"Second block should be chart, got {blocks[1]['type']}")
    assert_true("assistant_block" not in clean,
                "Block fences should be removed from clean text")
    assert_true("Here are the failed jobs" in clean,
                "Narrative text should be preserved")
    assert_true("That's the summary" in clean,
                "Trailing text should be preserved")


@th.django_unit_test()
def test_parse_blocks_handles_no_blocks(opts):
    """_parse_blocks should handle text with no blocks gracefully."""
    from mojo.apps.assistant.services.agent import _parse_blocks

    text = "Just a plain text response with no structured data."
    clean, blocks = _parse_blocks(text)
    assert_eq(len(blocks), 0, "Expected 0 blocks for plain text")
    assert_eq(clean, text, "Clean text should be unchanged")


@th.django_unit_test()
def test_parse_blocks_rejects_invalid_types(opts):
    """_parse_blocks should reject blocks with unknown types."""
    from mojo.apps.assistant.services.agent import _parse_blocks

    text = """Some text.

```assistant_block
{"type": "malicious", "data": "bad"}
```

More text."""

    clean, blocks = _parse_blocks(text)
    assert_eq(len(blocks), 0, "Invalid block types should be rejected")


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
def test_list_tools_returns_permitted_tools(opts):
    """list_tools should return tools grouped by domain, filtered by user perms."""
    from mojo.apps.assistant.services.tools.discovery import _tool_list_tools

    # Admin sees all domains
    result = _tool_list_tools({}, opts.admin_user)
    assert_true(result["total_tools"] >= 30,
                f"Admin should see 30+ tools, got {result['total_tools']}")
    assert_true("security" in result["domains"], "Admin should see security domain")
    assert_true("discovery" in result["domains"], "Admin should see discovery domain")

    # Limited user only sees security + discovery (view_security gives incident_trends)
    result = _tool_list_tools({}, opts.limited_user)
    domains = set(result["domains"].keys())
    assert_true("security" in domains,
                f"Limited user should see security domain, got {domains}")
    assert_true("jobs" not in domains,
                f"Limited user should NOT see jobs domain, got {domains}")
    assert_true("users" not in domains,
                f"Limited user should NOT see users domain, got {domains}")

    # Filter by domain
    result = _tool_list_tools({"domain": "security"}, opts.admin_user)
    assert_true("security" in result["domains"], "Should see security when filtered")
    assert_eq(len(result["domains"]), 1,
              f"Should only see 1 domain when filtered, got {len(result['domains'])}")


@th.django_unit_test()
def test_list_event_categories(opts):
    """list_event_categories should return a list of category strings."""
    from mojo.apps.assistant.services.tools.discovery import _tool_list_event_categories

    result = _tool_list_event_categories({"minutes": 1440}, opts.admin_user)
    assert_true("categories" in result, f"Expected 'categories' key, got {result.keys()}")
    assert_true(isinstance(result["categories"], list),
                f"Expected list, got {type(result['categories']).__name__}")
    assert_true("count" in result, "Expected 'count' key in result")


@th.django_unit_test()
def test_list_job_channels(opts):
    """list_job_channels should return configured channels."""
    from mojo.apps.assistant.services.tools.discovery import _tool_list_job_channels

    result = _tool_list_job_channels({}, opts.admin_user)
    assert_true("channels" in result, f"Expected 'channels' key, got {result.keys()}")
    assert_true(len(result["channels"]) >= 1,
                f"Expected at least 1 channel, got {len(result['channels'])}")
    # Each channel should have name and queue_depth
    ch = result["channels"][0]
    assert_true("name" in ch, "Channel should have 'name' field")
    assert_true("queue_depth" in ch, "Channel should have 'queue_depth' field")


@th.django_unit_test()
def test_list_permissions(opts):
    """list_permissions should return known permission keys from RestMeta."""
    from mojo.apps.assistant.services.tools.discovery import _tool_list_permissions

    result = _tool_list_permissions({}, opts.admin_user)
    assert_true(result["count"] >= 5,
                f"Expected at least 5 permissions, got {result['count']}")
    # Known permissions should be present
    perms = result["permissions"]
    for p in ["view_security", "manage_security", "view_jobs", "view_admin"]:
        assert_true(p in perms, f"Expected '{p}' in permissions list")
    # Meta-perms should be excluded
    for p in ["owner", "all", "authenticated"]:
        assert_true(p not in perms, f"'{p}' should NOT be in permissions list")


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
