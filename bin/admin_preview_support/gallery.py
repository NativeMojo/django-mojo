"""Foundation gallery and reset coordination for the Admin preview."""

from .features import activity, advanced, dashboard, people, platform, webapps


PROVIDERS = (dashboard, people, webapps, activity, platform, advanced)


def bootstrap(groups):
    capabilities = {
        "setup": True, "people": True, "groups": True,
        "network": True, "manage_network": True,
        "webapps": True, "manage_webapps": True,
    }
    return {
        "version": "1.9.0", "admin_path": "/", "groups": groups,
        "user": {"id": 1, "display_name": "Ian Smith",
                 "email": "ian@example.com", "is_superuser": True},
        "capabilities": capabilities,
        "features": {provider.NAME: provider.describe(capabilities)
                     for provider in PROVIDERS},
    }


def reset(handler, fixtures, *, key_state="active", setup_state="idle",
          onboarding_state="idle"):
    """Reset every stateful provider so scenarios never leak across runs."""
    for provider in PROVIDERS:
        provider.reset(handler, fixtures, key_state=key_state,
                       setup_state=setup_state,
                       onboarding_state=onboarding_state)
