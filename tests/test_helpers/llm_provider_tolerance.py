"""Provider answers carrying fields the adapters do not read still validate (#11908).

On 2026-11-23 Bedrock's ConverseStream began sending ``"type": "tool_use"`` in
a tool call's start, and an exact key check threw every such answer away as
provider_rejected. The adapters now read the fields they need, check those,
and skip everything else without forwarding it; our own requests drop keys
they do not know instead of refusing. The safety checks still refuse what
they must, and say which check failed.

Fake clients only: nothing here patches, touches settings or reaches a provider.
"""

from testit import helpers as th


SONNET = "us.anthropic.claude-sonnet-5-5"
NOVA = "us.amazon.nova-2-lite-v1:0"
MESSAGES = [{"role": "user", "content": "What is on the queue?"}]
QUEUE_TOOLS = [{"name": "workspace_queue", "description": "The workspace queue",
                "input_schema": {"type": "object",
                                 "properties": {"slim": {"type": "string"}}}}]
TOOL_ID = "tooluse_sCBDdAcyMFuucZJeOfFOwY"
PRODUCTION_USAGE = {"input_tokens": 38, "output_tokens": 57,
                    "cache_read_input_tokens": 12304, "cache_creation_input_tokens": 0}
PRODUCTION_CONTENT = [
    {"type": "text", "text": "Let me check."},
    {"type": "tool_use", "id": TOOL_ID, "name": "workspace_queue",
     "input": {"slim": "true"}}]
# Every made-up field below carries this word; it must never reach a caller.
FUTURE = "FUTURE-FIELD"


def _production_events():
    """The exact ConverseStream seen on production (boto3 1.42.88)."""
    return [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockDelta": {"delta": {"text": "Let me check."}, "contentBlockIndex": 0}},
        {"contentBlockStop": {"contentBlockIndex": 0}},
        {"contentBlockStart": {"start": {"toolUse": {
            "toolUseId": TOOL_ID, "name": "workspace_queue", "type": "tool_use"}},
            "contentBlockIndex": 1}},
        {"contentBlockDelta": {"delta": {"toolUse": {"input": ""}}, "contentBlockIndex": 1}},
        {"contentBlockDelta": {"delta": {"toolUse": {"input": '{"slim"'}},
                               "contentBlockIndex": 1}},
        {"contentBlockDelta": {"delta": {"toolUse": {"input": ': "true'}},
                               "contentBlockIndex": 1}},
        {"contentBlockDelta": {"delta": {"toolUse": {"input": '"}'}}, "contentBlockIndex": 1}},
        {"contentBlockStop": {"contentBlockIndex": 1}},
        {"messageStop": {"stopReason": "tool_use"}},
        {"metadata": {"usage": {"inputTokens": 38, "outputTokens": 57, "totalTokens": 12399,
                                "cacheReadInputTokens": 12304, "cacheWriteInputTokens": 0},
                      "metrics": {"latencyMs": 943}}},
    ]


def _production_response():
    """The same answer as Converse returns it unstreamed, with the new ``type``."""
    return {"ResponseMetadata": {"RequestId": "bedrock-11908"},
            "output": {"message": {"role": "assistant", "content": [
                {"text": "Let me check."},
                {"toolUse": {"toolUseId": TOOL_ID, "name": "workspace_queue",
                             "input": {"slim": "true"}, "type": "tool_use"}}]}},
            "stopReason": "tool_use",
            "usage": {"inputTokens": 38, "outputTokens": 57, "totalTokens": 12399,
                      "cacheReadInputTokens": 12304, "cacheWriteInputTokens": 0},
            "metrics": {"latencyMs": 943}}


class _Events:
    def __init__(self, events):
        self.events = list(events)
        self.closed = False

    def __iter__(self):
        return iter(self.events)

    def close(self):
        self.closed = True


class _Client:
    """A bedrock-runtime stand-in: one canned answer, streamed or not."""

    def __init__(self, response=None, events=None):
        self.response = response
        self.events = events
        self.calls = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        return self.response

    def converse_stream(self, **kwargs):
        self.calls.append(kwargs)
        return {"ResponseMetadata": {"RequestId": "bedrock-11908"},
                "stream": _Events(self.events)}


