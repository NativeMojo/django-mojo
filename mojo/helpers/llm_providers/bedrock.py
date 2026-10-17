"""Bounded Nova 2 Lite text calls through AWS Bedrock Converse."""

import math
import re

from .base import ProviderAdapter, ProviderError, validated_usage


NOVA_2_LITE_PROFILES = frozenset(
    f"{prefix}.amazon.nova-2-lite-v1:0" for prefix in ("us", "eu", "jp", "global"))
REASONING_EFFORTS = frozenset({"off", "low", "medium"})
MAX_OUTPUT_TOKENS = 65000
MAX_TEXT_BYTES = 1024 * 1024
REGION_PATTERN = re.compile(r"^[a-z]{2}(?:-[a-z]+)+-[1-9][0-9]*$")
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _request_id(response):
    metadata = response.get("ResponseMetadata") if isinstance(response, dict) else None
    value = metadata.get("RequestId") if isinstance(metadata, dict) else None
    return value if isinstance(value, str) and REQUEST_ID_PATTERN.fullmatch(value) else ""


def _usage(response):
    raw = response.get("usage") if isinstance(response, dict) else None
    if not isinstance(raw, dict):
        return None
    return validated_usage({
        "input_tokens": raw.get("inputTokens"),
        "output_tokens": raw.get("outputTokens"),
        "cache_read_input_tokens": raw.get("cacheReadInputTokens", 0),
        "cache_creation_input_tokens": raw.get("cacheWriteInputTokens", 0),
    })


def _text_blocks(content):
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    if not isinstance(content, list) or not content:
        raise ProviderError("provider_rejected")
    result = []
    for block in content:
        if not isinstance(block, dict) or set(block) != {"type", "text"} \
                or block.get("type") != "text" \
                or not isinstance(block.get("text"), str) or not block["text"].strip():
            raise ProviderError("provider_rejected")
        result.append({"text": block["text"]})
    return result


