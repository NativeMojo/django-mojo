"""
Tests for two-tier tool loading — core tools always sent, domain tools loaded on demand.
"""
from testit import helpers as th
from testit.helpers import assert_true, assert_eq

TEST_EMAIL = 'two-tier-admin@example.com'
TEST_PASSWORD = 'TestPass1!'


@th.django_unit_setup()
@th.requires_app("mojo.apps.assistant")
def setup_two_tier(opts):
    from mojo.apps.account.models import User

    User.objects.filter(email=TEST_EMAIL).delete()

    opts.admin = User.objects.create_user(
        username=TEST_EMAIL, email=TEST_EMAIL, password=TEST_PASSWORD,
    )
    opts.admin.is_email_verified = True
    opts.admin.is_superuser = True
    opts.admin.save()
    opts.admin.add_permission("view_admin")
    opts.admin.add_permission("assistant")
    opts.admin.add_permission("view_security")
    opts.admin.add_permission("manage_security")
    opts.admin.add_permission("view_jobs")
    opts.admin.add_permission("manage_jobs")
    opts.admin.add_permission("view_logs")
    opts.admin.add_permission("view_fileman")


# ---------------------------------------------------------------------------
# Registry functions
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_core_tools_only(opts):
    """get_core_tools_for_user returns only core tools, not all 70."""
    from mojo.apps.assistant import get_core_tools_for_user, get_tools_for_user

    core = get_core_tools_for_user(opts.admin)
    all_tools = get_tools_for_user(opts.admin)

    assert_true(len(core) < len(all_tools),
                f"Core tools ({len(core)}) should be fewer than all tools ({len(all_tools)})")
    assert_true(len(core) <= 20,
                f"Core tools should be ~18, got {len(core)}")
    assert_true(len(all_tools) > 50,
                f"All tools should be 50+, got {len(all_tools)}")

    # Verify expected core tools are present
    core_names = {t["name"] for t in core}
    for expected in ["load_tools", "read_memory", "write_memory", "delete_memory",
                     "describe_model", "query_model", "read_docs", "browse_url",
                     "query_logs", "query_files", "get_file", "analyze_image"]:
        assert_true(expected in core_names,
                    f"Expected '{expected}' in core tools, got {sorted(core_names)}")

    # Verify domain tools are NOT in core
    for excluded in ["query_incidents", "query_jobs", "query_users", "block_ip",
                     "list_tools", "list_permissions"]:
        assert_true(excluded not in core_names,
                    f"'{excluded}' should not be in core tools")


@th.django_unit_test()
def test_core_flag_on_registration(opts):
    """Tools registered with core=True have the flag in the registry."""
    from mojo.apps.assistant import get_registry

    registry = get_registry()

    assert_eq(registry["load_tools"]["core"], True,
              "load_tools should be core=True")
    assert_eq(registry["read_memory"]["core"], True,
              "read_memory should be core=True")
    assert_eq(registry["query_incidents"]["core"], False,
              "query_incidents should be core=False")
    assert_eq(registry["list_tools"]["core"], False,
              "list_tools should be core=False")


@th.django_unit_test()
def test_domain_tools_for_user(opts):
    """get_domain_tools_for_user returns tools for specified domains only."""
    from mojo.apps.assistant import get_domain_tools_for_user

    jobs_tools = get_domain_tools_for_user(opts.admin, ["jobs"])
    assert_true(len(jobs_tools) >= 7,
                f"Expected 7+ jobs tools, got {len(jobs_tools)}")

    names = {t["name"] for t in jobs_tools}
    assert_true("query_jobs" in names, f"Expected query_jobs in jobs domain, got {names}")
    assert_true("list_job_channels" in names,
                f"Expected list_job_channels in jobs domain, got {names}")

    # Should not include tools from other domains
    assert_true("query_incidents" not in names,
                f"query_incidents should not be in jobs domain")


@th.django_unit_test()
def test_domain_tools_multi_domain(opts):
    """get_domain_tools_for_user with multiple domains returns tools from both."""
    from mojo.apps.assistant import get_domain_tools_for_user

    tools = get_domain_tools_for_user(opts.admin, ["jobs", "groups"])
    names = {t["name"] for t in tools}

    assert_true("query_jobs" in names, f"Expected query_jobs, got {names}")
    assert_true("query_groups" in names, f"Expected query_groups, got {names}")