def _bedrock(response=None, events=None):
    from mojo.helpers.llm_providers.bedrock import BedrockProvider
    return BedrockProvider(client=_Client(response=response, events=events))


def _rejected(run):
    from mojo.helpers.llm_providers.base import ProviderError
    try:
        result = run()
        if not isinstance(result, dict):
            list(result)
        assert False, "the answer should have been refused"
    except ProviderError as err:
        assert err.code == "provider_rejected", f"a refusal must be provider_rejected, got {err.code}"
        return err


@th.tier("bug")
@th.django_unit_test()
def test_production_stream_with_a_typed_tool_start_is_a_valid_answer(opts):
    """Regression #11908: the 2026-11-23 stream is one text block and one tool request."""
    adapter = _bedrock(events=_production_events())
    events = list(adapter.stream(MESSAGES, SONNET, 4096, tools=QUEUE_TOOLS))
    assert [event["type"] for event in events] == ["text_delta", "final"] \
        and events[0]["text"] == "Let me check.", \
        f"the text must stream, then one final record: {events}"
    final = events[-1]["response"]
    assert final["content"] == PRODUCTION_CONTENT and final["stop_reason"] == "tool_use", \
        f"the typed tool start must assemble into the tool request: {final}"
    assert final["usage"] == PRODUCTION_USAGE, \
        f"usage must keep the four counters and drop totalTokens: {final['usage']}"

    answer = _bedrock(response=_production_response()).call(
        MESSAGES, SONNET, 4096, tools=QUEUE_TOOLS)
    assert answer["content"] == PRODUCTION_CONTENT and answer["stop_reason"] == "tool_use" \
        and answer["usage"] == PRODUCTION_USAGE, \
        f"the unstreamed answer with toolUse.type must validate the same way: {answer}"


def _future_events():
    """A stream with made-up fields at every level, kinds this adapter has
    never seen, and a tool the provider runs itself."""
    usage = {"inputTokens": 38, "outputTokens": 57, "totalTokens": 12399,
             "cacheReadInputTokens": 12304, "cacheWriteInputTokens": 0,
             "cacheDetails": [{"ttl": "5m", FUTURE: 1}], "futureTokens": 9}
    return [
        {"messageStart": {"role": "assistant", "future": FUTURE},
         "futureEnvelope": {"note": FUTURE}},
        {"futureEvent": {"note": FUTURE}},
        {"futureEvent": FUTURE},
        # An unknown start kind: the text deltas that follow define the block.
        {"contentBlockStart": {"start": {"futureText": {"note": FUTURE}},
                               "contentBlockIndex": 0}},
        {"contentBlockDelta": {"delta": {"text": "Let me ", "future": FUTURE},
                               "contentBlockIndex": 0, "future": FUTURE}},
        {"contentBlockDelta": {"delta": {"citation": {"title": FUTURE}},
                               "contentBlockIndex": 0}},
        {"contentBlockDelta": {"delta": {"text": "check."}, "contentBlockIndex": 0}},
        {"contentBlockStop": {"contentBlockIndex": 0, "future": FUTURE}},
        # Reasoning in a shape this adapter never reads.
        {"contentBlockDelta": {"delta": {"reasoningContent": FUTURE},
                               "contentBlockIndex": 1}},
        {"contentBlockStop": {"contentBlockIndex": 1}},
        # A block kind this adapter does not know, start and deltas.
        {"contentBlockStart": {"start": {"image": {"format": "png", "note": FUTURE}},
                               "contentBlockIndex": 2}},
        {"contentBlockDelta": {"delta": {"image": {"bytes": FUTURE}},
                               "contentBlockIndex": 2}},
        {"contentBlockStop": {"contentBlockIndex": 2}},
        # A tool the provider runs itself is never the caller's request.
        {"contentBlockStart": {"start": {"toolUse": {
            "toolUseId": "srvtoolu_1", "name": FUTURE, "type": "server_tool_use"}},
            "contentBlockIndex": 3}},
        {"contentBlockDelta": {"delta": {"toolUse": {"input": '{"q": "' + FUTURE + '"}'}},
                               "contentBlockIndex": 3}},
        {"contentBlockStop": {"contentBlockIndex": 3}},
        {"contentBlockStart": {"start": {"toolUse": {
            "toolUseId": TOOL_ID, "name": "workspace_queue", "type": "tool_use",
            "caller": {"note": FUTURE}}, "future": FUTURE}, "contentBlockIndex": 4,
            "future": FUTURE}},
        {"contentBlockDelta": {"delta": {"toolUse": {"input": '{"slim": "true"}',
                                                     "future": FUTURE}},
                               "contentBlockIndex": 4}},
        {"contentBlockStop": {"contentBlockIndex": 4}},
        {"messageStop": {"stopReason": "tool_use",
                         "additionalModelResponseFields": {"note": FUTURE}}},
        {"metadata": {"trace": {"note": FUTURE}}},
        {"metadata": {"usage": usage, "metrics": {"latencyMs": 943, "future": FUTURE},
                      "trace": {"note": FUTURE}, "performanceConfig": {"latency": FUTURE}}},
        {"futureTrailer": {"note": FUTURE}},
    ]


