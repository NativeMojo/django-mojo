"""Bounded Nova 2 Lite and Claude text, image and tool calls through AWS Bedrock Converse."""

import json
import math
import re
import threading
import time

from .base import ProviderAdapter, ProviderError, detail_word, validated_usage


NOVA_2_LITE_PROFILES = frozenset(
    f"{prefix}.amazon.nova-2-lite-v1:0" for prefix in ("us", "eu", "jp", "global"))
# Claude inference profiles, copied from each AWS model card's
# "Programmatic Access" table. Written out in full: never built.
CLAUDE_MODELS = {
    "us.anthropic.claude-haiku-5-5": "haiku",
    "ca.anthropic.claude-haiku-5-5": "haiku",
    "eu.anthropic.claude-haiku-5-5": "haiku",
    "au.anthropic.claude-haiku-5-5": "haiku",
    "jp.anthropic.claude-haiku-5-5": "haiku",
    "global.anthropic.claude-haiku-5-5": "haiku",
    "us.anthropic.claude-sonnet-5-5": "sonnet",
    "ca.anthropic.claude-sonnet-5-5": "sonnet",
    "eu.anthropic.claude-sonnet-5-5": "sonnet",
    "au.anthropic.claude-sonnet-5-5": "sonnet",
    "global.anthropic.claude-sonnet-5-5": "sonnet",
    "us.anthropic.claude-opus-5-5": "opus",
    "eu.anthropic.claude-opus-5-5": "opus",
    "au.anthropic.claude-opus-5-5": "opus",
    "jp.anthropic.claude-opus-5-5": "opus",
    "global.anthropic.claude-opus-5-5": "opus",
}
BEDROCK_MODELS = NOVA_2_LITE_PROFILES | frozenset(CLAUDE_MODELS)
# What "off" sends: the least thinking each Claude model allows.
CLAUDE_OFF_THINKING = {"haiku": "disabled", "sonnet": "between_tools", "opus": "adaptive"}
# Bedrock's per-image limit for Claude is 5 MB of base64, 3,750,000 raw bytes.
CLAUDE_MAX_IMAGE_BYTES = 3750000
CLAUDE_IMAGE_RESERVED_TOKENS = 4800
REASONING_EFFORTS = frozenset({"off", "low", "medium"})
MAX_OUTPUT_TOKENS = 65000
MAX_TEXT_BYTES = 1024 * 1024
MAX_TOOLS = 64
MAX_TOOL_INPUT_BYTES = 256 * 1024
MAX_CONTENT_BLOCKS = 128
# Claude on Bedrock takes at most four cache checkpoints per request, and a
# longer TTL may not follow a shorter one. Longest first.
MAX_CACHE_POINTS = 4
CACHE_TTLS = ("1h", "5m")
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
# The stream event kinds this adapter reads. Any other kind is skipped.
STREAM_EVENTS = frozenset({
    "messageStart", "contentBlockStart", "contentBlockDelta", "contentBlockStop",
    "messageStop", "metadata",
})
# The delta kinds this adapter reads. Any other kind is skipped.
DELTA_KINDS = ("text", "toolUse", "reasoningContent")


def model_family(model):
    """"claude" for an allowed Claude profile, else "nova"."""
    return "claude" if model in CLAUDE_MODELS else "nova"


def reasoning_fields(model, effort):
    """The additionalModelRequestFields for one model and effort, or None.

    Nova sends nothing for "off" and its reasoningConfig otherwise. Claude
    always gets an explicit thinking type and effort. A fresh dict each call.
    """
    if model_family(model) == "nova":
        if effort == "off":
            return None
        return {"reasoningConfig": {"type": "enabled", "maxReasoningEffort": effort}}
    if effort == "off":
        return {"thinking": {"type": CLAUDE_OFF_THINKING[CLAUDE_MODELS[model]]},
                "output_config": {"effort": "low"}}
    return {"thinking": {"type": "adaptive"}, "output_config": {"effort": effort}}


def image_reserved_tokens(model):
    """Tokens the guard reserves per image sent to this model."""
    if model_family(model) == "claude":
        return CLAUDE_IMAGE_RESERVED_TOKENS
    from mojo.helpers.llm_images import IMAGE_RESERVED_TOKENS
    return IMAGE_RESERVED_TOKENS


