import re


USAGE_ORDER = ("input_tokens", "output_tokens", "cache_read_input_tokens",
               "cache_creation_input_tokens")
USAGE_KEYS = frozenset(USAGE_ORDER)
MAX_USAGE_TOKENS = 2147483647
# LLMRequest.error_detail's max_length.
MAX_ERROR_DETAIL = 512
# A provider's own word (a stop reason, an event kind) that may be quoted in
# a detail: short and plain, never free text.
DETAIL_WORD_PATTERN = re.compile(r"[A-Za-z0-9_.:-]{1,64}")


def validated_usage(usage):
    """Copy complete, bounded token evidence, or retain unknown usage as None.

    The four counters must all be present and well-typed. Anything else a
    provider adds beside them is ignored and never copied.
    """
    if not isinstance(usage, dict) or not USAGE_KEYS.issubset(usage):
        return None
    result = {key: usage[key] for key in USAGE_ORDER}
    if any(isinstance(value, bool) or not isinstance(value, int)
           or not 0 <= value <= MAX_USAGE_TOKENS for value in result.values()):
        return None
    return result


def detail_word(value):
    """A provider's enum value fit to quote in a detail, else "unknown"."""
    if isinstance(value, str) and DETAIL_WORD_PATTERN.fullmatch(value):
        return value
    return "unknown"


class ProviderError(Exception):
    def __init__(self, code, request_id="", retry_after=None, usage=None, detail=""):
        self.code = code
        self.request_id = request_id or ""
        self.retry_after = retry_after
        self.usage = validated_usage(usage)
        # The provider's own error text verbatim, or which of the framework's
        # own checks refused the answer. Never request content.
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
