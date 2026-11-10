from testit import helpers as th


MODEL = "us.amazon.nova-2-lite-v1:0"
MESSAGES = [{"role": "user", "content": "Hello"}]
USAGE = {"input_tokens": 7, "output_tokens": 11,
         "cache_read_input_tokens": 3, "cache_creation_input_tokens": 5}


def _response():
    return {"ResponseMetadata": {"RequestId": "bedrock-request-123"},
            "output": {"message": {"role": "assistant", "content": [
                {"reasoningContent": {"reasoningText": {"text": "[REDACTED]"}}},
                {"text": "Final answer"}]}}, "stopReason": "end_turn",
            "usage": {"inputTokens": 7, "outputTokens": 11,
                      "cacheReadInputTokens": 3, "cacheWriteInputTokens": 5}}


class _Client:
    def __init__(self, response=None, error=None):
        self.response = _response() if response is None else response
        self.error = error
        self.calls = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


def _call_rejected(client, expected_usage=None, **kwargs):
    from mojo.helpers.llm_providers.base import ProviderError
    from mojo.helpers.llm_providers.bedrock import BedrockProvider

    parameters = {"messages": MESSAGES, "model": MODEL, "max_tokens": 32}
    parameters.update(kwargs)
    try:
        BedrockProvider(client=client).call(**parameters)
        assert False, "an invalid Bedrock request/response must fail safely"
    except ProviderError as err:
        assert err.code == "provider_rejected", \
            f"invalid Bedrock data must use provider_rejected, got {err.code}"
        assert err.usage == expected_usage, \
            f"rejected response lost its validated usage: {err.usage}"
        return err


@th.django_unit_test()
def test_provider_error_usage_is_complete_bounded_and_copied(opts):
    from mojo.helpers.llm_providers.base import ProviderError, validated_usage

    legacy = ProviderError("provider_failed", "request-legacy", 5)
    assert legacy.code == "provider_failed" and legacy.request_id == "request-legacy" \
        and legacy.retry_after == 5 and legacy.usage is None, \
        "adding usage must preserve legacy positional ProviderError semantics"
    source = dict(USAGE)
    error = ProviderError("provider_rejected", usage=source)
    source["input_tokens"] = 999
    assert error.usage == USAGE, "error usage must be copied instead of borrowing mutable evidence"
    zero = {key: 0 for key in USAGE}
    assert validated_usage(zero) == zero, "complete zero usage is valid evidence, distinct from unknown"
    malformed = [None, [], {}, dict(USAGE, unexpected=1)]
    malformed.extend(dict(USAGE, input_tokens=value) for value in (
        True, -1, 1.0, "7", 2147483648))
    incomplete = dict(USAGE)
    incomplete.pop("output_tokens")
    malformed.append(incomplete)
    for value in malformed:
        assert ProviderError("provider_rejected", usage=value).usage is None, \
            f"invalid or incomplete token evidence must remain unknown: {value!r}"


@th.django_unit_test()
def test_registry_adds_bedrock_without_constructing_any_sdk(opts):
    from mojo.helpers.llm_providers import get_provider
    from mojo.helpers.llm_providers.anthropic import AnthropicProvider
    from mojo.helpers.llm_providers.bedrock import BedrockProvider

    client = _Client()
    bedrock = get_provider("bedrock", region="us-east-1", client=client)
    anthropic = get_provider("anthropic", api_key="test-only-placeholder")
    assert isinstance(bedrock, BedrockProvider) and client.calls == [], \
        "registry/adapter construction must not dispatch a Bedrock request"
    assert isinstance(anthropic, AnthropicProvider) \
        and anthropic.api_key == "test-only-placeholder" and anthropic.client is None, \
        "the Anthropic constructor contract must remain unchanged and lazy"
    assert get_provider("unknown") is None, "unknown provider names must remain unsupported"
    assert bedrock.capabilities == frozenset({"text", "images", "tools", "stream"}), \
        f"Bedrock adapter must advertise text, images, tools and stream, got {bedrock.capabilities}"


@th.django_unit_test()
def test_converse_translates_text_and_preserves_discrete_usage(opts):
    from mojo.helpers.llm_providers.bedrock import BedrockProvider

    client = _Client()
    messages = [{"role": "user", "content": "Question"},
                {"role": "assistant", "content": [{"type": "text", "text": "Prior"}]},
                {"role": "user", "content": [{"type": "text", "text": "Follow-up"}]}]
    result = BedrockProvider(client=client).call(
        messages, model=MODEL, max_tokens=32,
        system=[{"type": "text", "text": "System"}], reasoning_effort="medium")
    expected = {"modelId": MODEL, "messages": [
        {"role": "user", "content": [{"text": "Question"}]},
        {"role": "assistant", "content": [{"text": "Prior"}]},
        {"role": "user", "content": [{"text": "Follow-up"}]}],
        "system": [{"text": "System"}], "inferenceConfig": {"maxTokens": 32},
        "additionalModelRequestFields": {"reasoningConfig": {
            "type": "enabled", "maxReasoningEffort": "medium"}}}
    assert client.calls == [expected], \
        f"Converse must receive only approved text/reasoning fields, got {client.calls}"
    assert result["content"] == [{"type": "text", "text": "Final answer"}], \
        f"only final text may cross the adapter boundary, got {result['content']}"
    assert "REDACTED" not in repr(result), "redacted reasoning must never leak into normalized output"
    assert result["usage"] == USAGE and sum(result["usage"].values()) == 26, \
        f"ordinary/cache input classes and billed output must remain separate, got {result['usage']}"
    assert result["provider"] == "bedrock" and result["id"] == "bedrock-request-123" \
        and result["model"] == MODEL and result["stop_reason"] == "end_turn", \
        f"normalized response must retain safe provider routing/evidence, got {result}"