def _request_id(response):
    metadata = response.get("ResponseMetadata") if isinstance(response, dict) else None
    value = metadata.get("RequestId") if isinstance(metadata, dict) else None
    return value if isinstance(value, str) and REQUEST_ID_PATTERN.fullmatch(value) else ""


def _usage(response):
    """The four counters from a Converse ``usage``; every other field is ignored.

    The cache counts are optional (Nova omits them) and read as zero.
    """
    raw = response.get("usage") if isinstance(response, dict) else None
    if not isinstance(raw, dict):
        return None

    def optional(key):
        value = raw.get(key)
        return 0 if value is None else value

    return validated_usage({
        "input_tokens": raw.get("inputTokens"),
        "output_tokens": raw.get("outputTokens"),
        "cache_read_input_tokens": optional("cacheReadInputTokens"),
        "cache_creation_input_tokens": optional("cacheWriteInputTokens"),
    })


def _refuse(detail):
    """Refuse a request before any SDK work, saying which check failed.

    The detail is one of this module's own phrases: never request content.
    """
    raise ProviderError("provider_rejected", detail=detail)


def _cache_point(marker):
    """The Converse checkpoint for one text block's ``cache_control`` marker.

    The marker is Anthropic's own: ``{"type": "ephemeral"}``, optionally with
    ``"ttl": "5m"`` or ``"1h"``. Bedrock's default TTL is 5 minutes. A marker
    decides what a request costs, so it stays exact: nothing else is read
    into a checkpoint.
    """
    if not isinstance(marker, dict) or marker.get("type") != "ephemeral" \
            or set(marker) - {"type", "ttl"} \
            or ("ttl" in marker and marker["ttl"] not in CACHE_TTLS):
        _refuse("cache_control must be ephemeral with an optional 5m or 1h ttl")
    point = {"type": "default"}
    if "ttl" in marker:
        point["ttl"] = marker["ttl"]
    return {"cachePoint": point}


def _no_cache_marker(value, where):
    # cache_control changes what a request costs; outside a cached text
    # block it is refused, never silently dropped.
    if isinstance(value, dict) and "cache_control" in value:
        _refuse(f"cache_control is not allowed on {where}")


def _text_blocks(content, cache=False):
    """Text as Converse blocks. With ``cache``, a block's marker becomes a
    checkpoint right after it; without, a marker is refused.

    Only ``type``, ``text`` and ``cache_control`` are read; any other key a
    caller's block carries is dropped, never sent.
    """
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    if not isinstance(content, list) or not content:
        _refuse("text content must be a string or a non-empty list of text blocks")
    result = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "text" \
                or not isinstance(block.get("text"), str) or not block["text"].strip():
            _refuse("a text block needs non-blank text")
        point = None
        if "cache_control" in block:
            if not cache:
                _refuse("cache_control is not allowed where caching is off")
            point = _cache_point(block["cache_control"])
        result.append({"text": block["text"]})
        if point is not None:
            result.append(point)
    return result


def _check_cache_points(request):
    """At most MAX_CACHE_POINTS, and no longer TTL after a shorter one.

    Bedrock reads the system prompt before the messages, in order.
    """
    blocks = list(request.get("system", []))
    blocks += [block for message in request["messages"] for block in message["content"]]
    ttls = [block["cachePoint"].get("ttl", "5m") for block in blocks if "cachePoint" in block]
    if len(ttls) > MAX_CACHE_POINTS:
        _refuse("more than four cache checkpoints")
    if ttls != sorted(ttls, key=CACHE_TTLS.index):
        _refuse("a longer cache ttl follows a shorter one")


def _image_block(block):
    """A validated canonical image block as Converse raw bytes.

    Image blocks stay exact (llm_images): their keys decide which bytes are
    sent, so an extra key is refused rather than dropped.
    """
    from mojo.helpers.llm_images import ImageInputError, decode_image_block
    try:
        fmt, raw = decode_image_block(block)
    except ImageInputError:
        fmt = raw = None
    if fmt is None:
        _refuse("an image block is invalid")
    return {"image": {"format": fmt, "source": {"bytes": raw}}}


def _is_json_object(value):
    """A JSON-serializable dict with no NaN or Infinity; the wire format is JSON."""
    if not isinstance(value, dict):
        return False
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        return False
    return True


