from .base import ProviderAdapter, ProviderError


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
        self.client = anthropic.Anthropic(api_key=self.api_key)
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
        retry_after = None
        if code == "provider_rate_limited":
            try:
                retry_after = int(err.response.headers.get("retry-after", ""))
            except (AttributeError, TypeError, ValueError):
                pass
        raise ProviderError(code, request_id, retry_after=retry_after) from None

    def call(self, messages, model, max_tokens, system=None, tools=None,
             cache_enabled=True, timeout=None):
        kwargs = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = tools
        if cache_enabled:
            kwargs["cache_control"] = {"type": "ephemeral"}
        if timeout is not None:
            kwargs["timeout"] = timeout
        try:
            response = self._client().messages.create(**kwargs)
            return response.model_dump()
        except Exception as err:
            self._raise_safe(err)

    def list_models(self, timeout=None):
        """Return one bounded page; pagination is a separately-accounted operation."""
        try:
            kwargs = {"limit": 100}
            if timeout is not None:
                kwargs["timeout"] = timeout
            page = self._client().models.list(**kwargs)
            return [model.model_dump(mode="json") for model in page.data]
        except Exception as err:
            self._raise_safe(err)

    def verify(self):
        try:
            self._client().models.list(limit=1)
            return True
        except Exception as err:
            self._raise_safe(err)