@th.django_unit_test()
def test_efforts_keep_max_tokens_and_never_send_guardrails_or_cache_controls(opts):
    from mojo.helpers.llm_providers.bedrock import BedrockProvider

    for effort in ("off", "low", "medium"):
        client = _Client()
        BedrockProvider(client=client).call(
            MESSAGES, MODEL, 32, system="System", reasoning_effort=effort,
            cache_enabled=True)
        request = client.calls[0]
        assert request["inferenceConfig"] == {"maxTokens": 32}, \
            f"effort {effort} must always keep the explicit output cap, got {request}"
        extra = request.get("additionalModelRequestFields")
        assert extra == (None if effort == "off" else {"reasoningConfig": {
            "type": "enabled", "maxReasoningEffort": effort}}), \
            f"effort {effort} must translate to only the fixed reasoningConfig, got {extra}"
        assert set(request) <= {"modelId", "messages", "system", "inferenceConfig",
                                "additionalModelRequestFields"}, \
            f"cache/guardrail/tool/provider-option fields must not be sent: {request}"


@th.django_unit_test()
def test_sdk_client_is_lazy_sigv4_single_attempt_and_budgeted_per_call(opts):
    from mojo.helpers.llm_providers.base import ProviderError
    from mojo.helpers.llm_providers.bedrock import BedrockProvider

    clients = []

    def factory(service, **kwargs):
        clients.append((service, kwargs))
        return _Client()

    adapter = BedrockProvider(region="us-east-1", client_factory=factory)
    assert clients == [], "adapter construction must not resolve AWS credentials or create a client"
    for timeout in (1, 10):
        adapter.call(MESSAGES, MODEL, 32, timeout=timeout)
        service, kwargs = clients[-1]
        config = kwargs["config"]
        assert service == "bedrock-runtime" and set(kwargs) == {"region", "config"} \
            and kwargs["region"] == "us-east-1", \
            f"client construction must use policy region and the ambient credential chain: {kwargs}"
        assert config.signature_version == "v4" \
            and config.ignore_configured_endpoint_urls is True, \
            "Bedrock must pin IAM SigV4 and ignore configured endpoint/bearer alternatives"
        assert config.retries == {"total_max_attempts": 1, "mode": "standard"}, \
            f"SDK retries must not multiply an accounted inference, got {config.retries}"
        assert 0 < config.connect_timeout <= 3 and config.read_timeout > 0 \
            and config.connect_timeout + config.read_timeout == timeout, \
            f"connect/read inactivity settings must fit this call's budget: {config}"
    assert len(clients) == 2, "reusing an adapter must not retain a previous call's timeout configuration"
    for region in ("us-east-0", "us-east-01", "US-east-1", "us-east-1\n"):
        try:
            BedrockProvider(region=region, client_factory=factory).call(MESSAGES, MODEL, 32)
            assert False, f"noncanonical region must be refused before SDK construction: {region!r}"
        except ProviderError as err:
            assert err.code == "provider_rejected", \
                f"invalid region must fail with a safe provider code, got {err.code}"
    assert len(clients) == 2, "invalid regions must never construct an AWS client"


@th.django_unit_test()
def test_unsupported_inputs_never_reach_converse(opts):
    cases = [
        {"model": "amazon.nova-2-lite-v1:0"}, {"model": "some-other-model"},
        {"reasoning_effort": "high"}, {"reasoning_effort": {}},
        {"max_tokens": 0}, {"max_tokens": True}, {"max_tokens": 65001},
        {"max_tokens": 1.0}, {"tools": [{"name": "anything"}]},
        {"messages": []}, {"messages": [{"role": "system", "content": "No"}]},
        {"messages": [{"role": "user", "content": " "}]},
        {"messages": [{"role": "user", "content": [{"type": "image", "source": {}}]}]},
        {"messages": [{"role": "user", "content": [
            {"type": "text", "text": "Hello", "cache_control": {"type": "ephemeral"}}]}]},
        {"system": [{"cachePoint": {"type": "default"}}]},
        {"timeout": 0}, {"timeout": True}, {"timeout": float("inf")},
        {"timeout": 10 ** 1000},
    ]
    for kwargs in cases:
        client = _Client()
        _call_rejected(client, **kwargs)
        assert client.calls == [], f"unsupported input reached Converse: {kwargs}"


@th.django_unit_test()
def test_profile_variants_and_missing_cache_fields_are_supported(opts):
    from mojo.helpers.llm_providers.bedrock import BedrockProvider

    for prefix in ("us", "eu", "jp", "global"):
        response = _response()
        response["usage"].pop("cacheReadInputTokens")
        response["usage"].pop("cacheWriteInputTokens")
        profile = f"{prefix}.amazon.nova-2-lite-v1:0"
        result = BedrockProvider(client=_Client(response)).call(MESSAGES, profile, 32)
        assert result["model"] == profile and result["usage"] == dict(
            USAGE, cache_read_input_tokens=0, cache_creation_input_tokens=0), \
            f"known profile with absent optional cache counts must normalize correctly: {result}"


