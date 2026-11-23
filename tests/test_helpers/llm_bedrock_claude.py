"""Claude inference profiles on the Bedrock adapter (#10757).

Fake clients only: nothing here patches, touches settings or reaches AWS.
"""

from testit import helpers as th


HAIKU_IDS = (
    "us.anthropic.claude-haiku-5-5",
    "ca.anthropic.claude-haiku-5-5",
    "eu.anthropic.claude-haiku-5-5",
    "au.anthropic.claude-haiku-5-5",
    "jp.anthropic.claude-haiku-5-5",
    "global.anthropic.claude-haiku-5-5",
)
SONNET_IDS = (
    "us.anthropic.claude-sonnet-5-5",
    "ca.anthropic.claude-sonnet-5-5",
    "eu.anthropic.claude-sonnet-5-5",
    "au.anthropic.claude-sonnet-5-5",
    "global.anthropic.claude-sonnet-5-5",
)
OPUS_IDS = (
    "us.anthropic.claude-opus-5-5",
    "eu.anthropic.claude-opus-5-5",
    "au.anthropic.claude-opus-5-5",
    "jp.anthropic.claude-opus-5-5",
    "global.anthropic.claude-opus-5-5",
)
NOVA_IDS = (
    "us.amazon.nova-2-lite-v1:0",
    "eu.amazon.nova-2-lite-v1:0",
    "jp.amazon.nova-2-lite-v1:0",
    "global.amazon.nova-2-lite-v1:0",
)
CLAUDE_IDS = HAIKU_IDS + SONNET_IDS + OPUS_IDS
SONNET = "us.anthropic.claude-sonnet-5-5"
MESSAGES = [{"role": "user", "content": "Hello"}]
USAGE = {"input_tokens": 7, "output_tokens": 11,
         "cache_read_input_tokens": 3, "cache_creation_input_tokens": 5}
STREAM_USAGE = {"inputTokens": 7, "outputTokens": 11, "cacheReadInputTokens": 3,
                "cacheWriteInputTokens": 5, "totalTokens": 26}
TOOLS = [{"name": "lookup_item", "description": "Look up a board item",
          "input_schema": {"type": "object", "properties": {"id": {"type": "integer"}},
                           "required": ["id"]}}]

# The effort table from the plan, per model family.
EXPECTED_FIELDS = {
    ("haiku", "off"): {"thinking": {"type": "disabled"}, "output_config": {"effort": "low"}},
    ("sonnet", "off"): {"thinking": {"type": "between_tools"},
                        "output_config": {"effort": "low"}},
    ("opus", "off"): {"thinking": {"type": "adaptive"}, "output_config": {"effort": "low"}},
}
for _name in ("haiku", "sonnet", "opus"):
    for _level in ("low", "medium"):
        EXPECTED_FIELDS[(_name, _level)] = {
            "thinking": {"type": "adaptive"}, "output_config": {"effort": _level}}


def _family(model):
    if model in HAIKU_IDS:
        return "haiku"
    if model in SONNET_IDS:
        return "sonnet"
    return "opus"


def _response(content=None, stop="end_turn"):
    if content is None:
        content = [{"reasoningContent": {"reasoningText": {
                        "text": "", "signature": "private-claude-signature"}}},
                   {"text": "Final answer"}]
    return {"ResponseMetadata": {"RequestId": "bedrock-claude-1"},
            "output": {"message": {"role": "assistant", "content": content}},
            "stopReason": stop,
            "usage": {"inputTokens": 7, "outputTokens": 11,
                      "cacheReadInputTokens": 3, "cacheWriteInputTokens": 5}}


class _Client:
    def __init__(self, response=None):
        self.response = _response() if response is None else response
        self.calls = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class _EventStream:
    def __init__(self, events):
        self.events = list(events)
        self.closed = False

    def __iter__(self):
        return iter(self.events)

    def close(self):
        self.closed = True


class _StreamClient:
    def __init__(self, events):
        self.stream = _EventStream(events)
        self.calls = []

    def converse_stream(self, **kwargs):
        self.calls.append(kwargs)
        return {"ResponseMetadata": {"RequestId": "bedrock-claude-stream"},
                "stream": self.stream}


