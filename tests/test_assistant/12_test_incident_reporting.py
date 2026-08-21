"""
Tests for assistant incident event reporting.

Verifies that security-relevant actions in the assistant generate
incident events via report_event().
"""
from testit import helpers as th
from testit.helpers import assert_true, assert_eq


TEST_EMAIL_ADMIN = 'evt-report-admin@example.com'
TEST_PASSWORD = 'TestPass1!'


def _clear_events(user, category):
    from mojo.apps.incident.models import Event
    Event.objects.filter(uid=user.pk, category=category).delete()


def _event(user, category):
    from mojo.apps.incident.models import Event
    return Event.objects.filter(uid=user.pk, category=category).latest("pk")


def _real_reporter():
    # Import the concrete callable, not the package alias that other parallel
    # packages intentionally patch while testing their own call sites.
    from mojo.apps.incident.reporter import report_event
    return report_event


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

    _clear_events(opts.admin, "assistant:test")
    # Converted for maestro item #1839: this used to wrap the call in
    # mock.patch("mojo.apps.incident.report_event") to simulate a parallel
    # package patching the public alias — a process-wide mutation that is
    # itself unsafe in the parallel default tier. The call-local _reporter=
    # seam is what the test asserts, and it needs no shared patch.
    _report_event(
        "assistant:test", 5, "Test title", "Test details",
        user=opts.admin, _reporter=_real_reporter(),
    )
    event = _event(opts.admin, "assistant:test")
    assert_eq(event.details, "Test details", "incident details should match")
    assert_eq(event.category, "assistant:test", "Category should match")
    assert_eq(event.level, 5, "Level should match")
    assert_eq(event.title, "Test title", "Title should match")
    assert_eq(event.uid, opts.admin.pk, "uid should be user pk")


@th.django_unit_test()
def test_report_event_helper_never_raises(opts):
    """_report_event swallows exceptions — never breaks the assistant."""
    from mojo.apps.assistant.services.agent import _report_event

    calls = []

    def failing_reporter(*args, **kwargs):
        calls.append((args, kwargs))
        raise Exception("DB down")

    # The injected reporter is call-local. A process-wide mock here races every
    # other module because testit runs packages in shared-process threads.
    _report_event(
        "assistant:test", 5, "Test", "Details", user=opts.admin,
        _reporter=failing_reporter,
    )
    assert_eq(len(calls), 1, "the call-local failing reporter must be exercised once")


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

    _clear_events(opts.admin, "assistant:permission_denied")
    _report_event(
        "assistant:permission_denied", 5,
        "Permission denied: some_tool",
        f"User {opts.admin.email} denied access to tool 'some_tool'",
        user=opts.admin, _reporter=_real_reporter(),
    )
    event = _event(opts.admin, "assistant:permission_denied")
    assert_eq(
        event.category,
        "assistant:permission_denied",
        "Category should be assistant:permission_denied",
    )
    assert_eq(event.level, 5, "Permission denied level should be 5")


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

# test_handler_permission_denied_fires_event moved to
# tests/test_assistant_extended_serial/12_test_incident_reporting.py — it
# mock.patches the shared settings singleton (mojo.helpers.settings.settings)
# around an in-process handler call (maestro item #1839).


# ---------------------------------------------------------------------------
# Mutating tool event reporting
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_mutating_tool_success_fires_event(opts):
    """Successful mutating tool execution should fire an event."""
    from mojo.apps.assistant.services.agent import _report_event

    # Simulate what the agent loop does after a successful mutating tool call
    tool_name = "block_ip"
    category = f"assistant:tool:{tool_name}"
    _clear_events(opts.admin, category)
    _report_event(
        category, 5,
        f"Assistant tool: {tool_name}",
        f"User {opts.admin.email} executed mutating tool '{tool_name}'.",
        user=opts.admin, _reporter=_real_reporter(),
    )
    event = _event(opts.admin, category)
    assert_eq(
        event.category,
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

    _clear_events(opts.admin, "assistant:error")
    _report_event(
        "assistant:error", 6,
        "Tool exception: query_incidents",
        "Tool 'query_incidents' raised an exception",
        user=opts.admin, _reporter=_real_reporter(),
    )
    event = _event(opts.admin, "assistant:error")
    assert_eq(event.level, 6, "Tool exception level should be 6")


@th.django_unit_test()
def test_agent_crash_fires_error_event(opts):
    """Agent loop crash should fire a level 7 assistant:error event."""
    from mojo.apps.assistant.services.agent import _report_event

    _clear_events(opts.admin, "assistant:error")
    _report_event(
        "assistant:error", 7,
        "Agent loop exception",
        "Agent crashed for user test@example.com",
        user=opts.admin, _reporter=_real_reporter(),
    )
    event = _event(opts.admin, "assistant:error")
    assert_eq(event.level, 7, "Agent crash level should be 7")


@th.django_unit_test()
def test_api_error_fires_event(opts):
    """LLM API errors should fire assistant:error:api events."""
    from mojo.apps.assistant.services.agent import _report_event

    _clear_events(opts.admin, "assistant:error:api")
    _report_event(
        "assistant:error:api", 7,
        "LLM API auth failure",
        "authentication_error: invalid key",
        user=opts.admin, _reporter=_real_reporter(),
    )
    event = _event(opts.admin, "assistant:error:api")
    assert_eq(
        event.category,
        "assistant:error:api",
        "Category should be assistant:error:api",
    )
