"""Guarded prompt caching on a Bedrock Claude route (#11820).

Serial and opt-in for the same reason as llm_safety.py: the guard reads the
settings singleton and the protected policy-hash row, which _guard_case
patches for the fixed installation identity.
"""

import json

from testit import helpers as th

from .llm_bedrock_claude import CLAUDE, _claude_case, _claude_policy
from .llm_safety import _assert_budget_total, _assert_denied, _guard_case, _guard_policy
from .llm_stream import _Client, _events, _guarded, _leases_released


TOOLS = [{"name": "lookup_item", "description": "Look up a board item",
          "input_schema": {"type": "object", "properties": {"id": {"type": "integer"}},
                           "required": ["id"]}}]
SYSTEM = [{"type": "text", "text": "You are on the call."},
          {"type": "text", "text": "Answer briefly."}]
MESSAGES = [
    {"role": "user", "content": "Here is the meeting so far."},
    {"role": "assistant", "content": [
        {"type": "text", "text": "Checking."},
        {"type": "tool_use", "id": "tooluse_1", "name": "lookup_item", "input": {"id": 7}}]},
    {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "tooluse_1", "content": "open"},
        {"type": "text", "text": "What did we decide?"}]},
]
NOVA = "us.amazon.nova-2-lite-v1:0"


def _golden(model):
    """The Converse request django-mojo 1.42.9 sent for SYSTEM/MESSAGES/TOOLS at
    effort low, captured from that code. Key order is part of the golden."""
    extra = {"thinking": {"type": "adaptive"}, "output_config": {"effort": "low"}} \
        if model == CLAUDE else \
        {"reasoningConfig": {"type": "enabled", "maxReasoningEffort": "low"}}
    return {
        "modelId": model,
        "messages": [
            {"role": "user", "content": [{"text": "Here is the meeting so far."}]},
            {"role": "assistant", "content": [
                {"text": "Checking."},
                {"toolUse": {"toolUseId": "tooluse_1", "name": "lookup_item",
                             "input": {"id": 7}}}]},
            {"role": "user", "content": [
                {"toolResult": {"toolUseId": "tooluse_1", "content": [{"text": "open"}]}},
                {"text": "What did we decide?"}]}],
        "inferenceConfig": {"maxTokens": 64},
        "system": [{"text": "You are on the call."}, {"text": "Answer briefly."}],
        "toolConfig": {"tools": [{"toolSpec": {
            "name": "lookup_item", "description": "Look up a board item",
            "inputSchema": {"json": {"type": "object",
                                     "properties": {"id": {"type": "integer"}},
                                     "required": ["id"]}}}}]},
        "additionalModelRequestFields": extra,
    }


def _reply():
    return {"ResponseMetadata": {"RequestId": "bedrock-11820-request"},
            "output": {"message": {"role": "assistant", "content": [{"text": "decided"}]}},
            "stopReason": "end_turn",
            "usage": {"inputTokens": 7, "outputTokens": 3, "cacheReadInputTokens": 500,
                      "cacheWriteInputTokens": 40, "totalTokens": 550,
                      "cacheDetails": [{"ttl": "1h", "inputTokens": 40}]}}


def _run_both(case, model, **kwargs):
    """One call and one stream of the golden request; returns both wire requests."""
    client = _Client(replies=[_reply()], streams=[_events(("ok",))])
    for stream in (False, True):
        answer = _guarded(case, client, stream=stream, system=SYSTEM, tools=TOOLS,
                          messages=MESSAGES, max_tokens=64, reasoning_effort="low",
                          **kwargs)
        if stream:
            list(answer)
    assert [name for name, _ in client.calls] == ["converse", "converse_stream"], \
        f"the golden request must reach both Converse and ConverseStream: {client.calls}"
    return [request for _, request in client.calls]