def _json_object(value, what):
    if not _is_json_object(value):
        _refuse(f"{what} must be a JSON object")
    return value


def _no_constant(name):
    # json.loads accepts NaN and Infinity; the echoed input would not.
    raise ValueError(name)


def _tool_config(tools):
    """Translate caller-declared tools; returns (toolConfig or None, names).

    ``name``, ``description`` and ``input_schema`` are required; any other
    key is dropped, never sent. A ``type`` other than ``custom`` names a tool
    the provider would run itself, ``cache_control`` changes the cost and
    ``strict`` promises input that matches the schema, which this adapter
    does not send: all three are refused.
    """
    if tools is None or tools == []:
        return None, frozenset()
    if not isinstance(tools, list):
        _refuse("tools must be a list")
    if len(tools) > MAX_TOOLS:
        _refuse("more than 64 tools")
    names = set()
    specs = []
    for tool in tools:
        if not isinstance(tool, dict):
            _refuse("a tool spec must be an object")
        _no_cache_marker(tool, "a tool spec")
        if tool.get("type", "custom") != "custom":
            _refuse("only custom tools can be declared")
        if tool.get("strict") not in (None, False):
            # Dropping it would silently lose the schema guarantee.
            _refuse("strict tool schemas are not supported")
        name = tool.get("name")
        description = tool.get("description")
        schema = tool.get("input_schema")
        if not isinstance(name, str) or not TOOL_NAME_PATTERN.fullmatch(name):
            _refuse("a tool name must be 1-64 letters, digits, _ or -")
        if name in names:
            _refuse("a tool name is declared twice")
        if not isinstance(description, str) or not description.strip():
            _refuse("a tool needs a non-blank description")
        if not isinstance(schema, dict) or schema.get("type") != "object":
            _refuse("a tool input_schema must be an object schema")
        names.add(name)
        specs.append({"toolSpec": {"name": name, "description": description,
                                   "inputSchema": {"json": _json_object(
                                       schema, "a tool input_schema")}}})
    return {"tools": specs}, frozenset(names)


def _message_blocks(role, content, tool_names, cache=False):
    """Translate one message; returns (blocks, tool_use ids, tool_result ids).

    Each block kind reads only its own keys; any other key is dropped.
    """
    if isinstance(content, str):
        return _text_blocks(content), [], []
    if not isinstance(content, list) or not content:
        _refuse("message content must be a string or a non-empty list")
    blocks, used, answered = [], [], []
    for block in content:
        kind = block.get("type") if isinstance(block, dict) else None
        if kind == "text":
            blocks.extend(_text_blocks([block], cache))
        elif kind == "image" and role == "user":
            blocks.append(_image_block(block))
        elif kind == "tool_use" and role == "assistant" and tool_names:
            _no_cache_marker(block, "a tool_use block")
            tool_id = block.get("id")
            name = block.get("name")
            if not isinstance(tool_id, str) or not TOOL_USE_ID_PATTERN.fullmatch(tool_id):
                _refuse("a tool_use id does not match its pattern")
            if not isinstance(name, str) or name not in tool_names:
                _refuse("a tool_use names a tool that is not declared")
            blocks.append({"toolUse": {"toolUseId": tool_id, "name": name,
                                       "input": _json_object(block.get("input"),
                                                             "tool_use input")}})
            used.append(tool_id)
        elif kind == "tool_result" and role == "user" and tool_names:
            _no_cache_marker(block, "a tool_result block")
            tool_use_id = block.get("tool_use_id")
            is_error = block.get("is_error", False)
            if not isinstance(tool_use_id, str) \
                    or not TOOL_USE_ID_PATTERN.fullmatch(tool_use_id):
                _refuse("a tool_result tool_use_id does not match its pattern")
            if not isinstance(is_error, bool):
                _refuse("a tool_result is_error must be a boolean")
            if "content" not in block:
                _refuse("a tool_result needs content")
            if isinstance(block["content"], list):
                for part in block["content"]:
                    _no_cache_marker(part, "a tool_result's content")
            result = {"toolUseId": tool_use_id, "content": _text_blocks(block["content"])}
            if is_error:
                result["status"] = "error"
            blocks.append({"toolResult": result})
            answered.append(tool_use_id)
        elif kind in ("tool_use", "tool_result") and not tool_names:
            _refuse("a tool block needs declared tools")
        else:
            _refuse(f"{detail_word(kind)} blocks are not allowed in {role} messages")
    return blocks, used, answered