def _future_response():
    usage = {"inputTokens": 38, "outputTokens": 57, "totalTokens": 12399,
             "cacheReadInputTokens": 12304, "cacheWriteInputTokens": 0,
             "cacheDetails": [{"ttl": "5m"}], "futureTokens": FUTURE}
    return {
        "ResponseMetadata": {"RequestId": "bedrock-11908", "future": FUTURE},
        "output": {"message": {"role": "assistant", "future": FUTURE, "content": [
            {"reasoningContent": {"reasoningText": {}}},
            {"reasoningContent": {"reasoningText": {"text": FUTURE, "future": FUTURE}}},
            {"reasoningContent": {"reasoningText": {"text": FUTURE, "signature": 4}}},
            {"reasoningContent": {"redactedContent": b"hidden", "future": FUTURE}},
            {"redactedContent": FUTURE},
            {"text": "Let me check.", "future": FUTURE},
            {"citationsContent": {"content": [{"text": FUTURE}]}},
            {"image": {"format": "png", "source": {"bytes": FUTURE}}},
            {"futureBlock": {"note": FUTURE}},
            {"toolUse": {"toolUseId": "srvtoolu_1", "name": FUTURE, "input": {"q": FUTURE},
                         "type": "server_tool_use"}},
            {"toolUse": {"toolUseId": TOOL_ID, "name": "workspace_queue",
                         "input": {"slim": "true"}, "type": "tool_use",
                         "caller": {"note": FUTURE}}, "future": FUTURE}],
        }, "future": FUTURE},
        "stopReason": "tool_use", "usage": usage,
        "metrics": {"latencyMs": 943, "future": FUTURE},
        "additionalModelResponseFields": {"note": FUTURE},
        "trace": {"note": FUTURE}, "performanceConfig": {"latency": FUTURE},
        "future": FUTURE,
    }


@th.django_unit_test()
def test_future_fields_and_kinds_are_ignored_and_never_forwarded(opts):
    events = list(_bedrock(events=_future_events()).stream(
        MESSAGES, SONNET, 4096, tools=QUEUE_TOOLS))
    assert [event.get("text") for event in events[:-1]] == ["Let me ", "check."], \
        f"only answer text may stream, in order: {events}"
    final = events[-1]["response"]
    assert final["content"] == PRODUCTION_CONTENT and final["stop_reason"] == "tool_use" \
        and final["usage"] == PRODUCTION_USAGE, \
        f"a stream with unknown fields and kinds must still validate: {final}"
    assert FUTURE not in repr(events), f"an unknown provider field crossed the adapter: {events}"

    answer = _bedrock(response=_future_response()).call(
        MESSAGES, SONNET, 4096, tools=QUEUE_TOOLS)
    assert answer["content"] == PRODUCTION_CONTENT and answer["stop_reason"] == "tool_use" \
        and answer["usage"] == PRODUCTION_USAGE, \
        f"an answer with unknown fields and block kinds must still validate: {answer}"
    assert set(answer) == {"id", "type", "role", "provider", "model", "content",
                           "stop_reason", "usage"} and FUTURE not in repr(answer), \
        f"an unknown provider field crossed the adapter: {answer}"


