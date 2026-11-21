"""Prompt-cache checkpoints for Claude on Bedrock (#11820).

Fake clients only: nothing here patches, touches settings or reaches AWS.
"""

import copy
import json

from testit import helpers as th


SONNET = "us.anthropic.claude-sonnet-5-5"
NOVA = "us.amazon.nova-2-lite-v1:0"
FIVE = {"type": "ephemeral"}
HOUR = {"type": "ephemeral", "ttl": "1h"}
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
REPLY_USAGE = {"inputTokens": 7, "outputTokens": 3, "cacheReadInputTokens": 500,
               "cacheWriteInputTokens": 40, "totalTokens": 550,
               "cacheDetails": [{"ttl": "1h", "inputTokens": 40}]}
USAGE = {"input_tokens": 7, "output_tokens": 3, "cache_read_input_tokens": 500,
         "cache_creation_input_tokens": 40}


def _golden(model):
    """The Converse request django-mojo 1.42.9 sent for SYSTEM/MESSAGES/TOOLS at
    effort low, captured from that code. Key order is part of the golden."""
    extra = {"thinking": {"type": "adaptive"}, "output_config": {"effort": "low"}} \
        if model == SONNET else \
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
    return {"ResponseMetadata": {"RequestId": "bedrock-cache-1"},
            "output": {"message": {"role": "assistant", "content": [{"text": "decided"}]}},
            "stopReason": "end_turn", "usage": dict(REPLY_USAGE)}


class _EventStream:
    def __init__(self, events):
        self.events = list(events)

    def __iter__(self):
        return iter(self.events)

    def close(self):
        pass


def _events():
    return [{"messageStart": {"role": "assistant"}},
            {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "decided"}}},
            {"contentBlockStop": {"contentBlockIndex": 0}},
            {"messageStop": {"stopReason": "end_turn"}},
            {"metadata": {"usage": dict(REPLY_USAGE), "metrics": {"latencyMs": 4}}}]


class _Client:
    def __init__(self):
        self.calls = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        return _reply()

    def converse_stream(self, **kwargs):
        self.calls.append(kwargs)
        return {"ResponseMetadata": {"RequestId": "bedrock-cache-stream"},
                "stream": _EventStream(_events())}


def _text(text, marker=None):
    block = {"type": "text", "text": text}
    if marker is not None:
        block["cache_control"] = marker
    return block


def _point(ttl=None):
    point = {"type": "default"}
    if ttl is not None:
        point["ttl"] = ttl
    return {"cachePoint": point}


def _both(messages, model=SONNET, cache_enabled=True, **kwargs):
    """Send one request through call() and stream(); return both wire requests."""
    from mojo.helpers.llm_providers.bedrock import BedrockProvider

    client = _Client()
    adapter = BedrockProvider(client=client)
    adapter.check_request(messages, model, 64, cache_enabled=cache_enabled, **kwargs)
    adapter.call(messages, model, 64, cache_enabled=cache_enabled, **kwargs)
    list(adapter.stream(messages, model, 64, cache_enabled=cache_enabled, **kwargs))
    assert len(client.calls) == 2, f"call and stream must each send one request: {client.calls}"
    return client.calls


def _refused(messages, model=SONNET, cache_enabled=True, **kwargs):
    """call(), stream() and check_request() all refuse, and nothing is sent."""
    from mojo.helpers.llm_providers.base import ProviderError
    from mojo.helpers.llm_providers.bedrock import BedrockProvider

    client = _Client()
    adapter = BedrockProvider(client=client)
    for run in (adapter.check_request, adapter.call, adapter.stream):
        try:
            run(messages, model, 64, cache_enabled=cache_enabled, **kwargs)
            assert False, f"{run.__name__} accepted {messages!r} / {kwargs!r}"
        except ProviderError as err:
            assert err.code == "provider_rejected", \
                f"{run.__name__} must refuse with provider_rejected, got {err.code}"
    assert client.calls == [], f"a refused cache request reached Bedrock: {client.calls}"


def _route(model, capabilities):
    limits = {
        "requests_minute": 2, "requests_hour": 10, "requests_day": 20,
        "tokens_minute": 1000, "tokens_hour": 5000, "tokens_day": 10000,
        "concurrency": 1, "max_input_bytes": 4096, "max_output_tokens": 128,
        "timeout_seconds": 10, "max_loop_calls": 2,
    }
    return {
        "version": 1,
        "routes": {"unattributed": {
            "provider": "bedrock", "model": model, "credential": "aws",
            "capabilities": list(capabilities), "region": "us-east-1",
            "reasoning_efforts": ["off", "low", "medium"]}},
        "shared": dict(limits),
        "features": {"unattributed": dict(limits)},
        "breaker": {"auth_failures": 2, "rate_failures": 2,
                    "server_failures": 2, "open_seconds": 60},
    }


