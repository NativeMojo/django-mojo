"""Foundation gallery and reset coordination for the Admin preview."""

from .features import activity, advanced, dashboard, people, platform, settings, webapps


PROVIDERS = (dashboard, people, webapps, activity, platform, advanced, settings)


def bootstrap(groups, membership_groups=None, can_create_webapp_group=True):
    capabilities = {
        "setup": True, "people": True, "groups": True,
        "manage_users": True, "manage_groups": True,
        "manage_api_keys": True, "view_logins": True,
        "view_logs": True, "view_events": True,
        "view_incidents": True, "view_tickets": True,
        "network": True, "manage_network": True,
        "webapps": True, "manage_webapps": True,
        "view_logs": True, "view_security": True, "manage_security": True,
        "view_platform": True, "manage_platform": True,
        "view_platform_security": True, "view_advanced": True,
        "manage_advanced": True, "view_advanced_inventory": True,
        "view_advanced_security": True, "view_advanced_settings": True,
        "settings": True, "catalog_write": True,
        "settings_owner_display": True, "settings_owner_edit": True,
    }
    return {
        "version": "1.9.0", "admin_path": "/",
        "groups": groups if membership_groups is None else membership_groups,
        "webapp_groups": [dict(group, can_manage_dns=True) for group in groups],
        "can_create_webapp_group": can_create_webapp_group,
        "user": {"id": 1, "display_name": "Ian Smith",
                 "username": "ian@example.com", "email": "ian@example.com",
                 "is_superuser": True},
        "capabilities": capabilities,
        "features": {provider.NAME: provider.describe(capabilities)
                     for provider in PROVIDERS},
    }


def reset(handler, fixtures, *, key_state="active", setup_state="idle",
          activity_state="full", onboarding_state="idle",
          dashboard_state="healthy", settings_state="normal"):
    """Reset every stateful provider so scenarios never leak across runs."""
    for provider in PROVIDERS:
        provider.reset(handler, fixtures, key_state=key_state,
                       setup_state=setup_state,
                       activity_state=activity_state,
                       dashboard_state=dashboard_state,
                       onboarding_state=onboarding_state,
                       settings_state=settings_state)
