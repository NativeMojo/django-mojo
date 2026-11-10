"""Bounded Nova 2 Lite text, image and tool calls through AWS Bedrock Converse."""

import json
import math
import re
import threading
import time

from .base import ProviderAdapter, ProviderError, validated_usage


NOVA_2_LITE_PROFILES = frozenset(
    f"{prefix}.amazon.nova-2-lite-v1:0" for prefix in ("us", "eu", "jp", "global"))
REASONING_EFFORTS = frozenset({"off", "low", "medium"})
MAX_OUTPUT_TOKENS = 65000
MAX_TEXT_BYTES = 1024 * 1024
MAX_TOOLS = 64
MAX_TOOL_INPUT_BYTES = 256 * 1024
MAX_CONTENT_BLOCKS = 128
REGION_PATTERN = re.compile(r"^[a-z]{2}(?:-[a-z]+)+-[1-9][0-9]*$")
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
TOOL_NAME_PATTERN = re.compile(r"[a-zA-Z0-9_-]{1,64}")
TOOL_USE_ID_PATTERN = re.compile(r"[a-zA-Z0-9_.:-]{1,64}")
# Exception events a Converse stream can carry, reduced like their HTTP twins.
STREAM_ERRORS = {
    "internalServerException": "provider_unavailable",
    "modelStreamErrorException": "provider_unavailable",
    "serviceUnavailableException": "provider_unavailable",
    "throttlingException": "provider_rate_limited",
    "validationException": "provider_rejected",
}


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


def _image_block(block):
    """A validated canonical image block as Converse raw bytes."""
    from mojo.helpers.llm_images import ImageInputError, decode_image_block
    try:
        fmt, raw = decode_image_block(block)
    except ImageInputError:
        raise ProviderError("provider_rejected") from None
    return {"image": {"format": fmt, "source": {"bytes": raw}}}


def _json_object(value):
    """A JSON-serializable dict, or rejection; the wire format is JSON."""
    if not isinstance(value, dict):
        raise ProviderError("provider_rejected")
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise ProviderError("provider_rejected") from None
    return value


def _no_constant(name):
    # json.loads accepts NaN and Infinity; the echoed input would not.
    raise ValueError(name)


def _tool_config(tools):
    """Translate caller-declared tools; returns (toolConfig or None, names)."""
    if tools is None or tools == []:
        return None, frozenset()
    if not isinstance(tools, list) or len(tools) > MAX_TOOLS:
        raise ProviderError("provider_rejected")
    names = set()
    specs = []
    for tool in tools:
        if not isinstance(tool, dict) or set(tool) != {"name", "description", "input_schema"}:
            raise ProviderError("provider_rejected")
        name = tool["name"]
        description = tool["description"]
        schema = tool["input_schema"]
        if not isinstance(name, str) or not TOOL_NAME_PATTERN.fullmatch(name) \
                or name in names or not isinstance(description, str) \
                or not description.strip() or not isinstance(schema, dict) \
                or schema.get("type") != "object":
            raise ProviderError("provider_rejected")
        names.add(name)
        specs.append({"toolSpec": {"name": name, "description": description,
                                   "inputSchema": {"json": _json_object(schema)}}})
    return {"tools": specs}, frozenset(names)


