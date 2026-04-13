from .base import OAuthProvider
from .google import GoogleOAuthProvider
from .apple import AppleOAuthProvider
from .github import GitHubOAuthProvider

PROVIDERS = {
    "google": GoogleOAuthProvider,
    "apple": AppleOAuthProvider,
    "github": GitHubOAuthProvider,
}


def get_provider(name):
    """Return an OAuthProvider instance for the given provider name."""
    cls = PROVIDERS.get(name)
    if cls is None:
        raise ValueError(f"Unknown OAuth provider: {name}")
    return cls()
