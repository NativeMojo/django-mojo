from .base import USAGE_ORDER, MAX_USAGE_TOKENS, ProviderAdapter, ProviderError


# The keys each request block kind may carry to Anthropic. Any other key on a
# block of these kinds is dropped, never sent; a block of another kind passes
# unchanged and the provider decides it. ``cache_control`` is kept only where
# the guard enables caching.
REQUEST_BLOCK_KEYS = {
    "text": ("type", "text", "cache_control", "citations"),
    "image": ("type", "source", "cache_control"),
    "tool_use": ("type", "id", "name", "input", "cache_control"),
    "tool_result": ("type", "tool_use_id", "content", "is_error", "cache_control"),
    "thinking": ("type", "thinking", "signature"),
    "redacted_thinking": ("type", "data"),
}
MESSAGE_KEYS = ("role", "content")
# A caller's own (custom) tool. A tool with any other ``type`` is the
# provider's own and passes unchanged.
TOOL_KEYS = ("type", "name", "description", "input_schema", "cache_control", "strict")
# The answer block kinds the caller receives, with the fields each must carry.
# Any other kind is skipped and never forwarded; any other key is dropped.
RESPONSE_BLOCK_FIELDS = {
    "text": (("text", str),),
    "tool_use": (("id", str), ("name", str), ("input", dict)),
    "thinking": (("thinking", str), ("signature", str)),
    "redacted_thinking": (("data", str),),
}


def _known_keys(value, keys, cache):
    """A copy holding only ``keys``; None values and, without ``cache``, a
    ``cache_control`` marker are left out."""
    return {key: value[key] for key in keys if key in value and value[key] is not None
            and (cache or key != "cache_control")}


def _request_blocks(content, cache):
    if not isinstance(content, list):
        return content
    result = []
    for block in content:
        keys = REQUEST_BLOCK_KEYS.get(block.get("type")) if isinstance(block, dict) else None
        if keys is None:
            result.append(block)
            continue
        block = _known_keys(block, keys, cache)
        if "content" in block:
            block["content"] = _request_blocks(block["content"], cache)
        result.append(block)
    return result


def _request_messages(messages, cache):
    if not isinstance(messages, list):
        return messages
    result = []
    for message in messages:
        if isinstance(message, dict):
            message = _known_keys(message, MESSAGE_KEYS, cache)
            if "content" in message:
                message["content"] = _request_blocks(message["content"], cache)
        result.append(message)
    return result


def _request_tools(tools, cache):
    if not isinstance(tools, list):
        return tools
    return [_known_keys(tool, TOOL_KEYS, cache)
            if isinstance(tool, dict) and tool.get("type", "custom") == "custom" else tool
            for tool in tools]


def _answer(dump):
    """The model_dump of one answer as the call() shape.

    Reads the id, model, role, content, stop reason, stop sequence and the
    four usage counters. Text, tool_use and thinking blocks are kept with
    their own fields; any other block kind is skipped and any other key or
    field is dropped, so nothing the SDK adds reaches the caller.
    """
    if not isinstance(dump, dict):
        raise ProviderError("provider_rejected", detail="the answer is not an object")
    request_id = dump.get("id") if isinstance(dump.get("id"), str) else ""

    def reject(detail):
        raise ProviderError("provider_rejected", request_id, detail=detail)

    if dump.get("role", "assistant") != "assistant":
        reject("the answer's role is not assistant")
    blocks = dump.get("content")
    if not isinstance(blocks, list):
        reject("the answer's content must be a list")
    content = []
    for block in blocks:
        if not isinstance(block, dict):
            reject("a content block must be an object")
        kind = block.get("type")
        fields = RESPONSE_BLOCK_FIELDS.get(kind) if isinstance(kind, str) else None
        if fields is None:
            continue
        for name, kind_of in fields:
            if not isinstance(block.get(name), kind_of):
                reject(f"a {kind} block needs {name}")
        content.append({"type": kind, **{name: block[name] for name, _kind in fields}})
    raw_usage = dump.get("usage")
    raw_usage = raw_usage if isinstance(raw_usage, dict) else {}
    usage = {}
    for key in USAGE_ORDER:
        value = raw_usage.get(key)
        if value is None:
            value = 0
        if isinstance(value, bool) or not isinstance(value, int) \
                or not 0 <= value <= MAX_USAGE_TOKENS:
            reject("usage counters must be non-negative integers")
        usage[key] = value
    stop_reason = dump.get("stop_reason")
    stop_sequence = dump.get("stop_sequence")
    model = dump.get("model")
    return {"id": request_id, "type": "message", "role": "assistant",
            "provider": "anthropic", "model": model if isinstance(model, str) else "",
            "content": content,
            "stop_reason": stop_reason if isinstance(stop_reason, str) else None,
            "stop_sequence": stop_sequence if isinstance(stop_sequence, str) else None,
            "usage": usage}


