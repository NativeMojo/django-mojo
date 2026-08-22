"""Foundation gallery and reset coordination for the Admin preview."""

from .features import (
    activity, advanced, assistant, capacity, dashboard, email, maintenance,
    people, platform, settings, sms, webapps,
)


PROVIDERS = (dashboard, people, webapps, activity, platform, advanced, settings,
             sms, email, assistant)
# Providers that serve pages but publish no feature lane of their own. They
# still hold scenario state, so reset must reach them; bootstrap must not,
# or the shell would learn about a feature its registry has never heard of.
RESET_ONLY = (maintenance, capacity)


def bootstrap(groups, membership_groups=None, can_create_webapp_group=True,
              infrastructure_mode="managed"):
    # Default managed so every existing caller — the tests included — keeps the
    # payload it had. The key is ALWAYS present because the feature providers
    # index it directly.
    capabilities = {
        # The readiness fixture deliberately fails django.base_url, so the
        # honest bootstrap state is "System Setup wants attention".
        "setup": True, "setup_attention": True, "people": True, "groups": True,
        "manage_users": True, "manage_groups": True,
        "manage_api_keys": True, "view_logins": True,
        "view_logs": True, "view_events": True,
        "view_incidents": True, "view_tickets": True,
        "network": True, "manage_network": True, "manage_aws": True,
        "webapps": True, "manage_webapps": True,
        "view_logs": True, "view_security": True, "manage_security": True,
        "view_platform": True, "manage_platform": True,
        "view_platform_security": True, "view_advanced": True,
        "manage_advanced": True, "view_advanced_inventory": True,
        "view_advanced_security": True, "view_advanced_settings": True,
        "settings": True, "catalog_write": True,
        "settings_owner_display": True, "settings_owner_edit": True,
        "messaging_sms": True, "messaging_sms_system_write": True,
        "email": True,
        "assistant": True, "assistant_ready": True, "assistant_setup": True,
        "infrastructure_managed": infrastructure_mode == "managed",
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
        "infrastructure": {"mode": infrastructure_mode,
                           "managed": infrastructure_mode == "managed"},
        "edge": {"available": True, "http_enabled": True,
                 "dnsman_issuance": "dns-01"},
        "features": {provider.NAME: provider.describe(capabilities)
                     for provider in PROVIDERS},
    }


def reset(handler, fixtures, *, key_state="active", setup_state="idle",
          activity_state="full", onboarding_state="idle",
          dashboard_state="healthy", settings_state="normal",
          metrics_state="live", maintenance_state="findings",
          deployments_state="mixed", capacity_state="healthy",
          sms_state="configured", email_state="configured",
          assistant_state="configured",
          infrastructure_mode="managed"):
    """Reset every stateful provider so scenarios never leak across runs."""
    # An installation-wide property rather than a provider scenario, so it is
    # stamped on the handler here instead of being threaded through resets.
    handler.infrastructure_mode = infrastructure_mode
    for provider in PROVIDERS + RESET_ONLY:
        provider.reset(handler, fixtures, key_state=key_state,
                       setup_state=setup_state,
                       activity_state=activity_state,
                       dashboard_state=dashboard_state,
                       onboarding_state=onboarding_state,
                       settings_state=settings_state,
                       metrics_state=metrics_state,
                       maintenance_state=maintenance_state,
                       deployments_state=deployments_state,
                       capacity_state=capacity_state,
                       sms_state=sms_state,
                       email_state=email_state,
                       assistant_state=assistant_state)