@th.django_unit_test()
def test_a_provider_run_tool_alone_is_not_a_tool_request(opts):
    events = [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockStart": {"start": {"toolUse": {
            "toolUseId": "srvtoolu_1", "name": "workspace_queue",
            "type": "server_tool_use"}}, "contentBlockIndex": 0}},
        {"contentBlockDelta": {"delta": {"toolUse": {"input": "{}"}}, "contentBlockIndex": 0}},
        {"contentBlockStop": {"contentBlockIndex": 0}},
        {"messageStop": {"stopReason": "tool_use"}},
        {"metadata": {"usage": {"inputTokens": 1, "outputTokens": 1}}},
    ]
    err = _rejected(lambda: _bedrock(events=events).stream(
        MESSAGES, SONNET, 64, tools=QUEUE_TOOLS))
    assert err.detail == "a tool_use stop without a tool request", \
        f"a tool the provider runs is skipped, so the stop has no request: {err.detail!r}"


@th.django_unit_test()
def test_a_skipped_block_hides_its_later_deltas_and_proves_nothing(opts):
    """A provider-run tool's block stays skipped whatever deltas arrive for it,
    and its result block (an unknown start) forwards nothing either."""
    usage = {"metadata": {"usage": {"inputTokens": 1, "outputTokens": 1}}}
    server = {"contentBlockStart": {"start": {"toolUse": {
        "toolUseId": "srvtoolu_1", "name": "workspace_queue", "type": "server_tool_use"}},
        "contentBlockIndex": 1}}
    events = [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockDelta": {"delta": {"text": "Here it is."}, "contentBlockIndex": 0}},
        {"contentBlockStop": {"contentBlockIndex": 0}},
        server,
        {"contentBlockDelta": {"delta": {"text": FUTURE}, "contentBlockIndex": 1}},
        {"contentBlockDelta": {"delta": {"toolUse": {"input": "not json"}},
                               "contentBlockIndex": 1}},
        {"contentBlockDelta": {"delta": {"reasoningContent": {"text": FUTURE}},
                               "contentBlockIndex": 1}},
        {"contentBlockStop": {"contentBlockIndex": 1}},
        {"contentBlockDelta": {"delta": {"text": FUTURE}, "contentBlockIndex": 1}},
        {"contentBlockStart": {"start": {"toolResult": {
            "toolUseId": "srvtoolu_1", "status": "success"}}, "contentBlockIndex": 2}},
        {"contentBlockDelta": {"delta": {"toolResult": [{"text": FUTURE}]},
                               "contentBlockIndex": 2}},
        {"contentBlockStop": {"contentBlockIndex": 2}},
        {"messageStop": {"stopReason": "end_turn"}},
        usage,
    ]
    streamed = list(_bedrock(events=events).stream(MESSAGES, SONNET, 64, tools=QUEUE_TOOLS))
    assert FUTURE not in repr(streamed) and streamed[-1]["response"]["content"] == [
        {"type": "text", "text": "Here it is."}], \
        f"nothing of a provider-run tool may reach the caller: {streamed}"

    # Alone, a skipped block is no answer: the stop reason still needs content.
    for stop, detail in (("end_turn", "end_turn without answer text"),
                         ("tool_use", "a tool_use stop without a tool request")):
        bare = [events[0], server, {"contentBlockStop": {"contentBlockIndex": 1}},
                {"messageStop": {"stopReason": stop}}, usage]
        err = _rejected(lambda: _bedrock(events=bare).stream(
            MESSAGES, SONNET, 64, tools=QUEUE_TOOLS))
        assert err.detail == detail, f"a skipped block must not satisfy {stop}: {err.detail!r}"

    answer = _bedrock(response=_tool_answer([
        {"text": "Here it is."},
        {"toolUse": {"toolUseId": "srvtoolu_1", "name": "workspace_queue",
                     "input": {"q": FUTURE}, "type": "server_tool_use"}},
        {"toolResult": {"toolUseId": "srvtoolu_1", "content": [{"text": FUTURE}]}}],
        stop="end_turn")).call(MESSAGES, SONNET, 64, tools=QUEUE_TOOLS)
    assert answer["content"] == [{"type": "text", "text": "Here it is."}] \
        and FUTURE not in repr(answer), \
        f"an unstreamed provider-run tool and its result must be skipped: {answer}"


