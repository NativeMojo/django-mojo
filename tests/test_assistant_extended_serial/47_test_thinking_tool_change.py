"""
Both Assistant tool loops drop thinking blocks when the tool list changes
(maestro item #10589, Part C). Anthropic signs a thinking block against the
tools it was given; replaying it after ``load_tools`` grew the list is
refused as ``provider_rejected``. When the tool list is unchanged the
thinking is kept. Serial because it patches the shared settings singleton
and ``llm.call``.
"""
import copy
from contextlib import contextmanager
from unittest import mock
from testit import helpers as th
from testit.helpers import assert_true, assert_eq


TEST_EMAIL = "thinking-tool-change-admin@example.com"
CONV_TITLE = "a10589_thinking_tool_change"


@th.django_unit_setup()
@th.requires_app("mojo.apps.assistant")
def setup_user(opts):
    from mojo.apps.account.models import User
    from mojo.apps.assistant.models import Conversation

    Conversation.objects.filter(title=CONV_TITLE).delete()
    User.objects.filter(email=TEST_EMAIL).delete()
    opts.user = User.objects.create_user(
        username=TEST_EMAIL, email=TEST_EMAIL, password="TestPass1!")
    opts.user.is_email_verified = True
    opts.user.save()
    for perm in ["view_admin", "assistant", "view_jobs"]:
        opts.user.add_permission(perm)


def _signature(tool_names):
    return "sig:" + ",".join(tool_names)


def _thinking(tool_names):
    return {"type": "thinking", "thinking": "Reasoning.", "signature": _signature(tool_names)}


def _load_tools_turns(names):
    """Thinking + load_tools(jobs), then thinking + a jobs tool, then the end."""
    return [
        lambda: {"stop_reason": "tool_use", "content": [
            _thinking(names()),
            {"type": "tool_use", "id": "toolu_01A", "name": "load_tools",
             "input": {"domain": "jobs"}},
        ]},
        lambda: {"stop_reason": "tool_use", "content": [
            _thinking(names()),
            {"type": "tool_use", "id": "toolu_01B", "name": "query_jobs",
             "input": {"limit": 1}},
        ]},
        lambda: {"stop_reason": "end_turn", "content": [{"type": "text", "text": "Done."}]},
    ]


def _same_tools_turns(names):
    """Thinking + read_memory (tool list unchanged), then the end."""
    return [
        lambda: {"stop_reason": "tool_use", "content": [
            _thinking(names()),
            {"type": "tool_use", "id": "toolu_01C", "name": "read_memory", "input": {}},
        ]},
        lambda: {"stop_reason": "end_turn", "content": [{"type": "text", "text": "Done."}]},
    ]


@contextmanager
def _fake_model(script):
    """Fake provider that refuses thinking signed under a different tool list.

    Records a copy of each call's messages and tool names.
    """
    from mojo.helpers import llm
    from mojo.helpers.settings import settings

    orig_get = settings.get
    calls = []
    current = {"names": []}
    turns = iter(script(lambda: current["names"]))

    def patched_get(name, *args, **kwargs):
        if name == "LLM_ADMIN_ENABLED":
            return True
        return orig_get(name, *args, **kwargs)

    def fake_call(messages, *args, **kwargs):
        names = [t["name"] for t in kwargs.get("tools") or []]
        calls.append({"messages": copy.deepcopy(messages), "tools": names})
        for msg in messages:
            if msg["role"] != "assistant" or not isinstance(msg["content"], list):
                continue
            for block in msg["content"]:
                if block.get("type") == "thinking" and block["signature"] != _signature(names):
                    raise llm.LLMExecutionError("provider_rejected")
        current["names"] = names
        return next(turns)()

    with mock.patch.object(settings, "get", side_effect=patched_get), \
            mock.patch.object(llm, "get_api_key", return_value="test-key"), \
            mock.patch(
                "mojo.apps.account.services.llm_safety.route_state",
                return_value={"ready": True, "error": ""}), \
            mock.patch("mojo.helpers.llm.call", side_effect=fake_call):
        yield calls


def _thinking_blocks(call):
    return [
        block for msg in call["messages"] if msg["role"] == "assistant"
        and isinstance(msg["content"], list)
        for block in msg["content"] if block.get("type") == "thinking"
    ]


def _assert_load_tools_run(result, calls):
    assert_true("error" not in result, f"run must succeed after load_tools, got: {result}")
    assert_eq(len(calls), 3, f"expected three model calls, got {len(calls)}")
    assert_true("query_jobs" in calls[1]["tools"],
                f"load_tools should add the jobs tools before call 2, got {calls[1]['tools']}")
    assert_eq(_thinking_blocks(calls[1]), [],
              "call 2 follows a tool-list change and must carry no thinking")
    assert_eq(len(_thinking_blocks(calls[2])), 1,
              "call 3 has the same tools as call 2, so call 2's thinking must be kept")
    tool_uses = [
        block["name"] for msg in calls[2]["messages"] if msg["role"] == "assistant"
        for block in msg["content"] if block.get("type") == "tool_use"
    ]
    assert_eq(tool_uses, ["load_tools", "query_jobs"],
              f"tool calls must stay in order after stripping, got {tool_uses}")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_http_loop_strips_thinking_after_load_tools(opts):
    from mojo.apps.assistant.models import Conversation
    from mojo.apps.assistant.services.agent import run_assistant

    Conversation.objects.filter(user=opts.user, title=CONV_TITLE).delete()
    with _fake_model(_load_tools_turns) as calls:
        result = run_assistant(opts.user, "hi")
    _assert_load_tools_run(result, calls)
    Conversation.objects.filter(pk=result.get("conversation_id")).delete()


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_ws_loop_strips_thinking_after_load_tools(opts):
    from mojo.apps.assistant.models import Conversation, Message
    from mojo.apps.assistant.services.agent import run_assistant_ws

    Conversation.objects.filter(user=opts.user, title=CONV_TITLE).delete()
    conv = Conversation.objects.create(user=opts.user, title=CONV_TITLE)
    Message.objects.create(conversation=conv, role="user", content="hi")
    with _fake_model(_load_tools_turns) as calls:
        result = run_assistant_ws(opts.user, "hi", conv.pk, on_event=lambda *a, **k: None)
    _assert_load_tools_run(result, calls)
    conv.delete()


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_unchanged_tools_keep_thinking(opts):
    from mojo.apps.assistant.models import Conversation
    from mojo.apps.assistant.services.agent import run_assistant

    Conversation.objects.filter(user=opts.user, title=CONV_TITLE).delete()
    with _fake_model(_same_tools_turns) as calls:
        result = run_assistant(opts.user, "hi")
    assert_true("error" not in result, f"run should succeed, got: {result}")
    assert_eq(len(calls), 2, f"expected two model calls, got {len(calls)}")
    assert_eq(calls[0]["tools"], calls[1]["tools"], "read_memory must not change the tool list")
    assert_eq(_thinking_blocks(calls[1]), [_thinking(calls[0]["tools"])],
              "with an unchanged tool list the thinking block must be sent back as is")
    Conversation.objects.filter(pk=result.get("conversation_id")).delete()