@th.django_unit_test()
def test_invalid_answers_keep_billed_usage_and_provider_error_codes(opts):
    cases = []
    for reason in ("content_filtered", "malformed_model_output", "tool_use", "unknown"):
        response = _response()
        response["stopReason"] = reason
        cases.append(response)
    for content in ([], [{"text": " "}],
                    [{"reasoningContent": {"redactedContent": b"hidden"}}],
                    [{"reasoningContent": {"reasoningText": {}}}, {"text": "Visible"}],
                    [{"reasoningContent": {"reasoningText": {
                        "text": "hidden", "unexpected": "private"}}}, {"text": "Visible"}],
                    [{"reasoningContent": {"reasoningText": {
                        "text": "hidden", "signature": 4}}}, {"text": "Visible"}],
                    [{"reasoningContent": {"redactedContent": b"hidden", "unknown": True}},
                     {"text": "Visible"}],
                    [{"text": "Visible"}, {"toolUse": {"name": "not-allowed"}}],
                    [{"text": "x" * (1024 * 1024 + 1)}],
                    [{"text": "\ud800"}]):
        response = _response()
        response["output"]["message"]["content"] = content
        cases.append(response)
    response = _response()
    response["output"]["message"]["role"] = "user"
    cases.append(response)
    response = _response()
    response["ResponseMetadata"]["RequestId"] = "unsafe\nrequest-id"
    cases.append(response)

    class BrokenOutput(dict):
        def get(self, key, default=None):
            if key == "output":
                raise TypeError("private provider normalization error")
            return super().get(key, default)

    cases.append(BrokenOutput(_response()))
    response = _response()
    response["stopReason"] = "max_tokens"
    response["output"]["message"]["content"] = [
        {"reasoningContent": {"redactedContent": b"hidden"}}]
    cases.append(response)
    for response in cases:
        client = _Client(response)
        _call_rejected(client, expected_usage=USAGE)
        assert len(client.calls) == 1, "invalid output must not trigger an automatic second inference"
    over_cap = _response()
    over_cap["usage"]["outputTokens"] = 33
    _call_rejected(_Client(over_cap), expected_usage=dict(USAGE, output_tokens=33))


@th.django_unit_test()
def test_malformed_usage_stays_unknown_and_partial_visible_output_succeeds(opts):
    from mojo.helpers.llm_providers.bedrock import BedrockProvider

    for value in (None, True, -1, 1.0, "7", 2147483648):
        response = _response()
        response["usage"]["inputTokens"] = value
        _call_rejected(_Client(response))
    response = _response()
    response.pop("usage")
    _call_rejected(_Client(response))
    response = _response()
    response["stopReason"] = "max_tokens"
    response["output"]["message"]["content"][0]["reasoningContent"]["reasoningText"][
        "signature"] = "private-signature"
    response["output"]["message"]["content"].insert(0, {"redactedContent": b"hidden"})
    result = BedrockProvider(client=_Client(response)).call(MESSAGES, MODEL, 32)
    assert result["stop_reason"] == "max_tokens" and result["usage"] == USAGE \
        and result["content"] == [{"type": "text", "text": "Final answer"}], \
        f"bounded visible output at the cap must be an explicit partial success: {result}"
    assert "private-signature" not in repr(result), "reasoning signatures must remain private"


@th.django_unit_test()
def test_sdk_errors_are_safe_bounded_and_never_use_guard_refusal_codes(opts):
    from botocore import exceptions as be
    from mojo.helpers.llm_providers.base import ProviderError
    from mojo.helpers.llm_providers.bedrock import BedrockProvider

    errors = [
        (be.NoCredentialsError(), "provider_authentication", None),
        (be.PartialCredentialsError(provider="test", cred_var="secret"),
         "provider_authentication", None),
        (be.ProfileNotFound(profile="secret"), "provider_authentication", None),
        (be.ReadTimeoutError(endpoint_url="https://secret.invalid"), "provider_timeout", None),
        (be.ConnectTimeoutError(endpoint_url="https://secret.invalid"), "provider_timeout", None),
        (be.EndpointConnectionError(endpoint_url="https://secret.invalid"),
         "provider_unavailable", None),
        (RuntimeError("secret raw exception"), "provider_failed", None),
        (ProviderError("budget_exhausted"), "provider_failed", None),
    ]
    for aws_code, status, expected in (
            ("AccessDeniedException", 403, "provider_authentication"),
            ("ThrottlingException", 429, "provider_rate_limited"),
            ("ModelNotReadyException", 429, "provider_rate_limited"),
            ("ModelTimeoutException", 408, "provider_timeout"),
            ("InternalServerException", 500, "provider_unavailable"),
            ("ValidationException", 400, "provider_rejected"),
            ("ResourceNotFoundException", 404, "provider_rejected")):
        response = {"Error": {"Code": aws_code, "Message": "secret raw response"},
                    "ResponseMetadata": {"RequestId": "safe-sdk-request",
                                         "HTTPStatusCode": status,
                                         "HTTPHeaders": {"retry-after": "7200"}}}
        errors.append((be.ClientError(response, "Converse"), expected,
                       3600 if expected == "provider_rate_limited" else None))
    for error, code, retry_after in errors:
        client = _Client(error=error)
        try:
            BedrockProvider(client=client).call(MESSAGES, MODEL, 32)
            assert False, "a failed provider request must raise a safe ProviderError"
        except ProviderError as err:
            assert err.code == code and err.retry_after == retry_after, \
                f"SDK error lost its bounded classification: {err.code}/{err.retry_after}"
            assert str(err) == code and "secret" not in str(err) and err.usage is None, \
                f"SDK error leaked raw text or invented billing evidence: {err}"
            assert len(client.calls) == 1, "one accounted adapter call must make no hidden retry"


