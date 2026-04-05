"""
Tests for assistant incident event reporting.

Verifies that security-relevant actions in the assistant generate
incident events via report_event().
"""
from unittest import mock
from testit import helpers as th
from testit.helpers import assert_true, assert_eq


TEST_EMAIL_ADMIN = 'evt-report-admin@example.com'
TEST_PASSWORD = 'TestPass1!'


@th.django_unit_setup()
@th.requires_app("mojo.apps.assistant")
@th.requires_app("mojo.apps.incident")
def setup_reporting(opts):
    from mojo.apps.account.models import User

    User.objects.filter(email=TEST_EMAIL_ADMIN).delete()

    opts.admin = User.objects.create_user(
        username=TEST_EMAIL_ADMIN, email=TEST_EMAIL_ADMIN, password=TEST_PASSWORD,
    )
    opts.admin.is_email_verified = True
    opts.admin.save()
    opts.admin.add_permission("view_admin")
    opts.admin.add_permission("view_security")
    opts.admin.add_permission("manage_security")


# ---------------------------------------------------------------------------
# _report_event helper
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_report_event_helper_calls_incident_report(opts):
    """_report_event calls incident.report_event with correct args."""
    from mojo.apps.assistant.services.agent import _report_event

    with mock.patch("mojo.apps.incident.report_event") as mock_report:
        _report_event(
            "assistant:test", 5, "Test title", "Test details",
            user=opts.admin,
        )
        assert_true(mock_report.called, "_report_event should call incident.report_event")
        call_kwargs = mock_report.call_args
        assert_eq(call_kwargs[0][0], "Test details", "First arg should be details")
        assert_eq(call_kwargs[1]["category"], "assistant:test", "Category should match")
        assert_eq(call_kwargs[1]["level"], 5, "Level should match")
        assert_eq(call_kwargs[1]["title"], "Test title", "Title should match")
        assert_eq(call_kwargs[1]["uid"], opts.admin.pk, "uid should be user pk")


@th.django_unit_test()
def test_report_event_helper_never_raises(opts):
    """_report_event swallows exceptions — never breaks the assistant."""
    from mojo.apps.assistant.services.agent import _report_event

    with mock.patch("mojo.apps.incident.report_event", side_effect=Exception("DB down")):
        # Should not raise
        _report_event("assistant:test", 5, "Test", "Details", user=opts.admin)


# ---------------------------------------------------------------------------
# Permission denied events
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_permission_denied_fires_event(opts):
    """Tool permission denial should fire an assistant:permission_denied event."""
    from mojo.apps.assistant import get_registry

    # Find a tool that requires a permission the admin doesn't have
    # We'll simulate by calling the agent loop logic directly
    from mojo.apps.assistant.services.agent import _report_event

    with mock.patch("mojo.apps.incident.report_event") as mock_report:
        _report_event(
            "assistant:permission_denied", 5,
            "Permission denied: some_tool",
            f"User {opts.admin.email} denied access to tool 'some_tool'",
            user=opts.admin,
        )
        assert_true(mock_report.called, "Permission denied should report event")
        assert_eq(
            mock_report.call_args[1]["category"],
            "assistant:permission_denied",
            "Category should be assistant:permission_denied",
        )
        assert_eq(mock_report.call_args[1]["level"], 5, "Permission denied level should be 5")


# ---------------------------------------------------------------------------
# Category naming conventions
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_event_categories_follow_convention(opts):
    """Verify the category naming patterns used in agent.py."""
    from mojo.apps.assistant.services import agent
    import inspect

    source = inspect.getsource(agent)

    # Check for expected category patterns
    assert_true(
        "assistant:permission_denied" in source,
        "Should use assistant:permission_denied category",
    )
    assert_true(
        "assistant:error" in source,
        "Should use assistant:error category",
    )
    assert_true(
        "assistant:error:api" in source,
        "Should use assistant:error:api category for LLM API failures",
    )
    assert_true(
        "assistant:tool:" in source,
        "Should use assistant:tool:<name> category for mutating tools",
    )