class BedrockProvider(ProviderAdapter):
    name = "bedrock"
    capabilities = frozenset({"text"})

    def __init__(self, region=None, client=None, client_factory=None):
        # Construction precedes guard admission; credentials and SDK clients
        # must remain lazy until call(), after the started ledger exists.
        self.region = region
        self.client = client
        self.client_factory = client_factory

    def _client(self, timeout):
        if self.client is not None:
            return self.client
        from botocore.config import Config
        factory = self.client_factory
        if factory is None:
            from mojo.helpers.aws.client import get_client
            factory = get_client
        connect_timeout = min(3.0, timeout / 2)
        config = Config(
            signature_version="v4", ignore_configured_endpoint_urls=True,
            connect_timeout=connect_timeout, read_timeout=timeout - connect_timeout,
            retries={"total_max_attempts": 1, "mode": "standard"})
        # No key, profile, endpoint, or model options originate in the call.
        # get_client leaves credential selection to the existing AWS chain.
        return factory("bedrock-runtime", region=self.region, config=config)

    def _raise_safe(self, err):
        from botocore import exceptions as be

        request_id = ""
        retry_after = None
        if isinstance(err, (be.NoCredentialsError, be.PartialCredentialsError,
                            be.CredentialRetrievalError, be.TokenRetrievalError,
                            be.ProfileNotFound)):
            code = "provider_authentication"
        elif isinstance(err, (be.ConnectTimeoutError, be.ReadTimeoutError)):
            code = "provider_timeout"
        elif isinstance(err, (be.EndpointConnectionError, be.ConnectionClosedError,
                              be.ProxyConnectionError)):
            code = "provider_unavailable"
        elif isinstance(err, be.ClientError):
            response = err.response if isinstance(err.response, dict) else {}
            request_id = _request_id(response)
            error = response.get("Error")
            error = error if isinstance(error, dict) else {}
            metadata = response.get("ResponseMetadata")
            metadata = metadata if isinstance(metadata, dict) else {}
            aws_code = error.get("Code")
            aws_code = aws_code if isinstance(aws_code, str) else ""
            status = metadata.get("HTTPStatusCode", 0)
            original_status = response.get("originalStatusCode", 0)
            if aws_code in {
                    "AccessDeniedException", "UnrecognizedClientException",
                    "InvalidSignatureException", "SignatureDoesNotMatch",
                    "ExpiredTokenException", "InvalidClientTokenId",
                    "IncompleteSignature", "UnauthorizedException"} or status in (401, 403):
                code = "provider_authentication"
            elif aws_code in {
                    "ThrottlingException", "TooManyRequestsException",
                    "ServiceQuotaExceededException", "ModelNotReadyException"} or status == 429:
                code = "provider_rate_limited"
            elif aws_code in {"ModelTimeoutException", "RequestTimeoutException"} or status == 408:
                code = "provider_timeout"
            elif aws_code in {"ServiceUnavailableException", "InternalServerException",
                              "InternalFailure"} or \
                    (isinstance(status, int) and status >= 500) or \
                    (isinstance(original_status, int) and original_status >= 500):
                code = "provider_unavailable"
            else:
                code = "provider_rejected"
            if code == "provider_rate_limited":
                headers = metadata.get("HTTPHeaders")
                headers = headers if isinstance(headers, dict) else {}
                value = headers.get("retry-after")
                if isinstance(value, str) and value.isascii() and value.isdigit() \
                        and len(value) <= 10:
                    retry_after = min(3600, max(1, int(value)))
        else:
            code = "provider_failed"
        raise ProviderError(code, request_id, retry_after=retry_after) from None

    def call(self, messages, model, max_tokens, system=None, tools=None,
             cache_enabled=False, timeout=None, reasoning_effort="off"):
        if not isinstance(model, str) or model not in NOVA_2_LITE_PROFILES \
                or not isinstance(reasoning_effort, str) \
                or reasoning_effort not in REASONING_EFFORTS \
                or isinstance(max_tokens, bool) or not isinstance(max_tokens, int) \
                or not 1 <= max_tokens <= MAX_OUTPUT_TOKENS or tools:
            raise ProviderError("provider_rejected")
        if not isinstance(messages, list) or not messages:
            raise ProviderError("provider_rejected")
        translated = []
        for message in messages:
            if not isinstance(message, dict) or set(message) != {"role", "content"} \
                    or message.get("role") not in ("user", "assistant"):
                raise ProviderError("provider_rejected")
            translated.append({"role": message["role"],
                               "content": _text_blocks(message["content"])})
        request = {"modelId": model, "messages": translated,
                   "inferenceConfig": {"maxTokens": max_tokens}}
        if system is not None and system != "":
            request["system"] = _text_blocks(system)
        if reasoning_effort != "off":
            request["additionalModelRequestFields"] = {"reasoningConfig": {
                "type": "enabled", "maxReasoningEffort": reasoning_effort}}
        timeout = 60 if timeout is None else timeout
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) \
                or timeout <= 0:
            raise ProviderError("provider_rejected")
        try:
            finite_timeout = math.isfinite(timeout)
        except OverflowError:
            finite_timeout = False
        if not finite_timeout:
            raise ProviderError("provider_rejected")
        if self.client is None and (not isinstance(self.region, str)
                                    or not REGION_PATTERN.fullmatch(self.region)):
            raise ProviderError("provider_rejected")
        try:
            response = self._client(timeout).converse(**request)
        except Exception as err:
            self._raise_safe(err)
        try:
            return self._normalize(response, model, max_tokens)
        except ProviderError:
            raise
        except Exception:
            # Malformed provider data must not escape as a guard refusal or
            # raw exception. Preserve any billing evidence already readable.
            usage = None
            request_id = ""
            try:
                usage = _usage(response)
                request_id = _request_id(response)
            except Exception:
                pass
            raise ProviderError("provider_rejected", request_id, usage=usage) from None

    def _normalize(self, response, model, max_tokens):
        usage = _usage(response)
        request_id = _request_id(response)

        def reject():
            # Retain validated billing evidence even when the answer is unusable.
            raise ProviderError("provider_rejected", request_id, usage=usage)

        if usage is None or not request_id or usage["output_tokens"] > max_tokens:
            reject()
        output = response.get("output")
        message = output.get("message") if isinstance(output, dict) else None
        if not isinstance(message, dict) or message.get("role") != "assistant":
            reject()
        blocks = message.get("content")
        if not isinstance(blocks, list) or not blocks:
            reject()
        content = []
        text_bytes = 0
        for block in blocks:
            if not isinstance(block, dict):
                reject()
            if set(block) == {"reasoningContent"}:
                reasoning = block["reasoningContent"]
                if not isinstance(reasoning, dict):
                    reject()
                if set(reasoning) == {"reasoningText"}:
                    text = reasoning["reasoningText"]
                    if not isinstance(text, dict) or "text" not in text \
                            or set(text) - {"text", "signature"} \
                            or not isinstance(text["text"], str) or \
                            ("signature" in text and not isinstance(text["signature"], str)):
                        reject()
                elif set(reasoning) != {"redactedContent"} or \
                        not isinstance(reasoning["redactedContent"], (str, bytes)):
                    reject()
                continue
            if set(block) == {"redactedContent"} and \
                    isinstance(block["redactedContent"], (str, bytes)):
                continue
            if set(block) != {"text"} or not isinstance(block["text"], str):
                reject()
            try:
                text_bytes += len(block["text"].encode("utf-8")) + bool(content)
            except UnicodeError:
                reject()
            if text_bytes > MAX_TEXT_BYTES:
                reject()
            content.append({"type": "text", "text": block["text"]})
        stop_reason = response.get("stopReason")
        if stop_reason not in ("end_turn", "max_tokens") or \
                not any(block["text"].strip() for block in content):
            reject()
        return {"id": request_id, "type": "message", "role": "assistant",
                "provider": self.name, "model": model, "content": content,
                "stop_reason": stop_reason, "usage": usage}
