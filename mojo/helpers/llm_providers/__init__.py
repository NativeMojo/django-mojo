from .anthropic import AnthropicProvider
from .bedrock import BedrockProvider


PROVIDERS = {"anthropic": AnthropicProvider, "bedrock": BedrockProvider}


def get_provider(name, **kwargs):
    provider = PROVIDERS.get(name)
    if provider is None:
        return None
    return provider(**kwargs)