@th.django_unit_test()
def test_policy_admits_prompt_cache_on_bedrock_claude_routes_only(opts):
    from mojo.apps.account.services import llm_safety

    bases = (["text"], ["text", "tools"], ["text", "images"], ["text", "tools", "images"])
    for model in ("us.anthropic.claude-haiku-5-5", SONNET, "global.anthropic.claude-opus-5-5"):
        for base in bases:
            for capabilities in (base + ["prompt_cache"], ["prompt_cache"] + base):
                raw = _route(model, capabilities)
                parsed = llm_safety.parse_policy(raw)
                assert parsed["routes"]["unattributed"]["capabilities"] == capabilities, \
                    f"a Claude route declaring {capabilities} must parse unchanged"
                assert parsed["hash"] == llm_safety._canonical_hash(raw) \
                    != llm_safety.parse_policy(_route(model, base))["hash"], \
                    "adding prompt_cache is an ordinary policy change under the same hash rule"
    refused = [(NOVA, base + ["prompt_cache"]) for base in bases]
    refused += [(SONNET, ["prompt_cache"]), (SONNET, ["tools", "prompt_cache"]),
                (SONNET, ["text", "prompt_cache", "prompt_cache"]),
                (SONNET, ["text", "prompt_cache", "models"]),
                (SONNET, ["text", "prompt_cache", "stream"])]
    for model, capabilities in refused:
        try:
            llm_safety.parse_policy(_route(model, capabilities))
            assert False, f"Bedrock accepted {capabilities} on {model}"
        except llm_safety.LLMSafetyError as err:
            assert err.code == "policy_invalid", \
                f"{capabilities} on {model} must be policy_invalid, got {err.code}"
    anthropic = _route(SONNET, ["text"])
    anthropic["routes"]["unattributed"] = {
        "provider": "anthropic", "model": "claude-test", "credential": "handler",
        "capabilities": ["text", "prompt_cache"]}
    assert llm_safety.parse_policy(anthropic)["routes"] == anthropic["routes"], \
        "Anthropic routes keep declaring prompt_cache exactly as before"


@th.django_unit_test()
def test_requests_without_markers_are_byte_identical_to_1_42_9(opts):
    for model in (SONNET, NOVA):
        for cache_enabled in (False, True):
            for wire in _both(MESSAGES, model, cache_enabled, system=SYSTEM, tools=TOOLS,
                              reasoning_effort="low", timeout=10):
                assert json.dumps(wire) == json.dumps(_golden(model)), \
                    f"{model} cache_enabled={cache_enabled} changed an unmarked request: {wire}"


@th.django_unit_test()
def test_markers_become_checkpoints_where_the_caller_put_them(opts):
    system = [_text("You are on the call."), _text("The meeting rules.", HOUR)]
    messages = [
        {"role": "user", "content": [_text("The transcript.", HOUR), _text("First?")]},
        {"role": "assistant", "content": [_text("An answer.", FIVE)]},
        {"role": "user", "content": [_text("Second?", {"type": "ephemeral", "ttl": "5m"})]},
    ]
    before = copy.deepcopy((system, messages))
    for wire in _both(messages, system=system):
        assert wire["system"] == [{"text": "You are on the call."},
                                  {"text": "The meeting rules."}, _point("1h")], \
            f"a system marker must become a checkpoint right after its block: {wire['system']}"
        assert wire["messages"] == [
            {"role": "user", "content": [{"text": "The transcript."}, _point("1h"),
                                         {"text": "First?"}]},
            {"role": "assistant", "content": [{"text": "An answer."}, _point()]},
            {"role": "user", "content": [{"text": "Second?"}, _point("5m")]}], \
            f"message markers must become checkpoints in place: {wire['messages']}"
        assert list(wire) == ["modelId", "messages", "inferenceConfig", "system",
                              "additionalModelRequestFields"], \
            f"checkpoints must add no request field: {list(wire)}"
        assert "cache_control" not in json.dumps(wire), \
            f"the caller's marker itself must never reach Bedrock: {wire}"
    assert (system, messages) == before, "translating markers must not change the caller's input"

    transcript = MESSAGES[:2] + [{"role": "user", "content": [
        MESSAGES[2]["content"][0], _text("What did we decide?", HOUR)]}]
    for wire in _both(transcript, tools=TOOLS):
        assert wire["messages"][2]["content"][-2:] == [
            {"text": "What did we decide?"}, _point("1h")], \
            f"a marker after a tool result must become a checkpoint: {wire['messages'][2]}"


