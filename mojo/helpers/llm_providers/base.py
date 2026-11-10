USAGE_KEYS = frozenset({
    "input_tokens", "output_tokens", "cache_read_input_tokens",
    "cache_creation_input_tokens",
})
MAX_USAGE_TOKENS = 2147483647
# LLMRequest.error_detail's max_length.
MAX_ERROR_DETAIL = 512


def validated_usage(usage):
    """Copy complete, bounded token evidence, or retain unknown usage as None."""
    if not isinstance(usage, dict) or set(usage) != USAGE_KEYS:
        return None
    if any(isinstance(value, bool) or not isinstance(value, int)
           or not 0 <= value <= MAX_USAGE_TOKENS for value in usage.values()):
        return None
    return dict(usage)


class ProviderError(Exception):
    def __init__(self, code, request_id="", retry_after=None, usage=None, detail=""):
        self.code = code
        self.request_id = request_id or ""
        self.retry_after = retry_after
        self.usage = validated_usage(usage)
        # The provider's own error text, verbatim; the framework adds no
        # request content.
        # Kept off str(err), which stays the safe code.
        self.detail = detail[:MAX_ERROR_DETAIL] if isinstance(detail, str) else ""
        super().__init__(code)


class ProviderAdapter:
    name = ""
    capabilities = frozenset()

    def supports(self, capability):
        return capability in self.capabilities

    def call(self, **kwargs):
        raise NotImplementedError

    def list_models(self, timeout=None):
        raise NotImplementedError

    def count_input_tokens(self, messages, model, system=None, tools=None,
                           timeout=None):
        """Provider-counted input tokens for this exact request, or None.

        None means the provider has no free count; the guard then reserves
        the serialized byte length instead.
        """
        return None

    def verify(self):
        raise NotImplementedError