def _stream_events(parts=("Hel", "lo"), stop="end_turn", tool_input=None):
    events = [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {
            "reasoningContent": {"text": "private claude thought"}}}},
        {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {
            "reasoningContent": {"signature": "private-claude-signature"}}}},
        {"contentBlockStop": {"contentBlockIndex": 0}},
    ]
    events += [{"contentBlockDelta": {"contentBlockIndex": 1, "delta": {"text": part}}}
               for part in parts]
    if parts:
        events.append({"contentBlockStop": {"contentBlockIndex": 1}})
    if tool_input is not None:
        events.append({"contentBlockStart": {"contentBlockIndex": 2, "start": {
            "toolUse": {"toolUseId": "tooluse_claude-1", "name": "lookup_item"}}}})
        events.append({"contentBlockDelta": {"contentBlockIndex": 2, "delta": {
            "toolUse": {"input": tool_input}}}})
        events.append({"contentBlockStop": {"contentBlockIndex": 2}})
    events.append({"messageStop": {"stopReason": stop}})
    events.append({"metadata": {"usage": dict(STREAM_USAGE), "metrics": {"latencyMs": 5}}})
    return events


def _policy(model):
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
            "capabilities": ["text", "tools", "images"], "region": "us-east-1",
            "reasoning_efforts": ["off", "low", "medium"]}},
        "shared": dict(limits),
        "features": {"unattributed": dict(limits)},
        "breaker": {"auth_failures": 2, "rate_failures": 2,
                    "server_failures": 2, "open_seconds": 60},
    }


def _png(size=(4, 3), noise=False):
    import io
    import os
    from PIL import Image

    out = io.BytesIO()
    if noise:
        image = Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3))
    else:
        image = Image.new("RGB", size, (10, 120, 200))
    image.save(out, format="PNG", compress_level=0)
    return out.getvalue()


def _rejected(run):
    from mojo.helpers.llm_providers.base import ProviderError

    try:
        run()
    except ProviderError as err:
        return err
    assert False, "the Bedrock adapter should have rejected this request or reply"


@th.django_unit_test()
def test_one_allow_list_names_exactly_the_twenty_ids(opts):
    from mojo.apps.account.services import llm_safety
    from mojo.helpers.llm_providers import bedrock

    assert llm_safety.BEDROCK_MODELS is bedrock.BEDROCK_MODELS, \
        "the policy check and the adapter must read one allow-list object"
    literal = {
        "us.amazon.nova-2-lite-v1:0", "eu.amazon.nova-2-lite-v1:0",
        "jp.amazon.nova-2-lite-v1:0", "global.amazon.nova-2-lite-v1:0",
        "us.anthropic.claude-haiku-5-5", "ca.anthropic.claude-haiku-5-5",
        "eu.anthropic.claude-haiku-5-5", "au.anthropic.claude-haiku-5-5",
        "jp.anthropic.claude-haiku-5-5", "global.anthropic.claude-haiku-5-5",
        "us.anthropic.claude-sonnet-5-5", "ca.anthropic.claude-sonnet-5-5",
        "eu.anthropic.claude-sonnet-5-5", "au.anthropic.claude-sonnet-5-5",
        "global.anthropic.claude-sonnet-5-5",
        "us.anthropic.claude-opus-5-5", "eu.anthropic.claude-opus-5-5",
        "au.anthropic.claude-opus-5-5", "jp.anthropic.claude-opus-5-5",
        "global.anthropic.claude-opus-5-5",
    }
    assert set(bedrock.BEDROCK_MODELS) == literal and len(literal) == 20, \
        f"the allow-list must be exactly the 20 published ids, got {sorted(bedrock.BEDROCK_MODELS)}"
    assert isinstance(bedrock.BEDROCK_MODELS, frozenset), \
        "the allow-list must be immutable"
    families = {model: _family(model) for model in CLAUDE_IDS}
    assert bedrock.CLAUDE_MODELS == families, \
        f"each Claude id must map to its model, got {bedrock.CLAUDE_MODELS}"
    for model in CLAUDE_IDS:
        assert bedrock.model_family(model) == "claude", f"{model} must be a Claude route"
    for model in NOVA_IDS:
        assert bedrock.model_family(model) == "nova", f"{model} must stay a Nova route"