STREAM_USAGE = {"inputTokens": 7, "outputTokens": 11, "cacheReadInputTokens": 3,
                "cacheWriteInputTokens": 5, "totalTokens": 26}
TOOLS = [{"name": "lookup_item", "description": "Look up a board item",
          "input_schema": {"type": "object", "properties": {"id": {"type": "integer"}},
                           "required": ["id"]}}]


class _Raw:
    """Stands in for the urllib3 response under botocore's EventStream."""

    def __init__(self):
        import threading
        self.stopped = threading.Event()
        self.shutdowns = 0

    def shutdown(self):
        self.shutdowns += 1
        self.stopped.set()


class _EventStream:
    """Yields events in order; an exception item is raised at its position.

    The ``"block"`` item waits until the socket is shut down, then fails the
    read the way a shut-down urllib3 socket does.
    """

    def __init__(self, events):
        self.events = list(events)
        self._raw_stream = _Raw()
        self.closed = False

    def __iter__(self):
        from urllib3.exceptions import ProtocolError
        for item in self.events:
            if item == "block":
                self._raw_stream.stopped.wait(5)
                raise ProtocolError("connection shut down")
            if isinstance(item, BaseException):
                raise item
            yield item

    def close(self):
        self.closed = True


class _StreamClient:
    def __init__(self, events=None, error=None):
        self.stream = _EventStream(_stream_events() if events is None else events)
        self.error = error
        self.calls = []

    def converse_stream(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return {"ResponseMetadata": {"RequestId": "bedrock-stream-1"}, "stream": self.stream}


def _stream_events(parts=("Hel", "lo", " there"), stop="end_turn", tool_input=None,
                   usage=True):
    events = [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {
            "reasoningContent": {"text": "[REDACTED] private chain"}}}},
        {"contentBlockStop": {"contentBlockIndex": 0}},
    ]
    events += [{"contentBlockDelta": {"contentBlockIndex": 1, "delta": {"text": part}}}
               for part in parts]
    events.append({"contentBlockStop": {"contentBlockIndex": 1}})
    if tool_input is not None:
        events.append({"contentBlockStart": {"contentBlockIndex": 2, "start": {
            "toolUse": {"toolUseId": "tooluse_abc-1", "name": "lookup_item"}}}})
        events += [{"contentBlockDelta": {"contentBlockIndex": 2, "delta": {
            "toolUse": {"input": piece}}}} for piece in tool_input]
        events.append({"contentBlockStop": {"contentBlockIndex": 2}})
    events.append({"messageStop": {"stopReason": stop}})
    if usage:
        events.append({"metadata": {"usage": dict(STREAM_USAGE), "metrics": {"latencyMs": 9}}})
    return events


def _stream(client, timeout=None, **kwargs):
    from mojo.helpers.llm_providers.bedrock import BedrockProvider

    parameters = {"messages": MESSAGES, "model": MODEL, "max_tokens": 32}
    parameters.update(kwargs)
    if timeout is not None:
        parameters["timeout"] = timeout
    return BedrockProvider(client=client).stream(**parameters)


def _drain_rejected(stream, code="provider_rejected"):
    from mojo.helpers.llm_providers.base import ProviderError

    seen = []
    try:
        for event in stream:
            seen.append(event)
        assert False, f"the stream should have failed with {code}, got {seen}"
    except ProviderError as err:
        assert err.code == code, f"stream failure must use {code}, got {err.code}"
        assert "secret" not in str(err), f"raw provider text crossed the adapter: {err}"
        return err, seen


@th.django_unit_test()
def test_stream_yields_ordered_text_deltas_then_one_validated_final(opts):
    client = _StreamClient()
    events = list(_stream(client, system="Be brief", reasoning_effort="low"))
    deltas = [event["text"] for event in events if event["type"] == "text_delta"]
    assert deltas == ["Hel", "lo", " there"], f"text deltas must arrive in provider order: {events}"
    assert [event["type"] for event in events] == ["text_delta"] * 3 + ["final"], \
        f"the stream must end with exactly one final record: {events}"
    final = events[-1]["response"]
    assert final == {"id": "bedrock-stream-1", "type": "message", "role": "assistant",
                     "provider": "bedrock", "model": MODEL,
                     "content": [{"type": "text", "text": "Hello there"}],
                     "stop_reason": "end_turn", "usage": USAGE}, \
        f"the final record must be exactly the call() response shape: {final}"
    assert "REDACTED" not in repr(events) and "private chain" not in repr(events), \
        "reasoning content must never be yielded"
    request = client.calls[0]
    assert request["system"] == [{"text": "Be brief"}] and "toolConfig" not in request \
        and request["additionalModelRequestFields"]["reasoningConfig"][
            "maxReasoningEffort"] == "low", \
        f"converse_stream must receive the same validated request as converse: {request}"
    assert client.stream.closed, "a finished stream must release its connection"


