"""
Tests for parallel tool execution and plan-aware batching.
"""
import time
from testit import helpers as th
from testit.helpers import assert_true, assert_eq


TEST_EMAIL = "parallel-test@example.com"
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
    for perm in ["view_admin", "assistant", "view_security", "view_jobs"]:
        opts.user.add_permission(perm)


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_execute_tool_single(opts):
    """_execute_tool should handle a single tool call correctly."""
    from mojo.apps.assistant.services.agent import _execute_tool
    from mojo.apps.assistant import get_registry
    from mojo.apps.assistant.models import Conversation

    Conversation.objects.filter(user=opts.user, title="exec-tool-single").delete()
    conv = Conversation.objects.create(user=opts.user, title="exec-tool-single")
    registry = get_registry()
    tool_calls_made = []

    # Use read_memory as a simple tool to test
    block = {
        "type": "tool_use",
        "id": "test-id-1",
        "name": "read_memory",
        "input": {},
    }

    result = _execute_tool(block, registry, opts.user, conv, [], None, tool_calls_made)
    assert_eq(result["type"], "tool_result", "Should return tool_result type")
    assert_eq(result["tool_use_id"], "test-id-1", "Should match tool_use_id")
    assert_true(len(tool_calls_made) == 1, f"Expected 1 tool call made, got {len(tool_calls_made)}")
    assert_eq(tool_calls_made[0]["tool"], "read_memory", "Tool call should be read_memory")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_execute_tool_unknown(opts):
    """_execute_tool should handle unknown tools gracefully."""
    from mojo.apps.assistant.services.agent import _execute_tool
    from mojo.apps.assistant import get_registry
    from mojo.apps.assistant.models import Conversation
    import ujson

    Conversation.objects.filter(user=opts.user, title="exec-tool-unknown").delete()
    conv = Conversation.objects.create(user=opts.user, title="exec-tool-unknown")
    registry = get_registry()
    tool_calls_made = []

    block = {
        "type": "tool_use",
        "id": "test-id-2",
        "name": "nonexistent_tool",
        "input": {},
    }

    result = _execute_tool(block, registry, opts.user, conv, [], None, tool_calls_made)
    parsed = ujson.loads(result["content"])
    assert_true("error" in parsed, "Should return error for unknown tool")
    assert_eq(len(tool_calls_made), 0, "Should not add unknown tool to calls made")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_execute_tools_single_no_threadpool(opts):
    """Single tool call should execute inline without ThreadPoolExecutor."""
    from mojo.apps.assistant.services.agent import _execute_tools
    from mojo.apps.assistant import get_registry
    from mojo.apps.assistant.models import Conversation

    Conversation.objects.filter(user=opts.user, title="exec-tools-single").delete()
    conv = Conversation.objects.create(user=opts.user, title="exec-tools-single")
    registry = get_registry()
    tool_calls_made = []

    blocks = [{
        "type": "tool_use",
        "id": "single-1",
        "name": "read_memory",
        "input": {},
    }]

    results = _execute_tools(blocks, registry, opts.user, conv, [], None, tool_calls_made)
    assert_eq(len(results), 1, f"Expected 1 result, got {len(results)}")
    assert_eq(results[0]["tool_use_id"], "single-1", "Result should match tool_use_id")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_execute_tools_multiple_concurrent(opts):
    """Multiple non-meta tools should execute concurrently."""
    from mojo.apps.assistant.services.agent import _execute_tools
    from mojo.apps.assistant import get_registry
    from mojo.apps.assistant.models import Conversation

    Conversation.objects.filter(user=opts.user, title="exec-tools-multi").delete()
    conv = Conversation.objects.create(user=opts.user, title="exec-tools-multi")
    registry = get_registry()
    tool_calls_made = []

    # Use multiple independent tools
    blocks = [
        {"type": "tool_use", "id": "multi-1", "name": "read_memory", "input": {}},
        {"type": "tool_use", "id": "multi-2", "name": "read_memory", "input": {"tier": "global"}},
        {"type": "tool_use", "id": "multi-3", "name": "read_memory", "input": {"tier": "user"}},
    ]

    results = _execute_tools(blocks, registry, opts.user, conv, [], None, tool_calls_made)
    assert_eq(len(results), 3, f"Expected 3 results, got {len(results)}")

    # Verify all tool_use_ids are present (order may vary due to concurrent execution)
    result_ids = {r["tool_use_id"] for r in results}
    assert_true("multi-1" in result_ids, "Should contain multi-1 result")
    assert_true("multi-2" in result_ids, "Should contain multi-2 result")
    assert_true("multi-3" in result_ids, "Should contain multi-3 result")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_execute_tools_meta_first(opts):
    """Meta-tools should execute before regular tools."""
    from mojo.apps.assistant.services.agent import _execute_tools
    from mojo.apps.assistant import get_registry
    from mojo.apps.assistant.models import Conversation

    Conversation.objects.filter(user=opts.user, title="exec-tools-meta").delete()
    conv = Conversation.objects.create(user=opts.user, title="exec-tools-meta")
    registry = get_registry()
    tool_calls_made = []

    # create_plan (meta) + read_memory (regular)
    blocks = [
        {
            "type": "tool_use", "id": "meta-1", "name": "create_plan",
            "input": {"title": "Test", "steps": [{"description": "Step 1"}]},
        },
        {"type": "tool_use", "id": "reg-1", "name": "read_memory", "input": {}},
    ]

    results = _execute_tools(blocks, registry, opts.user, conv, [], None, tool_calls_made)
    assert_eq(len(results), 2, f"Expected 2 results, got {len(results)}")

    # create_plan result should be first (meta-tools run serially before regular tools)
    assert_eq(results[0]["tool_use_id"], "meta-1",
              f"Meta-tool should execute first, got {results[0]['tool_use_id']}")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_execute_tools_error_isolation(opts):
    """One tool failing should not prevent other tools from completing."""
    from mojo.apps.assistant.services.agent import _execute_tools
    from mojo.apps.assistant import get_registry
    from mojo.apps.assistant.models import Conversation
    import ujson

    Conversation.objects.filter(user=opts.user, title="exec-tools-error").delete()
    conv = Conversation.objects.create(user=opts.user, title="exec-tools-error")
    registry = get_registry()
    tool_calls_made = []

    # One valid tool + one unknown tool
    blocks = [
        {"type": "tool_use", "id": "ok-1", "name": "read_memory", "input": {}},
        {"type": "tool_use", "id": "bad-1", "name": "nonexistent_tool", "input": {}},
    ]

    results = _execute_tools(blocks, registry, opts.user, conv, [], None, tool_calls_made)
    assert_eq(len(results), 2, f"Expected 2 results, got {len(results)}")

    # Both should have results — the bad one should be an error
    result_map = {r["tool_use_id"]: r for r in results}
    bad_result = ujson.loads(result_map["bad-1"]["content"])
    assert_true("error" in bad_result, "Unknown tool should return error")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_parallel_plan_steps_execute(opts):
    """Parallel plan steps with tools should execute concurrently."""
    from mojo.apps.assistant.services.agent import _execute_parallel_plan_steps
    from mojo.apps.assistant import get_registry
    from mojo.apps.assistant.models import Conversation

    Conversation.objects.filter(user=opts.user, title="plan-parallel").delete()
    conv = Conversation.objects.create(
        user=opts.user,
        title="plan-parallel",
        metadata={
            "plan": {
                "plan_id": "test-plan-parallel",
                "title": "Test Parallel",
                "steps": [
                    {"id": 1, "description": "Read memory global", "status": "pending",
                     "parallel": True, "tool": "read_memory", "tool_input": {"tier": "global"},
                     "summary": None},
                    {"id": 2, "description": "Read memory user", "status": "pending",
                     "parallel": True, "tool": "read_memory", "tool_input": {"tier": "user"},
                     "summary": None},
                    {"id": 3, "description": "Summarize", "status": "pending",
                     "parallel": False, "summary": None},
                ],
            },
        },
    )

    registry = get_registry()
    tool_calls_made = []
    events = []

    def on_event(event_type, data=None):
        events.append((event_type, data))

    plan = conv.metadata["plan"]
    results, blocks = _execute_parallel_plan_steps(
        plan, registry, opts.user, conv, [], on_event, tool_calls_made,
    )

    assert_eq(len(results), 2, f"Expected 2 parallel results, got {len(results)}")
    assert_eq(len(blocks), 2, f"Expected 2 fake blocks, got {len(blocks)}")

    # Verify steps were updated to done
    conv.refresh_from_db()
    step1 = conv.metadata["plan"]["steps"][0]
    step2 = conv.metadata["plan"]["steps"][1]
    step3 = conv.metadata["plan"]["steps"][2]
    assert_eq(step1["status"], "done", f"Step 1 should be done, got {step1['status']}")
    assert_eq(step2["status"], "done", f"Step 2 should be done, got {step2['status']}")
    assert_eq(step3["status"], "pending", f"Step 3 should still be pending, got {step3['status']}")

    # Verify WS events were published (in_progress + done for each step)
    plan_events = [e for e in events if e[0] == "plan_update"]
    assert_true(len(plan_events) >= 4,
                f"Expected at least 4 plan_update events (in_progress + done per step), got {len(plan_events)}")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_parallel_plan_skips_non_parallel(opts):
    """Sequential plan steps should not be executed by parallel runner."""
    from mojo.apps.assistant.services.agent import _execute_parallel_plan_steps
    from mojo.apps.assistant import get_registry
    from mojo.apps.assistant.models import Conversation

    Conversation.objects.filter(user=opts.user, title="plan-seq-only").delete()
    conv = Conversation.objects.create(
        user=opts.user,
        title="plan-seq-only",
        metadata={
            "plan": {
                "plan_id": "test-plan-seq",
                "title": "Sequential Only",
                "steps": [
                    {"id": 1, "description": "Summarize", "status": "pending",
                     "parallel": False, "summary": None},
                ],
            },
        },
    )

    registry = get_registry()
    tool_calls_made = []
    plan = conv.metadata["plan"]

    results, blocks = _execute_parallel_plan_steps(
        plan, registry, opts.user, conv, [], None, tool_calls_made,
    )

    assert_eq(len(results), 0, "No parallel steps should mean no results")
    assert_eq(len(blocks), 0, "No parallel steps should mean no blocks")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_parallel_plan_skips_already_done(opts):
    """Already-completed steps should not be re-executed."""
    from mojo.apps.assistant.services.agent import _execute_parallel_plan_steps
    from mojo.apps.assistant import get_registry
    from mojo.apps.assistant.models import Conversation

    Conversation.objects.filter(user=opts.user, title="plan-skip-done").delete()
    conv = Conversation.objects.create(
        user=opts.user,
        title="plan-skip-done",
        metadata={
            "plan": {
                "plan_id": "test-plan-done",
                "title": "Already Done",
                "steps": [
                    {"id": 1, "description": "Already done", "status": "done",
                     "parallel": True, "tool": "read_memory", "tool_input": {},
                     "summary": "Already done"},
                ],
            },
        },
    )

    registry = get_registry()
    tool_calls_made = []
    plan = conv.metadata["plan"]

    results, blocks = _execute_parallel_plan_steps(
        plan, registry, opts.user, conv, [], None, tool_calls_made,
    )

    assert_eq(len(results), 0, "Done steps should not be re-executed")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_parallel_plan_skips_mutating_tools(opts):
    """Mutating tools should be skipped in parallel plan execution."""
    from mojo.apps.assistant.services.agent import _execute_parallel_plan_steps
    from mojo.apps.assistant import get_registry
    from mojo.apps.assistant.models import Conversation

    Conversation.objects.filter(user=opts.user, title="plan-skip-mutating").delete()
    conv = Conversation.objects.create(
        user=opts.user,
        title="plan-skip-mutating",
        metadata={
            "plan": {
                "plan_id": "test-plan-mutating",
                "title": "Mutating Test",
                "steps": [
                    {"id": 1, "description": "Read memory", "status": "pending",
                     "parallel": True, "tool": "read_memory", "tool_input": {},
                     "summary": None},
                    {"id": 2, "description": "Write memory (mutating)", "status": "pending",
                     "parallel": True, "tool": "write_memory",
                     "tool_input": {"key": "test", "value": "test"},
                     "summary": None},
                ],
            },
        },
    )

    registry = get_registry()
    tool_calls_made = []
    plan = conv.metadata["plan"]

    results, blocks = _execute_parallel_plan_steps(
        plan, registry, opts.user, conv, [], None, tool_calls_made,
    )

    # Only the non-mutating tool should have executed
    assert_eq(len(results), 1, f"Expected 1 result (mutating skipped), got {len(results)}")
    assert_eq(len(blocks), 1, f"Expected 1 block (mutating skipped), got {len(blocks)}")

    # Mutating step should be marked as skipped
    conv.refresh_from_db()
    step2 = conv.metadata["plan"]["steps"][1]
    assert_eq(step2["status"], "skipped", f"Mutating step should be skipped, got {step2['status']}")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_create_plan_max_steps(opts):
    """create_plan should reject plans with more than 20 steps."""
    from mojo.apps.assistant.services.tools.planning import _tool_create_plan

    steps = [{"description": f"Step {i}"} for i in range(25)]
    result = _tool_create_plan({"title": "Too Many", "steps": steps}, opts.user)
    assert_true("error" in result, "Plan with >20 steps should be rejected")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_summarize_tool_result(opts):
    """_summarize_tool_result should extract useful summaries."""
    from mojo.apps.assistant.services.agent import _summarize_tool_result

    assert_eq(_summarize_tool_result({"error": "not found"}), "Error: not found",
              "Should summarize error results")
    assert_eq(_summarize_tool_result({"message": "No data"}), "No data",
              "Should use message field")
    assert_eq(_summarize_tool_result({"total": 42}), "42 results",
              "Should use total field")
    assert_eq(_summarize_tool_result([1, 2, 3]), "3 results",
              "Should count list items")
    assert_eq(_summarize_tool_result("plain string"), "Completed",
              "Should return Completed for unrecognized types")