def _translate_messages(messages, tool_names, cache=False):
    """Every tool request is answered by exactly the next user message.

    A message needs ``role`` and ``content``; any other key is dropped
    except ``cache_control``, which is refused.
    """
    if not isinstance(messages, list) or not messages:
        _refuse("messages must be a non-empty list")
    translated = []
    pending = []
    for message in messages:
        if not isinstance(message, dict) \
                or message.get("role") not in ("user", "assistant") \
                or "content" not in message:
            _refuse("a message needs a user or assistant role and content")
        _no_cache_marker(message, "a message")
        blocks, used, answered = _message_blocks(
            message["role"], message["content"], tool_names, cache)
        if message["role"] == "user":
            if len(answered) != len(set(answered)) or set(answered) != set(pending):
                _refuse("tool results must answer exactly the previous tool requests")
        elif pending:
            _refuse("a tool request must be answered by the next user message")
        if len(used) != len(set(used)):
            _refuse("a tool_use id is repeated")
        pending = used
        translated.append({"role": message["role"], "content": blocks})
    if pending:
        _refuse("the last tool request is not answered")
    return translated


def _finish(content, stop_reason, usage, request_id, model, max_tokens, tool_names,
            provider):
    """Validate one complete answer, streamed or not, into the call() shape."""
    def reject(detail):
        # Retain validated billing evidence even when the answer is unusable.
        raise ProviderError("provider_rejected", request_id, usage=usage, detail=detail)

    if usage is None:
        reject("the answer carries no valid usage")
    if not request_id:
        reject("the answer carries no valid request id")
    if usage["output_tokens"] > max_tokens:
        reject("output tokens exceed max_tokens")
    # Whitespace-only text is dropped so the caller can echo the content back.
    content = [block for block in content
               if block["type"] != "text" or block["text"].strip()]
    has_text = any(block["type"] == "text" for block in content)
    has_tool = any(block["type"] == "tool_use" for block in content)
    if stop_reason == "max_tokens" and not has_text and not has_tool:
        # Thinking can spend the whole cap; say so on the ledger.
        reject("max_tokens reached before any answer text")
    if stop_reason == "tool_use":
        if not tool_names:
            reject("a tool_use stop without declared tools")
        if not has_tool:
            reject("a tool_use stop without a tool request")
    elif stop_reason not in ("end_turn", "max_tokens"):
        reject(f"unexpected stop reason {detail_word(stop_reason)}")
    elif has_tool:
        reject(f"a tool request with stop reason {stop_reason}")
    elif not has_text:
        reject(f"{stop_reason} without answer text")
    return {"id": request_id, "type": "message", "role": "assistant",
            "provider": provider, "model": model, "content": content,
            "stop_reason": stop_reason, "usage": usage}


def _tool_request(tool, tool_names, reject):
    """One Converse ``toolUse`` as a tool_use block, or None for a tool that is
    not the caller's (a ``type`` other than ``tool_use``: the provider's own).

    Reads ``toolUseId``, ``name``, ``input`` and ``type``; nothing else.
    """
    if not isinstance(tool, dict):
        reject("a toolUse block must be an object")
    if tool.get("type", "tool_use") != "tool_use":
        return None
    tool_id = tool.get("toolUseId")
    name = tool.get("name")
    if not isinstance(tool_id, str) or not TOOL_USE_ID_PATTERN.fullmatch(tool_id):
        reject("a tool request id does not match its pattern")
    if not isinstance(name, str) or name not in tool_names:
        reject("the answer asks for a tool that was not declared")
    if not _is_json_object(tool.get("input")):
        reject("tool input must be a JSON object")
    return {"type": "tool_use", "id": tool_id, "name": name, "input": tool["input"]}