@th.django_unit_test()
def test_policy_accepts_every_claude_id_and_refuses_near_misses(opts):
    from mojo.apps.account.services import llm_safety

    for model in CLAUDE_IDS + NOVA_IDS:
        parsed = llm_safety.parse_policy(_policy(model))
        assert parsed["routes"]["unattributed"]["model"] == model, \
            f"the listed Bedrock id {model} must survive policy parsing"
    near_misses = (
        "anthropic.claude-sonnet-5-5", "jp.anthropic.claude-sonnet-5-5",
        "ca.anthropic.claude-opus-5-5", "us.anthropic.claude-sonnet-5",
        "us.anthropic.claude-opus-4-8", "us.anthropic.claude-sonnet-5-5-v1:0",
        "arn:aws:bedrock:us-east-1:123456789012:inference-profile/"
        "us.anthropic.claude-sonnet-5-5",
        "us.anthropic.claude-", "us.anthropic.claude-haiku-5-5 ",
        "US.anthropic.claude-haiku-5-5", "anthropic.claude-opus-5-5",
    )
    for model in near_misses:
        try:
            llm_safety.parse_policy(_policy(model))
            assert False, f"the unlisted Bedrock model {model!r} was accepted"
        except llm_safety.LLMSafetyError as err:
            assert err.code == "policy_invalid", \
                f"an unlisted Bedrock model must be policy_invalid, got {err.code} for {model!r}"


@th.django_unit_test()
def test_claude_requests_follow_the_effort_table_and_never_send_reasoning_config(opts):
    from mojo.helpers.llm_providers.bedrock import BedrockProvider

    for model in CLAUDE_IDS:
        for effort in ("off", "low", "medium"):
            client = _Client()
            BedrockProvider(client=client).call(
                MESSAGES, model, 32, system="Be brief", reasoning_effort=effort)
            expected = {
                "modelId": model, "messages": [{"role": "user", "content": [{"text": "Hello"}]}],
                "inferenceConfig": {"maxTokens": 32}, "system": [{"text": "Be brief"}],
                "additionalModelRequestFields": EXPECTED_FIELDS[(_family(model), effort)]}
            assert client.calls == [expected], \
                f"{model} effort {effort} sent the wrong request: {client.calls}"
            assert "reasoningConfig" not in repr(client.calls), \
                f"Nova's reasoningConfig must never reach Claude ({model}, {effort})"


@th.django_unit_test()
def test_nova_requests_are_unchanged(opts):
    from mojo.helpers.llm_providers.bedrock import BedrockProvider

    for model in NOVA_IDS:
        for effort in ("off", "low", "medium"):
            client = _Client()
            BedrockProvider(client=client).call(
                MESSAGES, model, 32, system="Be brief", reasoning_effort=effort)
            expected = {
                "modelId": model, "messages": [{"role": "user", "content": [{"text": "Hello"}]}],
                "inferenceConfig": {"maxTokens": 32}, "system": [{"text": "Be brief"}]}
            if effort != "off":
                expected["additionalModelRequestFields"] = {"reasoningConfig": {
                    "type": "enabled", "maxReasoningEffort": effort}}
            assert client.calls == [expected], \
                f"the Nova request for {model}/{effort} changed: {client.calls}"
            assert list(client.calls[0]) == list(expected), \
                f"the Nova request keys must keep their order: {list(client.calls[0])}"


@th.django_unit_test()
def test_reasoning_fields_are_fresh_on_every_call(opts):
    from mojo.helpers.llm_providers.bedrock import reasoning_fields

    for model in CLAUDE_IDS + NOVA_IDS:
        for effort in ("off", "low", "medium"):
            first = reasoning_fields(model, effort)
            if first is None:
                assert model in NOVA_IDS and effort == "off", \
                    f"only Nova off may send no extra fields, got None for {model}/{effort}"
                continue
            expected = repr(first)
            first.clear()
            second = reasoning_fields(model, effort)
            assert repr(second) == expected, \
                f"mutating a returned dict changed the next request for {model}/{effort}"
            inner = next(iter(second.values()))
            inner.clear()
            assert repr(reasoning_fields(model, effort)) == expected, \
                f"nested request fields must not be shared for {model}/{effort}"


@th.django_unit_test()
def test_claude_text_reply_normalizes_like_nova_and_drops_signed_reasoning(opts):
    from mojo.helpers.llm_providers.bedrock import BedrockProvider

    for model in CLAUDE_IDS:
        result = BedrockProvider(client=_Client()).call(MESSAGES, model, 32)
        assert result == {"id": "bedrock-claude-1", "type": "message", "role": "assistant",
                          "provider": "bedrock", "model": model,
                          "content": [{"type": "text", "text": "Final answer"}],
                          "stop_reason": "end_turn", "usage": USAGE}, \
            f"a Claude reply must normalise to the call() shape: {result}"
        assert "private-claude-signature" not in repr(result), \
            "a reasoning signature must never cross the adapter boundary"
    redacted = _response(content=[{"reasoningContent": {"redactedContent": b"hidden"}},
                                  {"text": "Visible"}])
    result = BedrockProvider(client=_Client(redacted)).call(MESSAGES, SONNET, 32)
    assert result["content"] == [{"type": "text", "text": "Visible"}], \
        f"redacted Claude reasoning must be dropped, got {result['content']}"