@th.django_unit_test()
def test_available_domains(opts):
    """get_available_domains returns domains with counts, descriptions, and examples."""
    from mojo.apps.assistant import get_available_domains

    domains = get_available_domains(opts.admin)

    assert_true("security" in domains, f"Expected security domain, got {list(domains.keys())}")
    assert_true("jobs" in domains, f"Expected jobs domain, got {list(domains.keys())}")
    assert_true("users" in domains, f"Expected users domain, got {list(domains.keys())}")

    # Each domain has count, description, examples
    sec = domains["security"]
    assert_true(sec["count"] > 0, f"Expected security tool count > 0, got {sec['count']}")
    assert_true(len(sec["description"]) > 0,
                f"Expected security description, got '{sec['description']}'")
    assert_true(len(sec["examples"]) > 0,
                f"Expected security examples, got {sec['examples']}")

    # Core-only domains should NOT appear (they're already loaded)
    # Domains with non-core tools should appear
    assert_true("discovery" in domains,
                f"Expected discovery (has non-core list_tools), got {list(domains.keys())}")


@th.django_unit_test()
def test_available_domains_excludes_core_only(opts):
    """get_available_domains excludes domains where all tools are core."""
    from mojo.apps.assistant import get_available_domains

    domains = get_available_domains(opts.admin)

    # docs, web, logs have only core tools — should not appear
    for domain in ["docs", "web", "logs"]:
        assert_true(domain not in domains,
                    f"'{domain}' should not appear in available domains (core-only), "
                    f"got {list(domains.keys())}")


# ---------------------------------------------------------------------------
# Discovery tools moved to parent domains
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_discovery_tools_moved_to_domains(opts):
    """Domain-specific discovery tools now belong to their parent domains."""
    from mojo.apps.assistant import get_registry

    registry = get_registry()

    assert_eq(registry["list_job_channels"]["domain"], "jobs",
              "list_job_channels should be in jobs domain")
    assert_eq(registry["list_event_categories"]["domain"], "security",
              "list_event_categories should be in security domain")
    assert_eq(registry["list_metric_categories"]["domain"], "metrics",
              "list_metric_categories should be in metrics domain")
    assert_eq(registry["list_metric_slugs"]["domain"], "metrics",
              "list_metric_slugs should be in metrics domain")
    assert_eq(registry["list_permissions"]["domain"], "users",
              "list_permissions should be in users domain")


# ---------------------------------------------------------------------------
# load_tools tool handler
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_load_tools_lists_domains(opts):
    """load_tools with no domain returns available domains with metadata."""
    from mojo.apps.assistant import get_registry

    registry = get_registry()
    handler = registry["load_tools"]["handler"]

    result = handler({}, opts.admin)
    assert_true("domains" in result, f"Expected 'domains' in result, got {list(result.keys())}")

    domains = result["domains"]
    assert_true("security" in domains, f"Expected security in domains, got {list(domains.keys())}")
    assert_true("jobs" in domains, f"Expected jobs in domains, got {list(domains.keys())}")


@th.django_unit_test()
def test_load_tools_loads_single_domain(opts):
    """load_tools with domain returns tool definitions for that domain."""
    from mojo.apps.assistant import get_registry

    registry = get_registry()
    handler = registry["load_tools"]["handler"]

    result = handler({"domain": "jobs"}, opts.admin)
    assert_true("loaded" in result, f"Expected 'loaded' in result, got {list(result.keys())}")
    assert_true("jobs" in result["loaded"], f"Expected 'jobs' in loaded, got {list(result['loaded'].keys())}")

    jobs_tools = result["loaded"]["jobs"]
    assert_true(isinstance(jobs_tools, list), f"Expected list, got {type(jobs_tools)}")
    tool_names = [t["name"] for t in jobs_tools]
    assert_true("query_jobs" in tool_names, f"Expected query_jobs in loaded tools, got {tool_names}")