@th.django_unit_test()
def test_unclassified_sdk_failures_name_only_their_class(opts):
    from mojo.helpers.llm_providers.anthropic import AnthropicProvider
    from mojo.helpers.llm_providers.base import ProviderError
    from mojo.helpers.llm_providers.bedrock import BedrockProvider

    class _Failing:
        def converse(self, **kwargs):
            raise RuntimeError(f"secret {FUTURE}")

    class _FailingMessages:
        def create(self, **kwargs):
            raise RuntimeError(f"secret {FUTURE}")

    class _FailingAnthropic:
        messages = _FailingMessages()

    for run in (lambda: BedrockProvider(client=_Failing()).call(MESSAGES, NOVA, 64),
                lambda: AnthropicProvider(client=_FailingAnthropic()).call(
                    MESSAGES, "claude-sonnet-5-5", 64)):
        try:
            run()
            assert False, "an SDK failure must raise a ProviderError"
        except ProviderError as err:
            assert err.code == "provider_failed" and err.detail == "unexpected RuntimeError", \
                f"an unclassified failure keeps its class only, got {err.code}/{err.detail!r}"


@th.django_unit_test()
def test_cache_control_inside_a_tool_result_is_refused_by_name(opts):
    from mojo.helpers.llm_providers.bedrock import BedrockProvider

    use = {"type": "tool_use", "id": "tooluse_1", "name": "workspace_queue", "input": {}}
    messages = [{"role": "user", "content": "Hi"},
                {"role": "assistant", "content": [use]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tooluse_1",
                                              "content": [{"type": "text", "text": "two",
                                                           "cache_control": {"type": "ephemeral"}}]}]}]
    client = _Client(response=_production_response())
    err = _rejected(lambda: BedrockProvider(client=client).check_request(
        messages, SONNET, 64, tools=QUEUE_TOOLS, cache_enabled=True) or {})
    assert err.detail == "cache_control is not allowed on a tool_result's content" \
        and client.calls == [], \
        f"a marker Converse cannot place must be refused by name, even on a caching route: {err.detail!r}"


@th.django_unit_test()
def test_unknown_keys_in_our_requests_are_dropped_not_sent(opts):
    from mojo.helpers.llm_providers.bedrock import BedrockProvider

    client = _Client(response=_production_response())
    tools = [dict(QUEUE_TOOLS[0], type="custom", strict=False, future=FUTURE)]
    use = {"type": "tool_use", "id": "tooluse_1", "name": "workspace_queue",
           "input": {"slim": "true"}, "caller": None, "future": FUTURE}
    messages = [
        {"role": "user", "id": FUTURE, "content": [
            {"type": "text", "text": "What is on the queue?", "citations": None,
             "future": FUTURE}]},
        {"role": "assistant", "future": FUTURE, "content": [
            {"type": "text", "text": "Checking.", "future": FUTURE}, use]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tooluse_1", "is_error": False,
             "future": FUTURE, "content": [
                 {"type": "text", "text": "two items", "future": FUTURE}]}]},
    ]
    system = [{"type": "text", "text": "Be brief.", "future": FUTURE}]
    BedrockProvider(client=client).call(messages, NOVA, 4096, system=system, tools=tools)
    assert client.calls == [{
        "modelId": NOVA,
        "messages": [
            {"role": "user", "content": [{"text": "What is on the queue?"}]},
            {"role": "assistant", "content": [
                {"text": "Checking."},
                {"toolUse": {"toolUseId": "tooluse_1", "name": "workspace_queue",
                             "input": {"slim": "true"}}}]},
            {"role": "user", "content": [{"toolResult": {
                "toolUseId": "tooluse_1", "content": [{"text": "two items"}]}}]}],
        "inferenceConfig": {"maxTokens": 4096},
        "system": [{"text": "Be brief."}],
        "toolConfig": {"tools": [{"toolSpec": {
            "name": "workspace_queue", "description": "The workspace queue",
            "inputSchema": {"json": QUEUE_TOOLS[0]["input_schema"]}}}]}}], \
        f"unknown keys must be dropped from the built request: {client.calls}"
    assert FUTURE not in repr(client.calls), "an unknown request key reached Converse"

    # Keys that change what a request means or costs are still refused.
    for kwargs, detail in (
            ({"tools": [dict(QUEUE_TOOLS[0], type="web_search_20250305")]},
             "only custom tools can be declared"),
            ({"tools": [dict(QUEUE_TOOLS[0], cache_control={"type": "ephemeral"})]},
             "cache_control is not allowed on a tool spec"),
            # Dropping it would silently lose the promise that input matches
            # the schema.
            ({"tools": [dict(QUEUE_TOOLS[0], strict=True)]},
             "strict tool schemas are not supported"),
            ({"messages": [{"role": "user", "content": [
                {"type": "text", "text": "Hi", "cache_control": {"type": "ephemeral"}}]}]},
             "cache_control is not allowed where caching is off")):
        refused = _Client(response=_production_response())
        values = {"messages": MESSAGES, "tools": QUEUE_TOOLS}
        values.update(kwargs)
        err = _rejected(lambda: BedrockProvider(client=refused).call(
            values["messages"], NOVA, 64, tools=values["tools"]))
        assert err.detail == detail and refused.calls == [], \
            f"a meaning-changing key must be refused before dispatch, got {err.detail!r}"