def _message_blocks(role, content, tool_names):
    """Translate one message; returns (blocks, tool_use ids, tool_result ids)."""
    if isinstance(content, str):
        return _text_blocks(content), [], []
    if not isinstance(content, list) or not content:
        raise ProviderError("provider_rejected")
    blocks, used, answered = [], [], []
    for block in content:
        kind = block.get("type") if isinstance(block, dict) else None
        if kind == "text":
            blocks.extend(_text_blocks([block]))
        elif kind == "image" and role == "user":
            blocks.append(_image_block(block))
        elif kind == "tool_use" and role == "assistant" and tool_names:
            if set(block) != {"type", "id", "name", "input"} \
                    or not isinstance(block["id"], str) \
                    or not TOOL_USE_ID_PATTERN.fullmatch(block["id"]) \
                    or block["name"] not in tool_names:
                raise ProviderError("provider_rejected")
            blocks.append({"toolUse": {"toolUseId": block["id"], "name": block["name"],
                                       "input": _json_object(block["input"])}})
            used.append(block["id"])
        elif kind == "tool_result" and role == "user" and tool_names:
            if set(block) - {"is_error"} != {"type", "tool_use_id", "content"} \
                    or not isinstance(block["tool_use_id"], str) \
                    or not TOOL_USE_ID_PATTERN.fullmatch(block["tool_use_id"]) \
                    or not isinstance(block.get("is_error", False), bool):
                raise ProviderError("provider_rejected")
            result = {"toolUseId": block["tool_use_id"],
                      "content": _text_blocks(block["content"])}
            if block.get("is_error"):
                result["status"] = "error"
            blocks.append({"toolResult": result})
            answered.append(block["tool_use_id"])
        else:
            raise ProviderError("provider_rejected")
    return blocks, used, answered


def _translate_messages(messages, tool_names):
    """Every tool request is answered by exactly the next user message."""
    if not isinstance(messages, list) or not messages:
        raise ProviderError("provider_rejected")
    translated = []
    pending = []
    for message in messages:
        if not isinstance(message, dict) or set(message) != {"role", "content"} \
                or message.get("role") not in ("user", "assistant"):
            raise ProviderError("provider_rejected")
        blocks, used, answered = _message_blocks(
            message["role"], message["content"], tool_names)
        if message["role"] == "user":
            if len(answered) != len(set(answered)) or set(answered) != set(pending):
                raise ProviderError("provider_rejected")
        elif pending:
            raise ProviderError("provider_rejected")
        if len(used) != len(set(used)):
            raise ProviderError("provider_rejected")
        pending = used
        translated.append({"role": message["role"], "content": blocks})
    if pending:
        raise ProviderError("provider_rejected")
    return translated


def _finish(content, stop_reason, usage, request_id, model, max_tokens, tool_names,
            provider):
    """Validate one complete answer, streamed or not, into the call() shape."""
    def reject():
        # Retain validated billing evidence even when the answer is unusable.
        raise ProviderError("provider_rejected", request_id, usage=usage)

    if usage is None or not request_id or usage["output_tokens"] > max_tokens:
        reject()
    # Whitespace-only text is dropped so the caller can echo the content back.
    content = [block for block in content
               if block["type"] != "text" or block["text"].strip()]
    has_text = any(block["type"] == "text" for block in content)
    has_tool = any(block["type"] == "tool_use" for block in content)
    if stop_reason == "tool_use":
        if not tool_names or not has_tool:
            reject()
    elif stop_reason not in ("end_turn", "max_tokens") or has_tool or not has_text:
        reject()
    return {"id": request_id, "type": "message", "role": "assistant",
            "provider": provider, "model": model, "content": content,
            "stop_reason": stop_reason, "usage": usage}