class AnthropicProvider(ProviderAdapter):
    name = "anthropic"
    capabilities = frozenset({"text", "tools", "images", "prompt_cache", "models"})

    def __init__(self, api_key=None, client=None):
        self.api_key = api_key
        self.client = client

    def _client(self):
        if self.client is not None:
            return self.client
        import anthropic
        # The safety boundary accounts one adapter call as one provider
        # request. SDK retries would silently multiply spend and half-open
        # breaker probes behind that accounting.
        self.client = anthropic.Anthropic(api_key=self.api_key, max_retries=0)
        return self.client

    def _raise_safe(self, err):
        import anthropic
        request_id = getattr(err, "request_id", "") or ""
        if isinstance(err, anthropic.AuthenticationError):
            code = "provider_authentication"
        elif isinstance(err, anthropic.RateLimitError):
            code = "provider_rate_limited"
        elif isinstance(err, anthropic.APITimeoutError):
            code = "provider_timeout"
        elif isinstance(err, anthropic.APIConnectionError):
            code = "provider_unavailable"
        elif isinstance(err, anthropic.APIStatusError):
            status = getattr(err, "status_code", 0)
            if status == 402:
                code = "provider_billing_exhausted"
            elif status >= 500:
                code = "provider_unavailable"
            else:
                code = "provider_rejected"
        else:
            code = "provider_failed"
        detail = ""
        if isinstance(err, anthropic.APIStatusError):
            detail = self._error_detail(err)
        retry_after = None
        if code == "provider_rate_limited":
            try:
                retry_after = int(err.response.headers.get("retry-after", ""))
            except (AttributeError, TypeError, ValueError):
                pass
        raise ProviderError(code, request_id, retry_after=retry_after,
                            detail=detail) from None

    @staticmethod
    def _error_detail(err):
        """"<type>: <message>" from the API's error body, else "HTTP <status>".

        Only the error Anthropic returns is read; the framework adds no
        request content.
        """
        body = getattr(err, "body", None)
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict):
            kind = error.get("type")
            message = error.get("message")
            kind = kind if isinstance(kind, str) else ""
            message = message if isinstance(message, str) else ""
            if kind or message:
                return f"{kind}: {message}" if kind and message else (kind or message)
        # The SDK's own message renders the whole response body; keep only
        # the status when the body has no usable error object.
        status = getattr(err, "status_code", None)
        return f"HTTP {status}" if isinstance(status, int) else ""

    def call(self, messages, model, max_tokens, system=None, tools=None,
             cache_enabled=True, timeout=None):
        cache = bool(cache_enabled)
        kwargs = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": _request_messages(messages, cache),
        }
        if system:
            kwargs["system"] = _request_blocks(system, cache)
        if tools:
            kwargs["tools"] = _request_tools(tools, cache)
        if cache_enabled:
            kwargs["cache_control"] = {"type": "ephemeral"}
        if timeout is not None:
            kwargs["timeout"] = timeout
        try:
            dump = self._client().messages.create(**kwargs).model_dump()
        except Exception as err:
            self._raise_safe(err)
        return _answer(dump)

    def count_input_tokens(self, messages, model, system=None, tools=None,
                           timeout=None):
        """One free count request for the exact message request."""
        kwargs = {"model": model, "messages": _request_messages(messages, True)}
        if system:
            kwargs["system"] = _request_blocks(system, True)
        if tools:
            kwargs["tools"] = _request_tools(tools, True)
        if timeout is not None:
            kwargs["timeout"] = timeout
        try:
            response = self._client().messages.count_tokens(**kwargs)
            return int(response.input_tokens)
        except Exception as err:
            self._raise_safe(err)

    def list_models(self, timeout=None):
        """Compatibility helper returning one page's model list."""
        return self.list_models_page(timeout=timeout)["data"]

    def list_models_page(self, timeout=None, after_id=None):
        """Exactly one bounded page; the guard accounts pagination separately."""
        try:
            kwargs = {"limit": 100}
            if timeout is not None:
                kwargs["timeout"] = timeout
            if after_id is not None:
                kwargs["after_id"] = after_id
            page = self._client().models.list(**kwargs)
            return {"data": [model.model_dump(mode="json") for model in page.data],
                    "has_more": page.has_more,
                    "last_id": getattr(page, "last_id", None)}
        except Exception as err:
            self._raise_safe(err)

    def verify(self):
        try:
            self._client().models.list(limit=1)
            return True
        except Exception as err:
            self._raise_safe(err)