def _tool_answer(content, stop="tool_use", output_tokens=11):
    return {"ResponseMetadata": {"RequestId": "bedrock-11908"},
            "output": {"message": {"role": "assistant", "content": content}},
            "stopReason": stop,
            "usage": {"inputTokens": 7, "outputTokens": output_tokens}}


def _tool_stream(start, pieces, stop="tool_use", output_tokens=11, text=None):
    events = [{"messageStart": {"role": "assistant"}}]
    if text is not None:
        events.append({"contentBlockDelta": {"delta": {"text": text},
                                             "contentBlockIndex": 0}})
    if start is not None:
        events.append({"contentBlockStart": {"start": {"toolUse": start},
                                             "contentBlockIndex": 1}})
        events += [{"contentBlockDelta": {"delta": {"toolUse": {"input": piece}},
                                          "contentBlockIndex": 1}} for piece in pieces]
        events.append({"contentBlockStop": {"contentBlockIndex": 1}})
    events += [{"messageStop": {"stopReason": stop}},
               {"metadata": {"usage": {"inputTokens": 7, "outputTokens": output_tokens}}}]
    return events


@th.django_unit_test()
def test_safety_checks_still_refuse_and_say_which_check_failed(opts):
    from mojo.helpers.llm_providers.bedrock import BedrockProvider

    good = {"toolUseId": TOOL_ID, "name": "workspace_queue", "type": "tool_use"}
    streamed = (
        (_tool_stream(dict(good, name="not_declared"), ["{}"]),
         "the answer asks for a tool that was not declared"),
        (_tool_stream(dict(good, toolUseId="bad id"), ["{}"]),
         "a tool request id does not match its pattern"),
        (_tool_stream(good, ["[1, 2]"]),
         "tool input must be a JSON object (no NaN or Infinity)"),
        (_tool_stream(good, ['{"slim": NaN}']),
         "tool input must be a JSON object (no NaN or Infinity)"),
        (_tool_stream(good, ['{"slim": Infinity}']),
         "tool input must be a JSON object (no NaN or Infinity)"),
        (_tool_stream(good, ["{}"], output_tokens=65),
         "output tokens exceed max_tokens"),
        (_tool_stream(good, ["{}"], stop="end_turn", text="Done."),
         "a tool request with stop reason end_turn"),
        (_tool_stream(None, [], stop="content_filtered", text="Done."),
         "unexpected stop reason content_filtered"),
        (_tool_stream(None, [], stop="end_turn"), "end_turn without answer text"),
    )
    for events, detail in streamed:
        err = _rejected(lambda: _bedrock(events=events).stream(
            MESSAGES, SONNET, 64, tools=QUEUE_TOOLS))
        assert err.detail == detail, f"streamed: expected {detail!r}, got {err.detail!r}"
        assert err.request_id == "bedrock-11908", \
            f"a refused streamed answer must keep its request id ({detail})"

    good = {"toolUseId": TOOL_ID, "name": "workspace_queue", "input": {}, "type": "tool_use"}
    answered = (
        (_tool_answer([{"toolUse": dict(good, name="not_declared")}]),
         "the answer asks for a tool that was not declared"),
        (_tool_answer([{"toolUse": dict(good, input="{}")}]), "tool input must be a JSON object"),
        (_tool_answer([{"toolUse": dict(good, input={"slim": float("nan")})}]),
         "tool input must be a JSON object"),
        (_tool_answer([{"toolUse": good}], output_tokens=65), "output tokens exceed max_tokens"),
        (_tool_answer([{"text": "Done."}], stop="content_filtered"),
         "unexpected stop reason content_filtered"),
        (_tool_answer([{"text": "Done."}], stop="tool_use"),
         "a tool_use stop without a tool request"),
        (_tool_answer([{"text": "Done.", "toolUse": good}]),
         "a content block is both text and a tool request"),
    )
    for response, detail in answered:
        err = _rejected(lambda: _bedrock(response=response).call(
            MESSAGES, SONNET, 64, tools=QUEUE_TOOLS))
        assert err.detail == detail, f"converse: expected {detail!r}, got {err.detail!r}"
        assert err.usage is not None, f"a refused answer must keep its usage ({detail})"

    use = {"type": "tool_use", "id": "tooluse_1", "name": "workspace_queue", "input": {}}
    asked = [{"role": "user", "content": "Hi"}, {"role": "assistant", "content": [use]}]
    requests = (
        ({"messages": asked}, "the last tool request is not answered"),
        ({"messages": asked + [{"role": "user", "content": "no answer"}]},
         "tool results must answer exactly the previous tool requests"),
        ({"messages": [{"role": "user", "content": "Hi"}, {"role": "assistant", "content": [
            dict(use, name="not_declared")]}]},
         "a tool_use names a tool that is not declared"),
        ({"tools": [dict(QUEUE_TOOLS[0], name="")]},
         "a tool name must be 1-64 letters, digits, _ or -"),
        ({"tools": [dict(QUEUE_TOOLS[0], input_schema={"type": "array"})]},
         "a tool input_schema must be an object schema"),
    )
    for kwargs, detail in requests:
        client = _Client(response=_production_response())
        values = {"messages": MESSAGES, "tools": QUEUE_TOOLS}
        values.update(kwargs)
        err = _rejected(lambda: BedrockProvider(client=client).check_request(
            values["messages"], SONNET, 64, tools=values["tools"]) or {})
        assert err.detail == detail and client.calls == [], \
            f"request: expected {detail!r} before dispatch, got {err.detail!r}"
        assert "Hi" not in err.detail, "a refusal detail must never carry request content"