@th.django_unit_test()
def test_stream_tool_request_arrives_only_in_the_final_record(opts):
    client = _StreamClient(_stream_events(
        parts=("Let me check.",), stop="tool_use", tool_input=['{"id"', ': 42}']))
    events = list(_stream(client, tools=TOOLS))
    assert [event["type"] for event in events] == ["text_delta", "final"], \
        f"tool requests must not be handed out before the answer validates: {events}"
    final = events[-1]["response"]
    assert final["stop_reason"] == "tool_use" and final["content"] == [
        {"type": "text", "text": "Let me check."},
        {"type": "tool_use", "id": "tooluse_abc-1", "name": "lookup_item",
         "input": {"id": 42}}], f"the assembled tool request is wrong: {final}"
    spec = client.calls[0]["toolConfig"]
    assert spec == {"tools": [{"toolSpec": {
        "name": "lookup_item", "description": "Look up a board item",
        "inputSchema": {"json": TOOLS[0]["input_schema"]}}}]}, \
        f"declared tools must translate to a Bedrock toolConfig: {spec}"
    empty = _StreamClient(_stream_events(parts=(), stop="tool_use", tool_input=[]))
    final = list(_stream(empty, tools=TOOLS))[-1]["response"]
    assert final["content"] == [{"type": "tool_use", "id": "tooluse_abc-1",
                                 "name": "lookup_item", "input": {}}], \
        f"a tool request with no input must carry an empty object: {final}"


@th.django_unit_test()
def test_plain_tool_round_trip_translates_requests_and_results(opts):
    from mojo.helpers.llm_providers.bedrock import BedrockProvider

    response = _response()
    response["stopReason"] = "tool_use"
    response["output"]["message"]["content"] = [
        {"text": "Checking."},
        {"toolUse": {"toolUseId": "tooluse_1", "name": "lookup_item", "input": {"id": 7}}}]
    client = _Client(response)
    adapter = BedrockProvider(client=client)
    first = adapter.call(MESSAGES, MODEL, 32, tools=TOOLS)
    assert first["stop_reason"] == "tool_use" and first["content"][-1] == {
        "type": "tool_use", "id": "tooluse_1", "name": "lookup_item", "input": {"id": 7}}, \
        f"a Bedrock tool request must come back as a tool_use block: {first}"
    transcript = MESSAGES + [
        {"role": "assistant", "content": first["content"]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tooluse_1",
                                      "content": "not found", "is_error": True}]}]
    client.response = _response()
    second = adapter.call(transcript, MODEL, 32, tools=TOOLS)
    assert second["stop_reason"] == "end_turn" and second["content"] == [
        {"type": "text", "text": "Final answer"}], f"the answer after a tool round is wrong: {second}"
    wire = client.calls[1]["messages"]
    assert wire[1] == {"role": "assistant", "content": [
        {"text": "Checking."},
        {"toolUse": {"toolUseId": "tooluse_1", "name": "lookup_item", "input": {"id": 7}}}]} \
        and wire[2] == {"role": "user", "content": [{"toolResult": {
            "toolUseId": "tooluse_1", "content": [{"text": "not found"}], "status": "error"}}]}, \
        f"tool turns must translate to Bedrock toolUse/toolResult blocks: {wire}"
    assert "toolConfig" in client.calls[1], "a tool transcript must carry its toolConfig"


@th.django_unit_test()
def test_invalid_tools_and_tool_transcripts_never_reach_bedrock(opts):
    use = {"type": "tool_use", "id": "tooluse_1", "name": "lookup_item", "input": {"id": 1}}
    result = {"type": "tool_result", "tool_use_id": "tooluse_1", "content": "ok"}
    asked = [{"role": "user", "content": "Hi"}, {"role": "assistant", "content": [use]}]
    cases = [
        {"tools": [dict(TOOLS[0], cache_control={"type": "ephemeral"})]},
        {"tools": [dict(TOOLS[0], name="bad name")]},
        {"tools": [dict(TOOLS[0], description=" ")]},
        {"tools": [dict(TOOLS[0], input_schema={"type": "array"})]},
        {"tools": [dict(TOOLS[0], input_schema={"type": "object", "x": float("nan")})]},
        {"tools": TOOLS + TOOLS},
        {"tools": [dict(TOOLS[0], name=f"tool_{n}") for n in range(65)]},
        {"tools": None, "messages": asked + [{"role": "user", "content": [result]}]},
        {"tools": TOOLS, "messages": asked},
        {"tools": TOOLS, "messages": asked + [{"role": "user", "content": "no answer"}]},
        {"tools": TOOLS, "messages": asked + [{"role": "user", "content": [
            dict(result, tool_use_id="tooluse_2")]}]},
        {"tools": TOOLS, "messages": asked + [{"role": "user", "content": [result, result]}]},
        {"tools": TOOLS, "messages": asked + [{"role": "user", "content": [
            dict(result, content=" ")]}]},
        {"tools": TOOLS, "messages": asked + [{"role": "user", "content": [
            dict(result, is_error="yes")]}]},
        {"tools": TOOLS, "messages": [{"role": "user", "content": "Hi"},
                                      {"role": "assistant", "content": [
                                          dict(use, name="not_declared")]}]},
        {"tools": TOOLS, "messages": [{"role": "user", "content": "Hi"},
                                      {"role": "assistant", "content": [
                                          dict(use, id="bad id")]}]},
        {"tools": TOOLS, "messages": [{"role": "user", "content": "Hi"},
                                      {"role": "assistant", "content": [
                                          dict(use, input=["not", "an", "object"])]}]},
        {"tools": TOOLS, "messages": [{"role": "user", "content": [use]}]},
    ]
    for kwargs in cases:
        client = _Client()
        _call_rejected(client, **kwargs)
        assert client.calls == [], f"an invalid tool request reached Converse: {kwargs}"
        streamer = _StreamClient()
        try:
            _stream(streamer, **kwargs)
            assert False, f"an invalid streamed tool request was dispatched: {kwargs}"
        except Exception as err:
            assert getattr(err, "code", None) == "provider_rejected", \
                f"invalid streamed tool input must be provider_rejected, got {err!r}"
        assert streamer.calls == [], f"an invalid tool request reached ConverseStream: {kwargs}"