class BedrockStream:
    """One Converse stream as facade events; the guard owns all accounting.

    Yields ``{"type": "text_delta", "text"}`` events, then one
    ``{"type": "final", "response"}`` holding the validated call() shape.
    Tool requests arrive only in that final response, after the whole answer
    validated. Reasoning content is never yielded. ``usage`` and
    ``request_id`` remain readable for settlement after a failure or cancel.

    Only the fields the answer needs are read and checked. Event kinds,
    delta kinds, block kinds and keys this adapter does not know are
    skipped and never forwarded, so a provider adding one does not break
    a valid answer.
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
        self._pending = []
        self._usage_seen = False
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

    def _reject(self, detail):
        self.close()
        raise ProviderError("provider_rejected", self.request_id, usage=self.usage,
                            detail=detail)

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
            if self._pending:
                return self._pending.pop(0)
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
            self._pending.extend(self._handle(event))

    def _index(self, body):
        index = body.get("contentBlockIndex")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            self._reject("a content block index must be a non-negative integer")
        return index

    def _block(self, index, kind):
        block = self._blocks.get(index)
        if block is None:
            if len(self._blocks) >= MAX_CONTENT_BLOCKS:
                self._reject("more than 128 content blocks")
            block = self._blocks[index] = {"type": kind, "parts": [], "bytes": 0,
                                           "closed": False}
        if block["type"] != kind:
            self._reject(f"{kind} content inside a {block['type']} block")
        if block["closed"]:
            self._reject("content after its block stopped")
        return block

    def _handle(self, event):
        """The facade events one stream event yields; unknown kinds are skipped."""
        if not isinstance(event, dict):
            self._reject("a stream event must be an object")
        results = []
        for kind, body in event.items():
            result = self._handle_one(kind, body)
            if result is not None:
                results.append(result)
        return results

    def _handle_one(self, kind, body):
        if kind in STREAM_ERRORS:
            self.close()
            message = body.get("message") if isinstance(body, dict) else None
            message = message if isinstance(message, str) else ""
            name = kind[:1].upper() + kind[1:]
            raise ProviderError(STREAM_ERRORS[kind], self.request_id, usage=self.usage,
                                detail=f"{name}: {message}" if message else name)
        if kind not in STREAM_EVENTS:
            # A kind this adapter does not read carries nothing it emits.
            return None
        if not isinstance(body, dict):
            self._reject(f"a {kind} event must be an object")
        if kind == "messageStart":
            if body.get("role", "assistant") != "assistant":
                self._reject("the answer's role is not assistant")
        elif kind == "contentBlockStart":
            self._start(body)
        elif kind == "contentBlockDelta":
            return self._delta(body)
        elif kind == "contentBlockStop":
            self._stop(self._index(body))
        elif kind == "messageStop":
            stop_reason = body.get("stopReason")
            if not isinstance(stop_reason, str):
                self._reject("messageStop carries no stop reason")
            if self._stop_reason is not None:
                self._reject("the stop reason arrived twice")
            self._stop_reason = stop_reason
        elif "usage" in body:
            # metadata: only its usage is read; metrics, trace and anything
            # else beside it are ignored.
            if self._usage_seen:
                self._reject("usage arrived twice")
            self._usage_seen = True
            self.usage = _usage(body)
        return None

    def _start(self, body):
        """A tool request's start. Any other start kind is skipped: its deltas,
        if it has any, say what the block holds."""
        start = body.get("start")
        tool = start.get("toolUse") if isinstance(start, dict) else None
        if tool is None:
            return
        index = self._index(body)
        if index in self._blocks:
            self._reject("a content block started twice")
        if not isinstance(tool, dict):
            self._reject("a toolUse start must be an object")
        if tool.get("type", "tool_use") != "tool_use":
            # A tool the provider runs itself is never the caller's request:
            # the whole block is skipped.
            self._block(index, "skip")
            return
        tool_id = tool.get("toolUseId")
        name = tool.get("name")
        if not isinstance(tool_id, str) or not TOOL_USE_ID_PATTERN.fullmatch(tool_id):
            self._reject("a tool request id does not match its pattern")
        if not isinstance(name, str) or name not in self._tool_names:
            self._reject("the answer asks for a tool that was not declared")
        block = self._block(index, "tool_use")
        block.update(id=tool_id, name=name)

    def _delta(self, body):
        delta = body.get("delta")
        if not isinstance(delta, dict):
            self._reject("contentBlockDelta carries no delta object")
        kinds = [kind for kind in DELTA_KINDS if kind in delta]
        if not kinds:
            return None
        index = self._index(body)
        current = self._blocks.get(index)
        if current is not None and current["type"] == "skip":
            return None
        result = None
        for kind in kinds:
            value = delta[kind]
            if kind == "reasoningContent":
                # Never read, never yielded; the block only keeps its place.
                self._block(index, "reasoning")
            elif kind == "text":
                result = self._text_delta(index, value)
            else:
                self._tool_delta(index, value)
        return result

    def _text_delta(self, index, value):
        if not isinstance(value, str):
            self._reject("a text delta must be a string")
        block = self._block(index, "text")
        try:
            self._text_bytes += len(value.encode("utf-8"))
        except UnicodeError:
            self._reject("answer text is not valid UTF-8")
        if self._text_bytes > MAX_TEXT_BYTES:
            self._reject("answer text exceeds 1 MiB")
        block["parts"].append(value)
        return {"type": "text_delta", "text": value} if value else None

    def _tool_delta(self, index, value):
        block = self._blocks.get(index)
        if block is None or block["type"] != "tool_use":
            self._reject("tool input outside a started tool request")
        if block["closed"]:
            self._reject("tool input after its tool request stopped")
        if not isinstance(value, dict):
            self._reject("a toolUse delta must be an object")
        piece = value.get("input", "")
        if not isinstance(piece, str):
            self._reject("a tool input delta must be a string")
        block["bytes"] += len(piece.encode("utf-8", "replace"))
        if block["bytes"] > MAX_TOOL_INPUT_BYTES:
            self._reject("tool input exceeds 256 KiB")
        block["parts"].append(piece)

    def _stop(self, index):
        block = self._blocks.get(index)
        if block is None:
            # A block that never carried a delta this adapter reads.
            return
        if block["closed"]:
            self._reject("a content block stopped twice")
        block["closed"] = True
        if block["type"] != "tool_use":
            return
        raw = "".join(block["parts"])
        try:
            value = json.loads(raw, parse_constant=_no_constant) if raw.strip() else {}
        except (ValueError, RecursionError):
            value = None
        if not isinstance(value, dict):
            self._reject("tool input must be a JSON object (no NaN or Infinity)")
        block["input"] = value

    def _final(self):
        content = []
        for index in sorted(self._blocks):
            block = self._blocks[index]
            if block["type"] == "text":
                content.append({"type": "text", "text": "".join(block["parts"])})
            elif block["type"] == "tool_use":
                if not block["closed"]:
                    self._reject("a tool request never stopped")
                content.append({"type": "tool_use", "id": block["id"],
                                "name": block["name"], "input": block["input"]})
        if self._stop_reason is None:
            self._reject("the stream ended without a stop reason")
        self.close()
        return _finish(content, self._stop_reason, self.usage, self.request_id,
                       self._model, self._max_tokens, self._tool_names, "bedrock")


class BedrockProvider(ProviderAdapter):
    name = "bedrock"
    capabilities = frozenset({"text", "images", "tools", "stream", "prompt_cache"})

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
            # The class only: an SDK message can quote request parameters.
            detail = f"unexpected {type(err).__name__}"
        raise ProviderError(code, request_id, retry_after=retry_after,
                            detail=detail) from None

    def _request(self, messages, model, max_tokens, system, tools, reasoning_effort,
                 timeout, cache_enabled=False):
        """Validate one request before any SDK work; returns (request, names, timeout).

        ``cache_enabled`` is the guard's word that the route declares
        ``prompt_cache``. Only then, and only for Claude, does a text block's
        ``cache_control`` marker become a checkpoint.
        """
        if not isinstance(model, str) or model not in BEDROCK_MODELS:
            _refuse("the model is not on the Bedrock allow list")
        if not isinstance(reasoning_effort, str) or reasoning_effort not in REASONING_EFFORTS:
            _refuse("reasoning_effort must be off, low or medium")
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) \
                or not 1 <= max_tokens <= MAX_OUTPUT_TOKENS:
            _refuse("max_tokens must be an integer from 1 to 65000")
        cache = cache_enabled is True and model_family(model) == "claude"
        tool_config, tool_names = _tool_config(tools)
        request = {"modelId": model,
                   "messages": _translate_messages(messages, tool_names, cache),
                   "inferenceConfig": {"maxTokens": max_tokens}}
        if model_family(model) == "claude" and any(
                len(block["image"]["source"]["bytes"]) > CLAUDE_MAX_IMAGE_BYTES
                for message in request["messages"] for block in message["content"]
                if "image" in block):
            _refuse("a Claude image exceeds 3,750,000 bytes")
        if system is not None and system != "":
            request["system"] = _text_blocks(system, cache)
        if cache:
            _check_cache_points(request)
        if tool_config is not None:
            request["toolConfig"] = tool_config
        extra = reasoning_fields(model, reasoning_effort)
        if extra is not None:
            request["additionalModelRequestFields"] = extra
        timeout = 60 if timeout is None else timeout
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) \
                or timeout <= 0:
            _refuse("timeout must be a positive number")
        try:
            finite_timeout = math.isfinite(timeout)
        except OverflowError:
            finite_timeout = False
        if not finite_timeout:
            _refuse("timeout must be finite")
        if self.client is None and (not isinstance(self.region, str)
                                    or not REGION_PATTERN.fullmatch(self.region)):
            _refuse("the region is not a canonical AWS region")
        return request, tool_names, timeout

    def check_request(self, messages, model, max_tokens, system=None, tools=None,
                      timeout=None, reasoning_effort="off", cache_enabled=False):
        """Validate a request without any SDK work, so the guard can refuse it
        before reserving budget. Raises ProviderError("provider_rejected")."""
        self._request(messages, model, max_tokens, system, tools, reasoning_effort,
                      timeout, cache_enabled)

    def call(self, messages, model, max_tokens, system=None, tools=None,
             cache_enabled=False, timeout=None, reasoning_effort="off"):
        request, tool_names, timeout = self._request(
            messages, model, max_tokens, system, tools, reasoning_effort, timeout,
            cache_enabled)
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
            raise ProviderError("provider_rejected", request_id, usage=usage,
                                detail="the answer could not be read") from None

    def stream(self, messages, model, max_tokens, system=None, tools=None,
               cache_enabled=False, timeout=None, reasoning_effort="off"):
        """Dispatch converse_stream; errors before the first event raise here."""
        request, tool_names, timeout = self._request(
            messages, model, max_tokens, system, tools, reasoning_effort, timeout,
            cache_enabled)
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
            raise ProviderError("provider_rejected", stream.request_id,
                                detail="the response carries no event stream")
        return stream

    def _normalize(self, response, model, max_tokens, tool_names=frozenset()):
        """One Converse answer as the call() shape.

        Reads the message's role and content, the stop reason, usage and the
        request id. Text and the caller's tool requests are kept; reasoning,
        redacted content and any block kind this adapter does not know are
        skipped and never forwarded. Extra keys anywhere are ignored.
        """
        usage = _usage(response)
        request_id = _request_id(response)

        def reject(detail):
            # Retain validated billing evidence even when the answer is unusable.
            raise ProviderError("provider_rejected", request_id, usage=usage,
                                detail=detail)

        output = response.get("output")
        message = output.get("message") if isinstance(output, dict) else None
        if not isinstance(message, dict):
            reject("the answer carries no output message")
        if message.get("role", "assistant") != "assistant":
            reject("the answer's role is not assistant")
        blocks = message.get("content")
        if not isinstance(blocks, list):
            reject("the answer's content must be a list")
        content = []
        text_bytes = 0
        for block in blocks:
            if not isinstance(block, dict):
                reject("a content block must be an object")
            if "toolUse" in block and "text" in block:
                reject("a content block is both text and a tool request")
            if "toolUse" in block:
                tool = _tool_request(block["toolUse"], tool_names, reject)
                if tool is not None:
                    content.append(tool)
            elif "text" in block:
                text = block["text"]
                if not isinstance(text, str):
                    reject("a text block must be a string")
                try:
                    text_bytes += len(text.encode("utf-8")) + bool(content)
                except UnicodeError:
                    reject("answer text is not valid UTF-8")
                if text_bytes > MAX_TEXT_BYTES:
                    reject("answer text exceeds 1 MiB")
                content.append({"type": "text", "text": text})
        return _finish(content, response.get("stopReason"), usage, request_id, model,
                       max_tokens, tool_names, self.name)