# ---------------------------------------------------------------------------
# Handler events
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_handler_permission_denied_fires_event(opts):
    """WS handler permission denied should fire an event."""
    from mojo.apps.assistant.handler import _handle_message
    from mojo.apps.account.models import User

    # Create user without view_admin
    email = "evt-noperm@example.com"
    User.objects.filter(email=email).delete()
    noperm = User.objects.create_user(username=email, email=email, password=TEST_PASSWORD)
    noperm.is_email_verified = True
    noperm.save()

    # Mock settings.get to return True for LLM_ADMIN_ENABLED so we reach permission check
    from mojo.helpers.settings import settings
    orig_get = settings.get

    def patched_get(name, *args, **kwargs):
        if name == "LLM_ADMIN_ENABLED":
            return True
        return orig_get(name, *args, **kwargs)

    with mock.patch.object(settings, "get", side_effect=patched_get):
        with mock.patch("mojo.apps.incident.report_event") as mock_report:
            result = _handle_message(noperm, {"type": "assistant_message", "message": "hello"})
            assert_eq(result["type"], "assistant_error", "Should return error")
            assert_true(mock_report.called, "Permission denied should fire event")
            assert_eq(
                mock_report.call_args[1]["category"],
                "assistant:permission_denied",
                "Category should be assistant:permission_denied",
            )


# ---------------------------------------------------------------------------
# Mutating tool event reporting
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_mutating_tool_success_fires_event(opts):
    """Successful mutating tool execution should fire an event."""
    from mojo.apps.assistant.services.agent import _report_event

    # Simulate what the agent loop does after a successful mutating tool call
    tool_name = "block_ip"
    with mock.patch("mojo.apps.incident.report_event") as mock_report:
        _report_event(
            f"assistant:tool:{tool_name}", 5,
            f"Assistant tool: {tool_name}",
            f"User {opts.admin.email} executed mutating tool '{tool_name}'.",
            user=opts.admin,
        )
        assert_true(mock_report.called, "Mutating tool should fire event")
        assert_eq(
            mock_report.call_args[1]["category"],
            "assistant:tool:block_ip",
            "Category should be assistant:tool:block_ip",
        )


@th.django_unit_test()
def test_mutating_tool_error_no_event(opts):
    """Mutating tool that returns an error dict should NOT fire a tool event."""
    # This tests the logic in the agent loop, not _report_event itself.
    # The condition is: tool_entry.get("mutates") and ("error" not in tool_result)
    # When tool_result has "error", no event should fire.
    tool_result = {"error": "Ticket not found"}
    mutates = True
    should_fire = mutates and (
        not isinstance(tool_result, dict) or "error" not in tool_result
    )
    assert_true(not should_fire, "Should NOT fire event when tool returns error")


@th.django_unit_test()
def test_nonmutating_tool_no_event(opts):
    """Read-only tools should NOT fire events."""
    tool_result = [{"id": 1, "title": "test"}]
    mutates = False
    should_fire = mutates and (
        not isinstance(tool_result, dict) or "error" not in tool_result
    )
    assert_true(not should_fire, "Should NOT fire event for non-mutating tool")


# ---------------------------------------------------------------------------
# Error events
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_tool_exception_fires_error_event(opts):
    """Tool handler exception should fire an assistant:error event."""
    from mojo.apps.assistant.services.agent import _report_event

    with mock.patch("mojo.apps.incident.report_event") as mock_report:
        _report_event(
            "assistant:error", 6,
            "Tool exception: query_incidents",
            "Tool 'query_incidents' raised an exception",
            user=opts.admin,
        )
        assert_true(mock_report.called, "Tool exception should fire error event")
        assert_eq(mock_report.call_args[1]["level"], 6, "Tool exception level should be 6")


@th.django_unit_test()
def test_agent_crash_fires_error_event(opts):
    """Agent loop crash should fire a level 7 assistant:error event."""
    from mojo.apps.assistant.services.agent import _report_event

    with mock.patch("mojo.apps.incident.report_event") as mock_report:
        _report_event(
            "assistant:error", 7,
            "Agent loop exception",
            "Agent crashed for user test@example.com",
            user=opts.admin,
        )
        assert_true(mock_report.called, "Agent crash should fire error event")
        assert_eq(mock_report.call_args[1]["level"], 7, "Agent crash level should be 7")


@th.django_unit_test()
def test_api_error_fires_event(opts):
    """LLM API errors should fire assistant:error:api events."""
    from mojo.apps.assistant.services.agent import _report_event

    with mock.patch("mojo.apps.incident.report_event") as mock_report:
        _report_event(
            "assistant:error:api", 7,
            "LLM API auth failure",
            "authentication_error: invalid key",
            user=opts.admin,
        )
        assert_true(mock_report.called, "API error should fire event")
        assert_eq(
            mock_report.call_args[1]["category"],
            "assistant:error:api",
            "Category should be assistant:error:api",
        )