@th.django_unit_test()
def test_ttl_is_the_callers_and_a_longer_ttl_never_follows_a_shorter_one(opts):
    for marker, point in ((FIVE, _point()), ({"type": "ephemeral", "ttl": "5m"}, _point("5m")),
                          (HOUR, _point("1h"))):
        for wire in _both([{"role": "user", "content": [_text("Context", marker)]}]):
            assert wire["messages"][0]["content"] == [{"text": "Context"}, point], \
                f"marker {marker} must send checkpoint {point}: {wire['messages']}"
    ordered = [{"role": "user", "content": [
        _text("a", HOUR), _text("b", HOUR), _text("c", {"type": "ephemeral", "ttl": "5m"}),
        _text("d", FIVE)]}]
    _both(ordered)
    _both([{"role": "user", "content": [_text("a", FIVE)]}], system=[_text("s", HOUR)])
    for system, content in (
            (None, [_text("a", FIVE), _text("b", HOUR)]),
            (None, [_text("a", {"type": "ephemeral", "ttl": "5m"}), _text("b", HOUR)]),
            ([_text("s", FIVE)], [_text("a", HOUR)])):
        _refused([{"role": "user", "content": content}], system=system)


@th.django_unit_test()
def test_at_most_four_checkpoints_per_request(opts):
    four = [{"role": "user", "content": [_text(f"part {n}", FIVE) for n in range(3)]}]
    wires = _both(four, system=[_text("s", FIVE)])
    assert sum("cachePoint" in block for block in wires[0]["system"] +
               wires[0]["messages"][0]["content"]) == 4, \
        f"four checkpoints are the limit and must all be sent: {wires[0]}"
    _refused(four, system=[_text("s", FIVE), _text("t", FIVE)])
    _refused([{"role": "user", "content": [_text(f"part {n}", FIVE) for n in range(5)]}])
    _refused([{"role": "user", "content": [_text("a", FIVE), _text("b", FIVE)]},
              {"role": "assistant", "content": [_text("c", FIVE)]},
              {"role": "user", "content": [_text("d", FIVE), _text("e", FIVE)]}])


@th.django_unit_test()
def test_every_other_marker_or_cache_block_is_refused(opts):
    from mojo.helpers.llm_images import image_block

    def user(*blocks):
        return [{"role": "user", "content": list(blocks)}]

    for marker in ({"type": "persistent"}, {}, "ephemeral", None, [], {"type": "default"},
                   {"type": "ephemeral", "ttl": "10m"}, {"type": "ephemeral", "ttl": 3600},
                   {"type": "ephemeral", "ttl": None}, {"type": "ephemeral", "ttl": ["1h"]},
                   {"type": "ephemeral", "scope": "global"}):
        _refused(user({"type": "text", "text": "Context", "cache_control": marker}))
        _refused(user(_text("Hi")), system=[
            {"type": "text", "text": "Rules", "cache_control": marker}])
    _refused(user(_text(" ", FIVE)))
    _refused([{"role": "user", "content": "Hi", "cache_control": FIVE}])
    _refused(user(_text("Hi"), {"cachePoint": {"type": "default"}}))
    _refused(user(_text("Hi"), {"type": "cache_point"}))
    _refused(user(_text("Hi")), system=[_text("Rules"), {"cachePoint": {"type": "default"}}])
    _refused(user(_text("Hi")), tools=[dict(TOOLS[0], cache_control=FIVE)])

    import io
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (4, 3), (10, 120, 200)).save(out, format="PNG")
    _refused(user(dict(image_block(out.getvalue(), "image/png"), cache_control=FIVE),
                  _text("Describe it.")))
    use = {"type": "tool_use", "id": "tooluse_1", "name": "lookup_item", "input": {"id": 7}}
    answered = {"type": "tool_result", "tool_use_id": "tooluse_1", "content": "open"}
    asked = [{"role": "user", "content": "Hi"}]
    _refused(asked + [{"role": "assistant", "content": [dict(use, cache_control=FIVE)]},
                      {"role": "user", "content": [answered]}], tools=TOOLS)
    _refused(asked + [{"role": "assistant", "content": [use]},
                      {"role": "user", "content": [dict(answered, cache_control=FIVE)]}],
             tools=TOOLS)
    _refused(asked + [{"role": "assistant", "content": [use]},
                      {"role": "user", "content": [dict(answered, content=[
                          _text("open", FIVE)])]}], tools=TOOLS)

    marked = user(_text("Context", FIVE))
    _refused(marked, model=NOVA)
    for cache_enabled in (False, None, 1, "yes"):
        _refused(marked, cache_enabled=cache_enabled)


@th.django_unit_test()
def test_cache_usage_reaches_the_caller(opts):
    from mojo.helpers.llm_providers.bedrock import BedrockProvider

    messages = [{"role": "user", "content": [_text("The transcript.", HOUR), _text("Q?")]}]
    adapter = BedrockProvider(client=_Client())
    called = adapter.call(messages, SONNET, 64, cache_enabled=True)
    events = list(adapter.stream(messages, SONNET, 64, cache_enabled=True))
    streamed = events[-1]["response"]
    for result in (called, streamed):
        assert result["usage"] == USAGE, \
            f"Bedrock's cache read and write counts must reach the caller: {result['usage']}"
    assert adapter.supports("prompt_cache"), "the Bedrock adapter must advertise prompt_cache"