@th.django_unit_test()
def test_unusable_tool_answers_are_rejected_with_their_usage(opts):
    for content, stop in (
            ([{"toolUse": {"toolUseId": "t1", "name": "not_declared", "input": {}}}], "tool_use"),
            ([{"toolUse": {"toolUseId": "t1", "name": "lookup_item", "input": "{}"}}], "tool_use"),
            ([{"toolUse": {"toolUseId": "t1", "name": "lookup_item", "input": {},
                           "type": "server_tool_use"}}], "tool_use"),
            ([{"text": "Visible"}], "tool_use"),
            ([{"text": "Visible"}, {"toolUse": {"toolUseId": "t1", "name": "lookup_item",
                                                "input": {}}}], "end_turn"),
            ([{"text": "Visible"}], "malformed_tool_use")):
        response = _response()
        response["stopReason"] = stop
        response["output"]["message"]["content"] = content
        client = _Client(response)
        _call_rejected(client, expected_usage=USAGE, tools=TOOLS)
    response = _response()
    response["stopReason"] = "tool_use"
    response["output"]["message"]["content"] = [
        {"toolUse": {"toolUseId": "t1", "name": "lookup_item", "input": {"id": float("nan")}}}]
    _call_rejected(_Client(response), expected_usage=USAGE, tools=TOOLS)
    for pieces in (['{"id": 1'], ['[1, 2]'], ['"text"'], ['{"id": NaN}'], ['{"id": Infinity}']):
        stream = _stream(_StreamClient(_stream_events(stop="tool_use", tool_input=pieces)),
                         tools=TOOLS)
        _drain_rejected(stream)


@th.django_unit_test()
def test_stream_failures_reduce_to_safe_codes_and_release_the_connection(opts):
    from botocore.exceptions import EventStreamError
    from urllib3.exceptions import ProtocolError, ReadTimeoutError

    delta = {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "Partial"}}}
    cases = [
        (EventStreamError({"Error": {"Code": "throttlingException", "Message": "secret"}},
                          "ConverseStream"), "provider_rate_limited"),
        (EventStreamError({"Error": {"Code": "modelStreamErrorException",
                                     "Message": "secret"}}, "ConverseStream"),
         "provider_unavailable"),
        (EventStreamError({"Error": {"Code": "validationException", "Message": "secret"}},
                          "ConverseStream"), "provider_rejected"),
        (ReadTimeoutError(None, "https://secret.invalid", "secret read timeout"),
         "provider_timeout"),
        (ProtocolError("secret connection reset"), "provider_unavailable"),
        ({"throttlingException": {"message": "secret"}}, "provider_rate_limited"),
        (RuntimeError("secret raw exception"), "provider_failed"),
    ]
    for failure, code in cases:
        client = _StreamClient([{"messageStart": {"role": "assistant"}}, delta, failure])
        err, seen = _drain_rejected(_stream(client), code)
        assert [event["text"] for event in seen] == ["Partial"], \
            f"text before a mid-stream failure must already have streamed: {seen}"
        assert err.usage is None and err.request_id == "bedrock-stream-1", \
            f"mid-stream failure invented usage or lost the request id: {err.usage}/{err.request_id}"
        assert client.stream.closed, f"a failed stream must release its connection ({code})"
    for events in (_stream_events(usage=False), _stream_events()[:-2] + _stream_events()[-1:],
                   _stream_events(stop="content_filtered")):
        _drain_rejected(_stream(_StreamClient(events)))
    over = _stream_events()
    over[-1]["metadata"]["usage"]["outputTokens"] = 33
    err, _ = _drain_rejected(_stream(_StreamClient(over)))
    assert err.usage == dict(USAGE, output_tokens=33), \
        f"an over-cap streamed answer must keep its billed usage: {err.usage}"