@th.django_unit_test()
def test_routes_without_prompt_cache_send_todays_request(opts):
    claude = _claude_policy(("text", "tools"))
    nova = _guard_policy()
    nova["routes"]["assistant"]["capabilities"] = ["text", "tools"]
    for model, raw, case_for in ((CLAUDE, claude, _claude_case), (NOVA, nova, _guard_case)):
        with case_for(raw) as case:
            for wire in _run_both(case, model):
                assert json.dumps(wire) == json.dumps(_golden(model)), \
                    f"a {model} route without prompt_cache changed its request: {wire}"


@th.django_unit_test()
def test_declaring_prompt_cache_alone_changes_nothing_on_the_wire(opts):
    with _claude_case(_claude_policy(("text", "tools", "prompt_cache"))) as case:
        for wire in _run_both(case, CLAUDE):
            assert json.dumps(wire) == json.dumps(_golden(CLAUDE)), \
                f"a caching route with no markers must send today's request: {wire}"


@th.django_unit_test()
def test_cached_route_sends_checkpoints_and_settles_cache_usage(opts):
    from mojo.apps.account.models import LLMRequest

    system = [{"type": "text", "text": "You are on the call.",
               "cache_control": {"type": "ephemeral", "ttl": "1h"}}]
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "The transcript.", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "What did we decide?"}]}]
    with _claude_case(_claude_policy(("text", "prompt_cache"))) as case:
        client = _Client(replies=[_reply()], streams=[_events(("ok",))])
        response = _guarded(case, client, stream=False, system=system, messages=messages)
        list(_guarded(case, client, system=system, messages=messages))
        for name, wire in client.calls:
            assert wire["system"] == [{"text": "You are on the call."},
                                      {"cachePoint": {"type": "default", "ttl": "1h"}}] \
                and wire["messages"] == [{"role": "user", "content": [
                    {"text": "The transcript."}, {"cachePoint": {"type": "default"}},
                    {"text": "What did we decide?"}]}], \
                f"{name} must carry the caller's checkpoints in place: {wire}"
        assert response["usage"] == {
            "input_tokens": 7, "output_tokens": 3, "cache_read_input_tokens": 500,
            "cache_creation_input_tokens": 40}, \
            f"the caller must receive Bedrock's cache usage: {response['usage']}"
        rows = list(LLMRequest.objects.filter(operation=case["marker"]).order_by("pk"))
        assert [(row.status, row.input_tokens, row.output_tokens,
                 row.cache_read_input_tokens, row.cache_creation_input_tokens)
                for row in rows] == [("succeeded", 7, 3, 500, 40),
                                     ("succeeded", 7, 3, 2, 1)], \
            "the ledger must settle cache reads and writes for the call and the stream"
        _assert_budget_total(case, 550 + 13)
        assert _leases_released(case), "cached calls must release both leases"


@th.django_unit_test()
def test_bad_markers_are_refused_before_any_reservation(opts):
    from mojo.apps.account.models import LLMRequest

    def marked(count, marker=None):
        return [{"role": "user", "content": [
            {"type": "text", "text": f"part {n}",
             "cache_control": marker or {"type": "ephemeral"}} for n in range(count)]}]

    with _claude_case(_claude_policy(("text", "prompt_cache"))) as cached:
        client = _Client()
        for messages in (marked(5), marked(1, {"type": "ephemeral", "ttl": "10m"}),
                         marked(1, {"type": "persistent"})):
            for stream in (False, True):
                _assert_denied("provider_rejected", lambda: _guarded(
                    cached, client, stream=stream, messages=messages))
        assert client.calls == [] and not LLMRequest.objects.filter(
            operation=cached["marker"]).exists(), \
            "a refused marker must reach neither Bedrock nor the ledger"
        _assert_budget_total(cached, 0)
    with _claude_case() as plain:
        client = _Client()
        _assert_denied("provider_rejected", lambda: _guarded(
            plain, client, stream=False, messages=marked(1)))
        assert client.calls == [] and not LLMRequest.objects.filter(
            operation=plain["marker"]).exists(), \
            "a route without prompt_cache must refuse a marker exactly as before"
