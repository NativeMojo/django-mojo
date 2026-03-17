from .base import OAuthProvider
from .google import GoogleOAuthProvider
from .apple import AppleOAuthProvider

PROVIDERS = {
    "google": GoogleOAuthProvider,
    "apple": AppleOAuthProvider,
}


def get_provider(name):
    """Return an OAuthProvider instance for the given provider name."""
    cls = PROVIDERS.get(name)
    if cls is None:
        raise ValueError(f"Unknown OAuth provider: {name}")
    return cls()