@th.django_unit_test()
def test_claude_tool_round_trip_replays_without_reasoning(opts):
    from mojo.helpers.llm_providers.bedrock import BedrockProvider

    asked = _response(stop="tool_use", content=[
        {"reasoningContent": {"reasoningText": {"text": "", "signature": "sig"}}},
        {"text": "Checking."},
        {"toolUse": {"toolUseId": "tooluse_claude-1", "name": "lookup_item",
                     "input": {"id": 7}}}])
    client = _Client(asked)
    adapter = BedrockProvider(client=client)
    first = adapter.call(MESSAGES, SONNET, 32, tools=TOOLS, reasoning_effort="low")
    assert first["stop_reason"] == "tool_use" and first["content"] == [
        {"type": "text", "text": "Checking."},
        {"type": "tool_use", "id": "tooluse_claude-1", "name": "lookup_item",
         "input": {"id": 7}}], f"a Claude tool request must normalise like Nova's: {first}"
    transcript = MESSAGES + [
        {"role": "assistant", "content": first["content"]},
        {"role": "user", "content": [{"type": "tool_result",
                                      "tool_use_id": "tooluse_claude-1", "content": "open"}]}]
    client.response = _response()
    second = adapter.call(transcript, SONNET, 32, tools=TOOLS, reasoning_effort="low")
    assert second["content"] == [{"type": "text", "text": "Final answer"}], \
        f"the answer after a Claude tool round is wrong: {second}"
    wire = client.calls[1]
    assert wire["messages"][1] == {"role": "assistant", "content": [
        {"text": "Checking."},
        {"toolUse": {"toolUseId": "tooluse_claude-1", "name": "lookup_item",
                     "input": {"id": 7}}}]}, \
        f"the replayed assistant turn must carry no reasoning block: {wire['messages'][1]}"
    assert wire["toolConfig"]["tools"][0]["toolSpec"]["name"] == "lookup_item" \
        and wire["additionalModelRequestFields"] == EXPECTED_FIELDS[("sonnet", "low")], \
        f"a Claude tool round must keep its toolConfig and effort fields: {wire}"


@th.django_unit_test()
def test_claude_stream_yields_text_and_hides_reasoning(opts):
    from mojo.helpers.llm_providers.bedrock import BedrockProvider

    client = _StreamClient(_stream_events())
    events = list(BedrockProvider(client=client).stream(
        MESSAGES, SONNET, 32, reasoning_effort="medium"))
    assert [event["type"] for event in events] == ["text_delta", "text_delta", "final"] \
        and [event["text"] for event in events[:2]] == ["Hel", "lo"], \
        f"a Claude stream must yield the text deltas then one final: {events}"
    assert events[-1]["response"] == {
        "id": "bedrock-claude-stream", "type": "message", "role": "assistant",
        "provider": "bedrock", "model": SONNET,
        "content": [{"type": "text", "text": "Hello"}],
        "stop_reason": "end_turn", "usage": USAGE}, \
        f"the Claude final record must be the call() shape: {events[-1]}"
    assert "private" not in repr(events), "Claude reasoning text or signature was yielded"
    assert client.calls[0]["additionalModelRequestFields"] == \
        EXPECTED_FIELDS[("sonnet", "medium")], \
        f"converse_stream must receive the Claude effort fields: {client.calls[0]}"

    client = _StreamClient(_stream_events(parts=("Let me check.",), stop="tool_use",
                                          tool_input='{"id": 9}'))
    events = list(BedrockProvider(client=client).stream(MESSAGES, SONNET, 32, tools=TOOLS))
    assert [event["type"] for event in events] == ["text_delta", "final"] \
        and events[-1]["response"]["content"][-1] == {
            "type": "tool_use", "id": "tooluse_claude-1", "name": "lookup_item",
            "input": {"id": 9}}, \
        f"a Claude tool request must arrive only in the final record: {events}"


