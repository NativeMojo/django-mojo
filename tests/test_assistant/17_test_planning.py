"""
Tests for task planning tools: create_plan, update_plan, and progress block.
"""
from testit import helpers as th
from testit.helpers import assert_true, assert_eq


TEST_EMAIL = "planning-test@example.com"
TEST_PASSWORD = "TestPass1!"


@th.django_unit_setup()
@th.requires_app("mojo.apps.assistant")
def setup_user(opts):
    from mojo.apps.account.models import User

    User.objects.filter(email=TEST_EMAIL).delete()
    opts.user = User.objects.create_user(
        username=TEST_EMAIL, email=TEST_EMAIL, password=TEST_PASSWORD,
    )
    opts.user.is_email_verified = True
    opts.user.save()
    for perm in ["view_admin", "assistant"]:
        opts.user.add_permission(perm)


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_create_plan_tool_registered(opts):
    """create_plan and update_plan should be registered as core tools."""
    from mojo.apps.assistant import get_registry

    registry = get_registry()
    assert_true("create_plan" in registry, "create_plan should be registered")
    assert_true("update_plan" in registry, "update_plan should be registered")
    assert_true(registry["create_plan"]["core"], "create_plan should be a core tool")
    assert_true(registry["update_plan"]["core"], "update_plan should be a core tool")
    assert_eq(registry["create_plan"]["domain"], "planning",
              f"Expected domain 'planning', got {registry['create_plan']['domain']}")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_create_plan_returns_plan(opts):
    """create_plan should return a plan with steps and a plan_id."""
    from mojo.apps.assistant.services.tools.planning import _tool_create_plan

    result = _tool_create_plan({
        "title": "Security Audit",
        "steps": [
            {"description": "Check incidents", "parallel": True, "tool": "query_incidents", "tool_input": {"minutes": 60}},
            {"description": "Check jobs", "parallel": True, "tool": "query_jobs", "tool_input": {"status": "failed"}},
            {"description": "Summarize", "parallel": False},
        ],
    }, opts.user)

    assert_true("plan_id" in result, "Plan should have a plan_id")
    assert_eq(len(result["plan_id"]), 36, f"plan_id should be a UUID, got {result['plan_id']}")
    assert_eq(result["title"], "Security Audit", f"Title mismatch: {result['title']}")
    assert_eq(len(result["steps"]), 3, f"Expected 3 steps, got {len(result['steps'])}")

    # Check step structure
    step1 = result["steps"][0]
    assert_eq(step1["id"], 1, f"First step ID should be 1, got {step1['id']}")
    assert_eq(step1["status"], "pending", f"Initial status should be pending, got {step1['status']}")
    assert_true(step1["parallel"], "First step should be parallel")
    assert_eq(step1["tool"], "query_incidents", f"Tool mismatch: {step1['tool']}")
    assert_eq(step1["tool_input"]["minutes"], 60, "Tool input should be preserved")

    step3 = result["steps"][2]
    assert_true(not step3["parallel"], "Last step should not be parallel")
    assert_true("tool" not in step3, "Sequential step should not have a tool")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_create_plan_empty_steps_rejected(opts):
    """create_plan with no steps should return an error."""
    from mojo.apps.assistant.services.tools.planning import _tool_create_plan

    result = _tool_create_plan({"title": "Empty", "steps": []}, opts.user)
    assert_true("error" in result, "Empty plan should return error")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_update_plan_returns_update(opts):
    """update_plan should return the update for the agent loop to apply."""
    from mojo.apps.assistant.services.tools.planning import _tool_update_plan

    result = _tool_update_plan({
        "step_id": 1,
        "status": "done",
        "summary": "Found 3 open incidents",
    }, opts.user)

    assert_true(result.get("updated"), "Result should have updated=True")
    assert_eq(result["step_id"], 1, f"step_id mismatch: {result['step_id']}")
    assert_eq(result["status"], "done", f"status mismatch: {result['status']}")
    assert_eq(result["summary"], "Found 3 open incidents", f"summary mismatch")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_update_plan_invalid_status(opts):
    """update_plan with invalid status should return an error."""
    from mojo.apps.assistant.services.tools.planning import _tool_update_plan

    result = _tool_update_plan({"step_id": 1, "status": "invalid"}, opts.user)
    assert_true("error" in result, "Invalid status should return error")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_handle_plan_tool_create(opts):
    """_handle_plan_tool should store plan in conversation metadata."""
    from mojo.apps.assistant.services.agent import _handle_plan_tool
    from mojo.apps.assistant.models import Conversation

    Conversation.objects.filter(user=opts.user, title="plan-test-create").delete()
    conv = Conversation.objects.create(user=opts.user, title="plan-test-create")

    plan = {
        "plan_id": "test-plan-123",
        "title": "Test Plan",
        "steps": [
            {"id": 1, "description": "Step 1", "status": "pending", "parallel": False, "summary": None},
        ],
    }

    events = []
    def on_event(event_type, data=None):
        events.append((event_type, data))

    handled = _handle_plan_tool(conv, "create_plan", {}, plan, on_event)
    assert_true(handled, "_handle_plan_tool should return True for create_plan")

    conv.refresh_from_db()
    assert_true("plan" in conv.metadata, "Plan should be stored in conversation metadata")
    assert_eq(conv.metadata["plan"]["plan_id"], "test-plan-123",
              f"Plan ID mismatch: {conv.metadata['plan']['plan_id']}")

    assert_eq(len(events), 1, f"Expected 1 WS event, got {len(events)}")
    assert_eq(events[0][0], "plan", f"Expected 'plan' event, got {events[0][0]}")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_handle_plan_tool_update(opts):
    """_handle_plan_tool should update step status in conversation metadata."""
    from mojo.apps.assistant.services.agent import _handle_plan_tool
    from mojo.apps.assistant.models import Conversation

    Conversation.objects.filter(user=opts.user, title="plan-test-update").delete()
    conv = Conversation.objects.create(
        user=opts.user,
        title="plan-test-update",
        metadata={
            "plan": {
                "plan_id": "test-plan-456",
                "title": "Test Plan",
                "steps": [
                    {"id": 1, "description": "Step 1", "status": "pending", "parallel": False, "summary": None},
                    {"id": 2, "description": "Step 2", "status": "pending", "parallel": False, "summary": None},
                ],
            },
        },
    )

    events = []
    def on_event(event_type, data=None):
        events.append((event_type, data))

    update_result = {"step_id": 1, "status": "done", "summary": "All good", "updated": True}
    handled = _handle_plan_tool(conv, "update_plan", {}, update_result, on_event)
    assert_true(handled, "_handle_plan_tool should return True for update_plan")

    conv.refresh_from_db()
    step1 = conv.metadata["plan"]["steps"][0]
    assert_eq(step1["status"], "done", f"Step 1 status should be done, got {step1['status']}")
    assert_eq(step1["summary"], "All good", f"Step 1 summary mismatch: {step1['summary']}")

    step2 = conv.metadata["plan"]["steps"][1]
    assert_eq(step2["status"], "pending", f"Step 2 should still be pending, got {step2['status']}")

    assert_eq(len(events), 1, f"Expected 1 WS event, got {len(events)}")
    assert_eq(events[0][0], "plan_update", f"Expected 'plan_update' event, got {events[0][0]}")
    assert_eq(events[0][1]["step_id"], 1, "Event should reference step 1")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_handle_plan_tool_ignores_non_plan(opts):
    """_handle_plan_tool should return False for non-plan tools."""
    from mojo.apps.assistant.services.agent import _handle_plan_tool

    handled = _handle_plan_tool(None, "query_incidents", {}, {}, None)
    assert_true(not handled, "_handle_plan_tool should return False for non-plan tools")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_progress_block_parsed(opts):
    """Progress block type should be accepted by _parse_blocks."""
    from mojo.apps.assistant.services.agent import _parse_blocks

    text = """Here's the plan progress:

```assistant_block
{"type": "progress", "plan_id": "abc-123", "title": "Security Audit", "steps": [{"id": 1, "description": "Check incidents", "status": "done", "summary": "3 open"}, {"id": 2, "description": "Check jobs", "status": "in_progress", "summary": null}]}
```

Working on it."""

    clean, blocks = _parse_blocks(text)
    assert_eq(len(blocks), 1, f"Expected 1 block, got {len(blocks)}")
    assert_eq(blocks[0]["type"], "progress", f"Expected progress type, got {blocks[0]['type']}")
    assert_eq(blocks[0]["plan_id"], "abc-123", f"plan_id mismatch")
    assert_eq(len(blocks[0]["steps"]), 2, f"Expected 2 steps, got {len(blocks[0]['steps'])}")
