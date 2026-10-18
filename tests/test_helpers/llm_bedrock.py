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
    assert bedrock.capabilities == frozenset({"text", "images"}), \
        f"the Bedrock adapter must advertise text and images only, got {bedrock.capabilities}"


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