class BedrockStream:
    """One Converse stream as facade events; the guard owns all accounting.

    Yields ``{"type": "text_delta", "text"}`` events, then one
    ``{"type": "final", "response"}`` holding the validated call() shape.
    Tool requests arrive only in that final response, after the whole answer
    validated. Reasoning content is never yielded. ``usage`` and
    ``request_id`` remain readable for settlement after a failure or cancel.
    """

    def __init__(self, response, model, max_tokens, tool_names, timeout, raise_safe):
        self.request_id = _request_id(response)
        self.usage = None
        self._stream = response.get("stream") if isinstance(response, dict) else None
        self._events = iter(self._stream) if self._stream is not None else None
        self._model = model
        self._max_tokens = max_tokens
        self._tool_names = tool_names
        self._deadline = time.monotonic() + timeout
        self._raise_safe = raise_safe
        self._blocks = {}
        self._stop_reason = None
        self._text_bytes = 0
        self._done = False
        self._interrupted = False
        self._timed_out = False
        self._reading = False
        self._caller_stalled = False
        self.events_seen = 0
        # Socket read timeouts bound each read, not the whole answer. The
        # timer ends a stream that keeps trickling past the guard's timeout.
        self._timer = threading.Timer(timeout, self._expire)
        self._timer.daemon = True
        if self._events is not None:
            self._timer.start()

    def __iter__(self):
        return self

    def _expire(self):
        # A deadline passed between reads is the caller's stall, not AWS's.
        self._caller_stalled = not self._reading
        self._timed_out = True
        self.interrupt()

    def interrupt(self):
        """Unblock a read in progress from another thread; never blocks.

        botocore exposes no public way to do this. urllib3's shutdown()
        stops the socket without taking the lock the reading thread holds,
        which close() would wait on.
        """
        self._interrupted = True
        raw = getattr(self._stream, "_raw_stream", None)
        shutdown = getattr(raw, "shutdown", None)
        if shutdown is not None:
            try:
                shutdown()
            except Exception:
                pass

    def close(self):
        """Release the connection. Call it from the reading thread only."""
        self._done = True
        self._timer.cancel()
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass

    def _reject(self):
        self.close()
        raise ProviderError("provider_rejected", self.request_id, usage=self.usage)

    def _timeout(self, caller_stalled):
        self.close()
        error = ProviderError("provider_timeout", self.request_id, usage=self.usage)
        error.caller_stalled = caller_stalled
        raise error

    def _read_failed(self, err):
        """A failure while reading the stream, reduced to one safe code."""
        import socket
        from botocore import exceptions as be
        from urllib3 import exceptions as ue

        if self._timed_out:
            self._timeout(self._caller_stalled)
        self.close()
        if isinstance(err, (be.BotoCoreError, be.ClientError)):
            self._raise_safe(err, self.request_id)
        # A running stream reads urllib3 directly; botocore wraps none of it.
        if isinstance(err, (ue.ReadTimeoutError, socket.timeout)):
            raise ProviderError("provider_timeout", self.request_id, usage=self.usage)
        if isinstance(err, (ue.HTTPError, OSError)):
            raise ProviderError("provider_unavailable", self.request_id, usage=self.usage)
        self._raise_safe(err, self.request_id)

    def __next__(self):
        while True:
            if self._done:
                raise StopIteration
            if self._timed_out or time.monotonic() > self._deadline:
                self._timeout(self._caller_stalled if self._timed_out else True)
            self._reading = True
            try:
                event = next(self._events)
            except StopIteration:
                if self._timed_out:
                    self._read_failed(None)
                self._done = True
                return {"type": "final", "response": self._final()}
            except ProviderError:
                raise
            except Exception as err:
                if self._interrupted and not self._timed_out:
                    self.close()
                    raise StopIteration from None
                self._read_failed(err)
            finally:
                self._reading = False
            self.events_seen += 1
            result = self._handle(event)
            if result is not None:
                return result

    def _index(self, body):
        index = body.get("contentBlockIndex")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            self._reject()
        return index

    def _block(self, index, kind):
        block = self._blocks.get(index)
        if block is None:
            if len(self._blocks) >= MAX_CONTENT_BLOCKS:
                self._reject()
            block = self._blocks[index] = {"type": kind, "parts": [], "bytes": 0,
                                           "closed": False}
        if block["type"] != kind or block["closed"]:
            self._reject()
        return block

    def _handle(self, event):
        if not isinstance(event, dict) or len(event) != 1:
            self._reject()
        kind, body = next(iter(event.items()))
        if not isinstance(body, dict):
            self._reject()
        if kind in STREAM_ERRORS:
            self.close()
            message = body.get("message")
            message = message if isinstance(message, str) else ""
            name = kind[:1].upper() + kind[1:]
            raise ProviderError(STREAM_ERRORS[kind], self.request_id, usage=self.usage,
                                detail=f"{name}: {message}" if message else name)
        if kind == "messageStart":
            if body.get("role") != "assistant":
                self._reject()
        elif kind == "contentBlockStart":
            index = self._index(body)
            start = body.get("start")
            tool = start.get("toolUse") if isinstance(start, dict) else None
            if index in self._blocks or not isinstance(start, dict) \
                    or set(start) != {"toolUse"} \
                    or not isinstance(tool, dict) or set(tool) != {"toolUseId", "name"} \
                    or not isinstance(tool["toolUseId"], str) \
                    or not TOOL_USE_ID_PATTERN.fullmatch(tool["toolUseId"]) \
                    or tool["name"] not in self._tool_names:
                self._reject()
            block = self._block(index, "tool_use")
            block.update(id=tool["toolUseId"], name=tool["name"])
        elif kind == "contentBlockDelta":
            return self._delta(self._index(body), body.get("delta"))
        elif kind == "contentBlockStop":
            block = self._blocks.get(self._index(body))
            if block is None:
                # A text block that never carried a delta: nothing to close.
                return None
            if block["closed"]:
                self._reject()
            block["closed"] = True
            if block["type"] == "tool_use":
                raw = "".join(block["parts"])
                try:
                    value = json.loads(raw, parse_constant=_no_constant) \
                        if raw.strip() else {}
                except (ValueError, RecursionError):
                    self._reject()
                if not isinstance(value, dict):
                    self._reject()
                block["input"] = value
        elif kind == "messageStop":
            stop_reason = body.get("stopReason")
            if not isinstance(stop_reason, str) or self._stop_reason is not None:
                self._reject()
            self._stop_reason = stop_reason
        elif kind == "metadata":
            if self.usage is not None:
                self._reject()
            self.usage = _usage(body)
        # Unknown top-level events carry nothing this adapter emits.
        return None

    def _delta(self, index, delta):
        if not isinstance(delta, dict) or len(delta) != 1:
            self._reject()
        kind, value = next(iter(delta.items()))
        if kind == "reasoningContent":
            if not isinstance(value, dict):
                self._reject()
            self._block(index, "reasoning")
            return None
        if kind == "text":
            block = self._block(index, "text")
            if not isinstance(value, str):
                self._reject()
            try:
                self._text_bytes += len(value.encode("utf-8"))
            except UnicodeError:
                self._reject()
            if self._text_bytes > MAX_TEXT_BYTES:
                self._reject()
            block["parts"].append(value)
            return {"type": "text_delta", "text": value} if value else None
        if kind == "toolUse":
            block = self._blocks.get(index)
            if block is None or block["type"] != "tool_use" or block["closed"] \
                    or not isinstance(value, dict) or set(value) != {"input"} \
                    or not isinstance(value["input"], str):
                self._reject()
            block["bytes"] += len(value["input"].encode("utf-8", "replace"))
            if block["bytes"] > MAX_TOOL_INPUT_BYTES:
                self._reject()
            block["parts"].append(value["input"])
            return None
        self._reject()

    def _final(self):
        content = []
        for index in sorted(self._blocks):
            block = self._blocks[index]
            if block["type"] == "text":
                content.append({"type": "text", "text": "".join(block["parts"])})
            elif block["type"] == "tool_use":
                if not block["closed"]:
                    self._reject()
                content.append({"type": "tool_use", "id": block["id"],
                                "name": block["name"], "input": block["input"]})
        if self._stop_reason is None:
            self._reject()
        self.close()
        return _finish(content, self._stop_reason, self.usage, self.request_id,
                       self._model, self._max_tokens, self._tool_names, "bedrock")


