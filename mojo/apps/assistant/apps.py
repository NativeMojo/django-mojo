from django.apps import AppConfig as BaseAppConfig
from django.utils.module_loading import autodiscover_modules


class AppConfig(BaseAppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'mojo.apps.assistant'

    def ready(self):
        # Register built-in tools first
        from mojo.apps.assistant.services import tools as _  # noqa: F401
        # Then discover assistant_tools.py in all installed apps
        autodiscover_modules("assistant_tools")
        self.register_settings_descriptors()

    def register_settings_descriptors(self):
        """Advertise the Assistant's configuration in the Admin catalog.

        Registered here rather than from ``register_core_descriptors()`` — the
        documented seam for an OPTIONAL application — so an installation
        without the assistant app never advertises settings it does not read.
        The runtime protection (``admin_settings.is_catalog_protected``) is
        unconditional and does not depend on this registration.

        All four are read-only in the catalog: an unknown ``writable`` value
        yields can_write / can_clear / can_owner_edit all false, exactly as
        ``fleet_config`` and ``provider_setup`` already behave. The owner
        editor lives at POST /api/account/admin/assistant.
        """
        from mojo.apps.account.services.admin_settings import (
            Descriptor, register_descriptor)

        register_descriptor(Descriptor(
            "LLM_ADMIN_ENABLED", "Assistant enabled", "Security & operations",
            "Whether the built-in Admin Assistant answers messages.", "boolean",
            False, resolver="dynamic", writable="assistant_setup",
            owner="Assistant setup", change_behavior="immediate",
            storage="database"))
        register_descriptor(Descriptor(
            "LLM_ADMIN_MODEL", "Assistant model", "Security & operations",
            "Model the Assistant is pinned to.", "string",
            resolver="dynamic", writable="assistant_setup",
            owner="Assistant setup", change_behavior="immediate",
            storage="database",
            unset_meaning="the newest Sonnet is selected automatically"))
        register_descriptor(Descriptor(
            "LLM_ADMIN_API_KEY", "Assistant API key", "Security & operations",
            "Encrypted Anthropic credential used by the Assistant.", "configured",
            resolver="dynamic", sensitivity="configured_only",
            writable="assistant_setup", owner="Assistant setup",
            change_behavior="immediate", storage="database"))
        # Visible as PROVENANCE, never as an editor: the fallback is read from
        # the deployment file, and a page whose job is "how this platform is
        # configured" has to be able to say where the credential comes from.
        register_descriptor(Descriptor(
            "LLM_HANDLER_API_KEY", "Assistant fallback API key",
            "Security & operations",
            "Deployment credential used when no Assistant key is configured.",
            "configured", resolver="static", sensitivity="configured_only",
            writable="none", owner="Deployment settings",
            change_behavior="deploy",
            unset_meaning="the Assistant has no fallback credential"))
