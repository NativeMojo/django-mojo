from .anthropic import AnthropicProvider


PROVIDERS = {"anthropic": AnthropicProvider}


def get_provider(name, **kwargs):
    provider = PROVIDERS.get(name)
    if provider is None:
        return None
    return provider(**kwargs)