class _AnthropicMessages:
    def __init__(self, dump):
        self.dump = dump
        self.created = []
        self.counted = []

    def create(self, **kwargs):
        self.created.append(kwargs)
        dump = self.dump

        class Response:
            def model_dump(self):
                return dump
        return Response()

    def count_tokens(self, **kwargs):
        self.counted.append(kwargs)

        class Count:
            input_tokens = 12
        return Count()


class _AnthropicClient:
    def __init__(self, dump):
        self.messages = _AnthropicMessages(dump)


def _anthropic_dump():
    """An Anthropic model_dump with fields and block kinds the adapter never reads."""
    return {
        "id": "msg_11908", "type": "message", "role": "assistant",
        "model": "claude-sonnet-5-5", "container": None, "future": FUTURE,
        "content": [
            {"type": "thinking", "thinking": "Look at the queue.", "signature": "sig-1",
             "future": FUTURE},
            {"type": "text", "text": "Let me check.", "citations": None, "future": FUTURE},
            {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search",
             "input": {"q": FUTURE}},
            {"type": "future_block", "note": FUTURE},
            {"type": "tool_use", "id": TOOL_ID, "name": "workspace_queue",
             "input": {"slim": "true"}, "caller": {"type": FUTURE}},
        ],
        "stop_reason": "tool_use", "stop_sequence": None,
        "usage": {"input_tokens": 38, "output_tokens": 57,
                  "cache_read_input_tokens": 12304, "cache_creation_input_tokens": None,
                  "cache_creation": {"ephemeral_5m_input_tokens": 0, "future": FUTURE},
                  "server_tool_use": {"web_search_requests": 1},
                  "service_tier": FUTURE, "future_tokens": 4},
    }


