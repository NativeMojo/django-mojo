class ProviderError(Exception):
    def __init__(self, code, request_id="", retry_after=None):
        self.code = code
        self.request_id = request_id or ""
        self.retry_after = retry_after
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

    def verify(self):
        raise NotImplementedError