@th.django_unit_test()
def test_stream_deadline_and_interrupt_end_a_blocked_read(opts):
    from mojo.helpers.llm_providers.base import ProviderError

    delta = {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "Partial"}}}
    client = _StreamClient([{"messageStart": {"role": "assistant"}}, delta, "block"])
    stream = _stream(client, timeout=0.2)
    assert next(stream)["text"] == "Partial", "the first delta must stream before the deadline"
    try:
        next(stream)
        assert False, "a read blocked past the deadline must fail"
    except ProviderError as err:
        assert err.code == "provider_timeout", f"the deadline must be provider_timeout, got {err.code}"
        assert err.caller_stalled is False, \
            "a deadline hit inside a blocked read is the provider's slowness"
    assert client.stream._raw_stream.shutdowns >= 1 and client.stream.closed, \
        "the deadline must shut the socket down and release the connection"

    import time
    client = _StreamClient([{"messageStart": {"role": "assistant"}}, delta, delta])
    stream = _stream(client, timeout=0.2)
    next(stream)
    time.sleep(0.35)
    try:
        next(stream)
        assert False, "a stream held past its deadline by the caller must fail"
    except ProviderError as err:
        assert err.code == "provider_timeout" and err.caller_stalled is True, \
            "a deadline passed between reads must be marked as the caller's stall"

    client = _StreamClient([{"messageStart": {"role": "assistant"}}, delta, "block"])
    stream = _stream(client, timeout=30)
    next(stream)
    stream.interrupt()
    try:
        next(stream)
        assert False, "an interrupted stream must stop"
    except StopIteration:
        pass
    assert client.stream._raw_stream.shutdowns == 1 and client.stream.closed, \
        "interrupt() must shut the socket down without waiting on the reader"


@th.django_unit_test()
def test_stream_dispatch_errors_raise_before_any_event(opts):
    from botocore.exceptions import ClientError

    error = ClientError({"Error": {"Code": "ThrottlingException", "Message": "secret"},
                         "ResponseMetadata": {"RequestId": "stream-request",
                                              "HTTPStatusCode": 429}}, "ConverseStream")
    client = _StreamClient(error=error)
    try:
        _stream(client)
        assert False, "a refused converse_stream must raise from stream() itself"
    except Exception as err:
        assert getattr(err, "code", None) == "provider_rate_limited" \
            and "secret" not in str(err), f"dispatch error lost its safe code: {err!r}"
    assert len(client.calls) == 1, "a dispatch failure must not retry"


@th.django_unit_test()
def test_check_request_validates_without_any_sdk_work(opts):
    from mojo.helpers.llm_providers.base import ProviderError
    from mojo.helpers.llm_providers.bedrock import BedrockProvider

    made = []
    adapter = BedrockProvider(region="us-east-1",
                              client_factory=lambda *args, **kwargs: made.append(args))
    adapter.check_request(MESSAGES, MODEL, 32, tools=TOOLS, timeout=10)
    asked = [{"role": "user", "content": "Hi"}, {"role": "assistant", "content": [
        {"type": "tool_use", "id": "t1", "name": "lookup_item", "input": {}}]}]
    for kwargs in ({"messages": asked, "tools": TOOLS}, {"tools": [{"name": "x"}]},
                   {"max_tokens": 0}, {"timeout": 0}):
        values = {"messages": MESSAGES, "model": MODEL, "max_tokens": 32, "timeout": 10}
        values.update(kwargs)
        try:
            adapter.check_request(**values)
            assert False, f"check_request accepted an invalid request: {kwargs}"
        except ProviderError as err:
            assert err.code == "provider_rejected", f"invalid request used {err.code}"
    assert made == [], "check_request must never construct an AWS client"


def _png():
    import io
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (4, 3), (10, 120, 200)).save(out, format="PNG")
    return out.getvalue()


@th.django_unit_test()
def test_image_blocks_become_converse_bytes(opts):
    from mojo.helpers.llm_images import image_block
    from mojo.helpers.llm_providers.bedrock import BedrockProvider

    raw = _png()
    client = _Client()
    messages = [{"role": "user", "content": [
        image_block(raw, "image/png"), {"type": "text", "text": "Describe this screenshot."}]}]
    result = BedrockProvider(client=client).call(messages, MODEL, 32)
    sent = client.calls[0]["messages"]
    assert sent == [{"role": "user", "content": [
        {"image": {"format": "png", "source": {"bytes": raw}}},
        {"text": "Describe this screenshot."}]}], \
        "an image block must reach Converse as raw bytes in an ImageBlock, then the text"
    assert result["content"] == [{"type": "text", "text": "Final answer"}], \
        f"image requests must normalize text output as before, got {result['content']}"


@th.django_unit_test()
def test_text_only_requests_are_unchanged_by_image_support(opts):
    from mojo.helpers.llm_providers.bedrock import BedrockProvider

    client = _Client()
    BedrockProvider(client=client).call(
        [{"role": "user", "content": "Hello"},
         {"role": "assistant", "content": [{"type": "text", "text": "Hi"}]},
         {"role": "user", "content": "Again"}],
        MODEL, 32, system="Be brief")
    assert client.calls == [{
        "modelId": MODEL,
        "messages": [{"role": "user", "content": [{"text": "Hello"}]},
                     {"role": "assistant", "content": [{"text": "Hi"}]},
                     {"role": "user", "content": [{"text": "Again"}]}],
        "inferenceConfig": {"maxTokens": 32},
        "system": [{"text": "Be brief"}]}], \
        f"text-only Converse requests must be byte-identical, got {client.calls}"


@th.django_unit_test()
def test_invalid_or_misplaced_images_never_reach_converse(opts):
    import base64
    from mojo.helpers.llm_images import image_block

    block = image_block(_png(), "image/png")
    forged = {"type": "image", "source": {
        "type": "base64", "media_type": "image/jpeg",
        "data": block["source"]["data"]}}
    secret = base64.b64encode(b"\x89PNG\r\n\x1a\nPRIVATE").decode("ascii")
    broken = {"type": "image", "source": dict(block["source"], data=secret)}
    cases = [
        {"messages": [{"role": "user", "content": [forged]}]},
        {"messages": [{"role": "user", "content": [broken]}]},
        {"messages": [{"role": "assistant", "content": [block]}]},
        {"system": [block]},
    ]
    for kwargs in cases:
        client = _Client()
        err = _call_rejected(client, **kwargs)
        assert client.calls == [], f"an invalid image reached Converse: {list(kwargs)}"
        assert secret not in f"{err!r} {err}" and "PRIVATE" not in f"{err!r} {err}", \
            "a rejected image must not be echoed in the provider error"