@th.django_unit_test()
def test_load_tools_loads_multiple_domains(opts):
    """load_tools with domains list returns tools from all requested domains."""
    from mojo.apps.assistant import get_registry

    registry = get_registry()
    handler = registry["load_tools"]["handler"]

    result = handler({"domains": ["jobs", "groups"]}, opts.admin)
    loaded = result["loaded"]
    assert_true("jobs" in loaded, f"Expected jobs in loaded, got {list(loaded.keys())}")
    assert_true("groups" in loaded, f"Expected groups in loaded, got {list(loaded.keys())}")


# ---------------------------------------------------------------------------
# Conversation tool building
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_build_tools_new_conversation(opts):
    """New conversation with no history gets core tools only."""
    from mojo.apps.assistant.models.conversation import Conversation
    from mojo.apps.assistant.services.agent import _build_tools_for_conversation

    conv = Conversation.objects.create(user=opts.admin, title="new")
    messages = []  # no history

    tools = _build_tools_for_conversation(opts.admin, conv, messages)
    names = {t["name"] for t in tools}

    assert_true("load_tools" in names, f"Expected load_tools in core, got {sorted(names)}")
    assert_true("query_incidents" not in names,
                f"query_incidents should not be in core tools for new conversation")

    conv.delete()


@th.django_unit_test()
def test_build_tools_with_active_domains(opts):
    """Conversation with active_domains in metadata loads those domain tools."""
    from mojo.apps.assistant.models.conversation import Conversation
    from mojo.apps.assistant.services.agent import _build_tools_for_conversation

    conv = Conversation.objects.create(
        user=opts.admin, title="resumed",
        metadata={"active_domains": ["jobs"]},
    )
    messages = []

    tools = _build_tools_for_conversation(opts.admin, conv, messages)
    names = {t["name"] for t in tools}

    # Should have core + jobs
    assert_true("load_tools" in names, f"Expected core tool load_tools, got {sorted(names)}")
    assert_true("query_jobs" in names, f"Expected jobs tool query_jobs, got {sorted(names)}")
    assert_true("query_incidents" not in names,
                f"query_incidents should not be loaded (not in active_domains)")

    conv.delete()


@th.django_unit_test()
def test_backward_compat_old_conversation(opts):
    """Old conversation with tool_use in history but no active_domains gets all tools."""
    from mojo.apps.assistant.models.conversation import Conversation
    from mojo.apps.assistant.services.agent import _build_tools_for_conversation

    conv = Conversation.objects.create(user=opts.admin, title="old conv")
    # Simulate history with tool_use block (from before two-tier)
    messages = [
        {"role": "user", "content": "show jobs"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "fake", "name": "query_jobs", "input": {}},
        ]},
    ]

    tools = _build_tools_for_conversation(opts.admin, conv, messages)
    names = {t["name"] for t in tools}

    # Should fall back to ALL tools
    assert_true("query_incidents" in names,
                f"Expected all tools for backward compat, query_incidents missing from {len(names)} tools")
    assert_true("query_jobs" in names,
                f"Expected query_jobs in all tools")
    assert_true(len(tools) > 50,
                f"Expected 50+ tools for backward compat, got {len(tools)}")

    conv.delete()


# ---------------------------------------------------------------------------
# _handle_load_tools
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_handle_load_tools_updates_metadata(opts):
    """_handle_load_tools adds domain to conversation metadata."""
    from mojo.apps.assistant.models.conversation import Conversation
    from mojo.apps.assistant.services.agent import _handle_load_tools
    from mojo.apps.assistant import get_core_tools_for_user

    conv = Conversation.objects.create(user=opts.admin, title="load test")
    tools = get_core_tools_for_user(opts.admin)
    initial_count = len(tools)

    added = _handle_load_tools(conv, {"domain": "jobs"}, tools, opts.admin)

    # Check metadata updated
    conv.refresh_from_db()
    assert_true("jobs" in conv.metadata.get("active_domains", []),
                f"Expected 'jobs' in active_domains, got {conv.metadata}")

    # Check tools were injected
    assert_true(len(tools) > initial_count,
                f"Expected tools to grow from {initial_count}, got {len(tools)}")
    assert_true(len(added) > 0, f"Expected newly added tool names, got {added}")

    names = {t["name"] for t in tools}
    assert_true("query_jobs" in names,
                f"Expected query_jobs after load, got {sorted(names)}")

    conv.delete()