class BedrockProvider(ProviderAdapter):
    name = "bedrock"
    capabilities = frozenset({"text", "images", "tools", "stream"})

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

    def _raise_safe(self, err, request_id=""):
        from botocore import exceptions as be

        retry_after = None
        detail = ""
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
            request_id = _request_id(response) or request_id
            error = response.get("Error")
            error = error if isinstance(error, dict) else {}
            metadata = response.get("ResponseMetadata")
            metadata = metadata if isinstance(metadata, dict) else {}
            aws_code = error.get("Code")
            aws_code = aws_code if isinstance(aws_code, str) else ""
            # Stream exception events name their shape: throttlingException.
            aws_code = aws_code[:1].upper() + aws_code[1:]
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
                              "InternalFailure", "ModelStreamErrorException"} or \
                    (isinstance(status, int) and status >= 500) or \
                    (isinstance(original_status, int) and original_status >= 500):
                code = "provider_unavailable"
            else:
                code = "provider_rejected"
            message = error.get("Message")
            message = message if isinstance(message, str) else ""
            detail = f"{aws_code}: {message}" if aws_code and message \
                else (aws_code or message)
            if code == "provider_rate_limited":
                headers = metadata.get("HTTPHeaders")
                headers = headers if isinstance(headers, dict) else {}
                value = headers.get("retry-after")
                if isinstance(value, str) and value.isascii() and value.isdigit() \
                        and len(value) <= 10:
                    retry_after = min(3600, max(1, int(value)))
        else:
            code = "provider_failed"
        raise ProviderError(code, request_id, retry_after=retry_after,
                            detail=detail) from None

    def _request(self, messages, model, max_tokens, system, tools, reasoning_effort,
                 timeout):
        """Validate one request before any SDK work; returns (request, names, timeout)."""
        if not isinstance(model, str) or model not in NOVA_2_LITE_PROFILES \
                or not isinstance(reasoning_effort, str) \
                or reasoning_effort not in REASONING_EFFORTS \
                or isinstance(max_tokens, bool) or not isinstance(max_tokens, int) \
                or not 1 <= max_tokens <= MAX_OUTPUT_TOKENS:
            raise ProviderError("provider_rejected")
        tool_config, tool_names = _tool_config(tools)
        request = {"modelId": model,
                   "messages": _translate_messages(messages, tool_names),
                   "inferenceConfig": {"maxTokens": max_tokens}}
        if system is not None and system != "":
            request["system"] = _text_blocks(system)
        if tool_config is not None:
            request["toolConfig"] = tool_config
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
        return request, tool_names, timeout

    def check_request(self, messages, model, max_tokens, system=None, tools=None,
                      timeout=None, reasoning_effort="off"):
        """Validate a request without any SDK work, so the guard can refuse it
        before reserving budget. Raises ProviderError("provider_rejected")."""
        self._request(messages, model, max_tokens, system, tools, reasoning_effort,
                      timeout)

    def call(self, messages, model, max_tokens, system=None, tools=None,
             cache_enabled=False, timeout=None, reasoning_effort="off"):
        request, tool_names, timeout = self._request(
            messages, model, max_tokens, system, tools, reasoning_effort, timeout)
        try:
            response = self._client(timeout).converse(**request)
        except Exception as err:
            self._raise_safe(err)
        try:
            return self._normalize(response, model, max_tokens, tool_names)
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

    def stream(self, messages, model, max_tokens, system=None, tools=None,
               cache_enabled=False, timeout=None, reasoning_effort="off"):
        """Dispatch converse_stream; errors before the first event raise here."""
        request, tool_names, timeout = self._request(
            messages, model, max_tokens, system, tools, reasoning_effort, timeout)
        started = time.monotonic()
        try:
            response = self._client(timeout).converse_stream(**request)
        except Exception as err:
            self._raise_safe(err)
        remaining = max(0.001, timeout - (time.monotonic() - started))
        stream = BedrockStream(response, model, max_tokens, tool_names, remaining,
                               self._raise_safe)
        if stream._events is None:
            stream.close()
            raise ProviderError("provider_rejected", stream.request_id)
        return stream

    def _normalize(self, response, model, max_tokens, tool_names=frozenset()):
        usage = _usage(response)
        request_id = _request_id(response)

        def reject():
            # Retain validated billing evidence even when the answer is unusable.
            raise ProviderError("provider_rejected", request_id, usage=usage)

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
            if set(block) == {"toolUse"}:
                tool = block["toolUse"]
                if not isinstance(tool, dict) or set(tool) != {"toolUseId", "name", "input"} \
                        or not isinstance(tool["toolUseId"], str) \
                        or not TOOL_USE_ID_PATTERN.fullmatch(tool["toolUseId"]) \
                        or tool["name"] not in tool_names:
                    reject()
                try:
                    _json_object(tool["input"])
                except ProviderError:
                    reject()
                content.append({"type": "tool_use", "id": tool["toolUseId"],
                                "name": tool["name"], "input": tool["input"]})
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
        return _finish(content, response.get("stopReason"), usage, request_id, model,
                       max_tokens, tool_names, self.name)