@th.django_unit_test()
def test_image_requests_use_the_route_region(opts):
    from mojo.helpers.llm_images import image_block
    from mojo.helpers.llm_providers.bedrock import BedrockProvider

    clients = []

    def factory(service, **kwargs):
        clients.append((service, kwargs["region"]))
        return _Client()

    messages = [{"role": "user", "content": [image_block(_png(), "image/png")]}]
    BedrockProvider(region="eu-west-1", client_factory=factory).call(messages, MODEL, 32)
    assert clients == [("bedrock-runtime", "eu-west-1")], \
        f"an image request must use only the route's region, got {clients}"


@th.django_unit_test()
def test_image_blocks_stream_and_survive_a_tool_round(opts):
    from mojo.helpers.llm_images import image_block
    from mojo.helpers.llm_providers.bedrock import BedrockProvider

    raw = _png()
    image = image_block(raw, "image/png")
    asked = [{"role": "user", "content": [image, {"type": "text", "text": "What is broken?"}]}]
    wire_image = {"image": {"format": "png", "source": {"bytes": raw}}}

    client = _StreamClient()
    events = list(_stream(client, messages=asked))
    assert events[-1]["type"] == "final" \
        and events[-1]["response"]["content"] == [{"type": "text", "text": "Hello there"}], \
        f"a streamed image request must answer like a text one: {events}"
    assert client.calls[0]["messages"] == [{"role": "user", "content": [
        wire_image, {"text": "What is broken?"}]}], \
        f"converse_stream must receive the image as raw bytes: {client.calls[0]['messages']}"

    use = {"type": "tool_use", "id": "tooluse_1", "name": "lookup_item", "input": {"id": 7}}
    transcript = asked + [
        {"role": "assistant", "content": [use]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tooluse_1",
                                      "content": "item 7 is open"}]}]
    client = _Client()
    result = BedrockProvider(client=client).call(transcript, MODEL, 32, tools=TOOLS)
    assert result["content"] == [{"type": "text", "text": "Final answer"}], \
        f"a tool round after an image turn must answer normally: {result}"
    wire = client.calls[0]["messages"]
    assert wire[0] == {"role": "user", "content": [wire_image, {"text": "What is broken?"}]} \
        and wire[2] == {"role": "user", "content": [{"toolResult": {
            "toolUseId": "tooluse_1", "content": [{"text": "item 7 is open"}]}}]} \
        and "toolConfig" in client.calls[0], \
        f"the image turn must be resent unchanged inside the tool loop: {wire}"
    streamer = _StreamClient()
    list(_stream(streamer, messages=transcript, tools=TOOLS))
    assert streamer.calls[0]["messages"] == wire, \
        "stream() and call() must translate the same image tool transcript identically"


@th.django_unit_test()
def test_images_inside_tool_results_never_reach_bedrock(opts):
    from mojo.helpers.llm_images import image_block

    image = image_block(_png(), "image/png")
    use = {"type": "tool_use", "id": "tooluse_1", "name": "lookup_item", "input": {"id": 7}}
    transcript = [{"role": "user", "content": "Hi"},
                  {"role": "assistant", "content": [use]},
                  {"role": "user", "content": [{"type": "tool_result",
                                                "tool_use_id": "tooluse_1",
                                                "content": [image]}]}]
    client = _Client()
    _call_rejected(client, messages=transcript, tools=TOOLS)
    assert client.calls == [], "an image inside a tool result must not reach Converse"


@th.django_unit_test()
def test_validation_exception_keeps_aws_code_and_message(opts):
    """#10589: a Bedrock refusal carries AWS's own reason, bounded, never the request."""
    from botocore import exceptions as be
    from mojo.helpers.llm_providers.base import ProviderError
    from mojo.helpers.llm_providers.bedrock import BedrockProvider

    message = "The model returned the following errors: toolConfig.tools.0: invalid"
    for text, expected in ((message, f"ValidationException: {message}"),
                           ("y" * 2000, ("ValidationException: " + "y" * 2000)[:512])):
        response = {"Error": {"Code": "ValidationException", "Message": text},
                    "ResponseMetadata": {"RequestId": "bedrock-10589",
                                         "HTTPStatusCode": 400}}
        client = _Client(error=be.ClientError(response, "Converse"))
        try:
            BedrockProvider(client=client).call(MESSAGES, MODEL, 32)
            assert False, "a ValidationException must raise ProviderError"
        except ProviderError as err:
            assert err.code == "provider_rejected" and err.request_id == "bedrock-10589", \
                f"a ValidationException must stay provider_rejected with its id, got {err.code}"
            assert err.detail == expected, \
                f"the AWS code and message must be kept, cut to 512, got {err.detail[:80]!r} ({len(err.detail)})"
            assert str(err) == "provider_rejected" and "Hello" not in err.detail, \
                "str(err) must stay the code and the request must never reach the detail"