@th.django_unit_test()
def test_anthropic_answers_keep_known_fields_and_drop_the_rest(opts):
    from mojo.helpers.llm_providers.anthropic import AnthropicProvider

    client = _AnthropicClient(_anthropic_dump())
    answer = AnthropicProvider(client=client).call(
        MESSAGES, "claude-sonnet-5-5", 4096, tools=QUEUE_TOOLS, cache_enabled=False)
    assert answer == {
        "id": "msg_11908", "type": "message", "role": "assistant", "provider": "anthropic",
        "model": "claude-sonnet-5-5",
        "content": [
            {"type": "thinking", "thinking": "Look at the queue.", "signature": "sig-1"},
            {"type": "text", "text": "Let me check."},
            {"type": "tool_use", "id": TOOL_ID, "name": "workspace_queue",
             "input": {"slim": "true"}}],
        "stop_reason": "tool_use", "stop_sequence": None,
        "usage": dict(PRODUCTION_USAGE, cache_creation_input_tokens=0)}, \
        f"only known kinds and fields may reach the caller: {answer}"
    assert FUTURE not in repr(answer), f"an unknown Anthropic field crossed the adapter: {answer}"

    broken = _anthropic_dump()
    broken["content"].append({"type": "text", "text": None})
    err = _rejected(lambda: AnthropicProvider(client=_AnthropicClient(broken)).call(
        MESSAGES, "claude-sonnet-5-5", 4096))
    assert err.detail == "a text block needs text" and err.request_id == "msg_11908", \
        f"an ill-typed known field must be refused and named: {err.detail!r}"


@th.django_unit_test()
def test_anthropic_requests_drop_unknown_keys(opts):
    from mojo.helpers.llm_providers.anthropic import AnthropicProvider

    use = {"type": "tool_use", "id": "toolu_1", "name": "workspace_queue",
           "input": {"slim": "true"}, "caller": None, "future": FUTURE}
    document = {"type": "document", "source": {"type": "text", "data": "notes",
                                               "media_type": "text/plain"}}
    messages = [
        {"role": "user", "id": FUTURE, "content": [
            {"type": "text", "text": "What is on the queue?", "future": FUTURE,
             "cache_control": {"type": "ephemeral"}}, document]},
        {"role": "assistant", "content": [use]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "future": FUTURE,
             "content": [{"type": "text", "text": "two items", "future": FUTURE}]}]},
    ]
    server_tool = {"type": "web_search_20250305", "name": "web_search", "max_uses": 2}
    tools = [dict(QUEUE_TOOLS[0], future=FUTURE), server_tool]
    system = [{"type": "text", "text": "Be brief.", "future": FUTURE}]
    for cache_enabled in (True, False):
        client = _AnthropicClient(_anthropic_dump())
        adapter = AnthropicProvider(client=client)
        adapter.call(messages, "claude-sonnet-5-5", 4096, system=system, tools=tools,
                     cache_enabled=cache_enabled)
        sent = client.messages.created[0]
        marker = {"cache_control": {"type": "ephemeral"}} if cache_enabled else {}
        assert sent["messages"] == [
            {"role": "user", "content": [
                dict({"type": "text", "text": "What is on the queue?"}, **marker), document]},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "workspace_queue",
                 "input": {"slim": "true"}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1",
                 "content": [{"type": "text", "text": "two items"}]}]}], \
            f"unknown keys must be dropped, cache_control kept only with caching: {sent['messages']}"
        assert sent["tools"] == [QUEUE_TOOLS[0], server_tool] \
            and sent["system"] == [{"type": "text", "text": "Be brief."}], \
            f"custom tools and system blocks drop unknown keys; provider tools pass: {sent}"
        assert FUTURE not in repr(sent), "an unknown request key reached Anthropic"
        assert ("cache_control" in sent) is cache_enabled, \
            "the request-level cache marker must follow the guard's setting"
        assert messages[0]["id"] == FUTURE and use["future"] == FUTURE, \
            "the caller's own transcript must not be changed in place"
        adapter.count_input_tokens(messages, "claude-sonnet-5-5", system=system, tools=tools)
        assert FUTURE not in repr(client.messages.counted), \
            "the free count must see the same request the answer is asked with"