@th.django_unit_test()
def test_handle_load_tools_no_duplicates(opts):
    """Loading the same domain twice doesn't duplicate tools."""
    from mojo.apps.assistant.models.conversation import Conversation
    from mojo.apps.assistant.services.agent import _handle_load_tools
    from mojo.apps.assistant import get_core_tools_for_user

    conv = Conversation.objects.create(user=opts.admin, title="dup test")
    tools = get_core_tools_for_user(opts.admin)

    # Load jobs twice
    _handle_load_tools(conv, {"domain": "jobs"}, tools, opts.admin)
    count_after_first = len(tools)

    added = _handle_load_tools(conv, {"domain": "jobs"}, tools, opts.admin)
    count_after_second = len(tools)

    assert_eq(count_after_first, count_after_second,
              f"Tool count should not change on duplicate load: {count_after_first} vs {count_after_second}")
    assert_eq(len(added), 0, f"No new tools should be added on duplicate load, got {added}")

    conv.delete()


@th.django_unit_test()
def test_handle_load_tools_multi_domain(opts):
    """_handle_load_tools accepts domains list."""
    from mojo.apps.assistant.models.conversation import Conversation
    from mojo.apps.assistant.services.agent import _handle_load_tools
    from mojo.apps.assistant import get_core_tools_for_user

    conv = Conversation.objects.create(user=opts.admin, title="multi load")
    tools = get_core_tools_for_user(opts.admin)

    added = _handle_load_tools(conv, {"domains": ["jobs", "groups"]}, tools, opts.admin)

    conv.refresh_from_db()
    active = conv.metadata.get("active_domains", [])
    assert_true("jobs" in active, f"Expected jobs in active_domains, got {active}")
    assert_true("groups" in active, f"Expected groups in active_domains, got {active}")

    names = {t["name"] for t in tools}
    assert_true("query_jobs" in names, f"Expected query_jobs after multi-load")
    assert_true("query_groups" in names, f"Expected query_groups after multi-load")

    conv.delete()


@th.django_unit_test()
def test_handle_load_tools_listing_mode(opts):
    """_handle_load_tools with no domain returns empty (listing mode, no injection)."""
    from mojo.apps.assistant.models.conversation import Conversation
    from mojo.apps.assistant.services.agent import _handle_load_tools
    from mojo.apps.assistant import get_core_tools_for_user

    conv = Conversation.objects.create(user=opts.admin, title="list mode")
    tools = get_core_tools_for_user(opts.admin)
    initial_count = len(tools)

    added = _handle_load_tools(conv, {}, tools, opts.admin)

    assert_eq(len(added), 0, f"Listing mode should not add tools, got {added}")
    assert_eq(len(tools), initial_count,
              f"Tool count should not change in listing mode")

    conv.delete()


# ---------------------------------------------------------------------------
# Permission filtering
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_permission_filter_on_domain_load(opts):
    """User without view_security gets no security tools even when loading."""
    from mojo.apps.account.models import User
    from mojo.apps.assistant import get_domain_tools_for_user

    # Create user without security perms
    email = "no-security@example.com"
    User.objects.filter(email=email).delete()
    limited = User.objects.create_user(username=email, email=email, password=TEST_PASSWORD)
    limited.is_email_verified = True
    limited.save()
    limited.add_permission("view_admin")
    limited.add_permission("assistant")
    # NOT adding view_security

    sec_tools = get_domain_tools_for_user(limited, ["security"])
    # Should get 0 security tools (all require view_security or manage_security)
    sec_names = {t["name"] for t in sec_tools}
    assert_true("query_incidents" not in sec_names,
                f"User without view_security should not get query_incidents, got {sec_names}")

    limited.delete()


# ---------------------------------------------------------------------------
# Unloaded tool execution
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_unloaded_tool_still_executes(opts):
    """Tool from an unloaded domain can still be executed via the registry."""
    from mojo.apps.assistant import get_registry

    registry = get_registry()

    # query_jobs is a non-core tool — even if not in the tools list sent to the LLM,
    # the registry can execute it
    entry = registry.get("query_jobs")
    assert_true(entry is not None, "query_jobs should be in registry")

    result = entry["handler"]({"status": "completed", "limit": 1}, opts.admin)
    # Should execute without error — result may be empty list which is fine
    assert_true(isinstance(result, list),
                f"Expected list result from query_jobs, got {type(result)}")