@th.django_unit_test()
def test_claude_image_is_sent_as_bytes_and_capped(opts):
    from mojo.helpers.llm_images import image_block
    from mojo.helpers.llm_providers.bedrock import (
        CLAUDE_MAX_IMAGE_BYTES, BedrockProvider, image_reserved_tokens)

    raw = _png()
    client = _Client()
    messages = [{"role": "user", "content": [
        image_block(raw, "image/png"), {"type": "text", "text": "Describe it."}]}]
    result = BedrockProvider(client=client).call(messages, SONNET, 32)
    assert client.calls[0]["messages"] == [{"role": "user", "content": [
        {"image": {"format": "png", "source": {"bytes": raw}}}, {"text": "Describe it."}]}] \
        and result["content"] == [{"type": "text", "text": "Final answer"}], \
        f"a Claude image request must reach Converse as raw bytes: {client.calls}"

    big = _png(size=(1150, 1120), noise=True)
    assert CLAUDE_MAX_IMAGE_BYTES == 3750000 and CLAUDE_MAX_IMAGE_BYTES < len(big) \
        <= 5 * 1024 * 1024, f"the fixture must sit between the two caps, got {len(big)}"
    oversized = [{"role": "user", "content": [image_block(big, "image/png")]}]
    for model in CLAUDE_IDS:
        adapter = BedrockProvider(client=_Client())
        err = _rejected(lambda: adapter.check_request(oversized, model, 32, timeout=10))
        assert err.code == "provider_rejected" and adapter.client.calls == [], \
            f"a Claude image over 3,750,000 raw bytes must be refused before dispatch ({model})"
    nova = BedrockProvider(client=_Client())
    nova.call(oversized, NOVA_IDS[0], 32)
    assert len(nova.client.calls) == 1, "Nova's image cap must stay unchanged"
    assert [image_reserved_tokens(model) for model in (SONNET, NOVA_IDS[0])] == [4800, 1600], \
        "Claude must reserve 4800 tokens per image and Nova 1600"


@th.django_unit_test()
def test_claude_answer_cut_at_max_tokens_names_the_reason(opts):
    from mojo.helpers.llm_providers.bedrock import BedrockProvider

    thinking_only = _response(stop="max_tokens", content=[
        {"reasoningContent": {"reasoningText": {"text": "", "signature": "sig"}}}])
    client = _Client(thinking_only)
    err = _rejected(lambda: BedrockProvider(client=client).call(MESSAGES, SONNET, 32))
    assert err.code == "provider_rejected" and err.usage == USAGE \
        and err.detail == "max_tokens reached before any answer text", \
        f"thinking that spent the cap must be rejected with its usage and reason: {err.code}/{err.detail}"
    events = _stream_events(parts=(), stop="max_tokens")
    stream = BedrockProvider(client=_StreamClient(events)).stream(MESSAGES, SONNET, 32)
    err = _rejected(lambda: list(stream))
    assert err.code == "provider_rejected" and err.usage == USAGE \
        and err.detail == "max_tokens reached before any answer text", \
        f"a streamed answer cut before text must say why: {err.code}/{err.detail}"
    filtered = _rejected(lambda: BedrockProvider(client=_Client(
        _response(stop="content_filtered"))).call(MESSAGES, SONNET, 32))
    assert filtered.detail == "unexpected stop reason content_filtered", \
        f"a refused stop reason must be named on the error (#11908), got {filtered.detail!r}"


@th.django_unit_test()
def test_unlisted_models_never_reach_converse(opts):
    from mojo.helpers.llm_providers.bedrock import BedrockProvider

    for model in ("anthropic.claude-sonnet-5-5", "jp.anthropic.claude-sonnet-5-5",
                  "ca.anthropic.claude-opus-5-5", "us.anthropic.claude-opus-4-8",
                  "us.anthropic.claude-haiku-5-5-v1:0", None):
        client = _Client()
        err = _rejected(lambda: BedrockProvider(client=client).call(MESSAGES, model, 32))
        assert err.code == "provider_rejected" and client.calls == [], \
            f"the unlisted model {model!r} reached Converse"
        streamer = _StreamClient(_stream_events())
        err = _rejected(lambda: BedrockProvider(client=streamer).stream(MESSAGES, model, 32))
        assert err.code == "provider_rejected" and streamer.calls == [], \
            f"the unlisted model {model!r} reached ConverseStream"
