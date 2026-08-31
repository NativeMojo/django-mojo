from django.apps import AppConfig as BaseAppConfig
from django.utils.module_loading import autodiscover_modules

from mojo.helpers import logit


class AppConfig(BaseAppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'mojo.apps.assistant'

    def ready(self):
        # Register built-in tools first
        from mojo.apps.assistant.services import tools as _  # noqa: F401
        # Then discover assistant_tools.py in all installed apps
        autodiscover_modules("assistant_tools")
        self.register_settings_descriptors()
        self.register_oauth_resource()

    def register_oauth_resource(self):
        """Declare the two remote-agent resources to the authorization server.

        Registration is what makes a path a resource the server will mint
        tokens for and confine them to. Two are declared here, behind ONE
        switch — ASSISTANT_MCP_ENABLED means "remote agents may sign in", for
        either kind of access:

        * the MCP door, EXACT, offering only `mcp`. A token minted for it
          authenticates at that one path and nowhere else.
        * the REST API root, a PREFIX resource offering `mcp` and `api`. A
          token minted for it with the consented `api` scope reaches every
          endpoint beneath the root exactly as the person's own session would,
          and nothing outside it.

        The `enabled` callable is re-read on every check, so the switch takes
        effect immediately in both directions. Guarded on the account app being
        installed: the assistant can ship without it.

        The MCP path comes from `mcp_auth.configured_path()`, the same helper
        the route and the challenge use. `validate_access` resolves the token
        audience's path EXACTLY, so a registration that disagreed with the
        route by one trailing slash would refuse every token this server had
        just minted.

        One grant can serve both doors only while the MCP path sits BENEATH
        `API_ROOT` — which the shipped default (`/api/assistant/mcp` under
        `/api`) does. An installation that moves ASSISTANT_MCP_PATH outside the
        root keeps both resources working separately; what it loses is the
        `mcp api` grant at the root reaching the door, which then answers 401
        because the root does not cover it. Fail-closed, and the same caveat a
        SCRIPT_NAME-mounted deployment already carries.

        The work is in `register_oauth_resources()` so it can be driven with an
        explicit root and registry from a test, rather than through `ready()`.
        """
        from django.apps import apps

        if not apps.is_installed("mojo.apps.account"):
            return
        from mojo.apps.assistant.mcp import auth as mcp_auth
        from mojo.helpers.request import API_ROOT
        from mojo.helpers.settings import settings

        register_oauth_resources(
            mcp_auth.configured_path(), API_ROOT,
            lambda: settings.get("ASSISTANT_MCP_ENABLED", False, kind="bool"))

    def register_settings_descriptors(self):
        """Advertise the Assistant's configuration in the Admin catalog.

        Registered here rather than from ``register_core_descriptors()`` — the
        documented seam for an OPTIONAL application — so an installation
        without the assistant app never advertises settings it does not read.
        The runtime protection (``admin_settings.is_catalog_protected``) is
        unconditional and does not depend on this registration.

        All are read-only in the catalog: an unknown ``writable`` value
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
        # The PLATFORM credential: incident triage, the LLM agent, and the
        # Assistant's fallback all read it. Settable from the same owner editor
        # as the Assistant key; a deployment-file value still applies when no
        # row is stored.
        register_descriptor(Descriptor(
            "LLM_HANDLER_API_KEY", "Platform LLM API key",
            "Security & operations",
            "Encrypted Anthropic credential used by every LLM feature and as "
            "the Assistant's fallback.",
            "configured", resolver="dynamic", sensitivity="configured_only",
            writable="assistant_setup", owner="Assistant setup",
            change_behavior="immediate", storage="database",
            unset_meaning="LLM features are off and the Assistant has no fallback"))
        # The MCP door. Off by default and read on every request, so the switch
        # is immediate in both directions. Catalog-protected
        # (admin_settings.ASSISTANT_KEYS) from the moment it exists: a global
        # database row outranks the deployment file, so without protection any
        # manage_settings holder could open a remote-agent door on every node.
        register_descriptor(Descriptor(
            "ASSISTANT_MCP_ENABLED", "Remote agent access (MCP)",
            "Security & operations",
            "Whether the Assistant's MCP endpoint accepts remote AI clients "
            "that signed in through the OAuth flow.",
            "boolean", False, resolver="dynamic", writable="assistant_setup",
            owner="Assistant setup", change_behavior="immediate",
            storage="database"))
        register_descriptor(Descriptor(
            "LLM_EMERGENCY_STOP", "LLM emergency stop", "Security & operations",
            "Stops every ordinary external LLM request immediately.", "boolean",
            False, resolver="dynamic", writable="assistant_setup",
            owner="Assistant setup", change_behavior="immediate", storage="database"))
        register_descriptor(Descriptor(
            "LLM_AUTONOMOUS_INCIDENT_TRIAGE_ENABLED", "Autonomous incident triage",
            "Security & operations",
            "Allows catch-all triage only for incidents created after activation.",
            "boolean", False, resolver="dynamic", writable="assistant_setup",
            owner="Assistant setup", change_behavior="immediate", storage="database"))
        register_descriptor(Descriptor(
            "ASSISTANT_MCP_PATH", "MCP endpoint path", "Security & operations",
            "Request path of the Assistant's MCP endpoint; also the registered "
            "OAuth resource path.",
            "string", "api/assistant/mcp", resolver="static", writable="none",
            owner="Deployment settings", change_behavior="restart"))


def register_oauth_resources(mcp_path, api_root, enabled, registry=None):
    """Register the MCP door and, when it is safe to, the REST API root.

    Both ride the ONE `enabled` callable — ASSISTANT_MCP_ENABLED means "remote
    agents may sign in", for either kind of access — and it is re-read on every
    check, so the switch is immediate in both directions.

    `api_root == "/"` (an installation with `MOJO_PREFIX=""`) is REFUSED, and
    the MCP registration still proceeds. A prefix resource is covered by
    `path.rstrip("/") + "/"`, so a root entry would reach every path this host
    serves — the hosted sign-in pages and any other product sharing the host,
    not merely the REST API. "Full API access" cannot honestly mean that, so
    such an installation simply does not offer the `api` scope: discovery never
    advertises it and consent answers `invalid_scope`. The tool door is
    unaffected.
    """
    from mojo.apps.account.services import oauth_server

    oauth_server.register_resource(mcp_path, ["mcp"], enabled, registry=registry)
    if not str(api_root or "").strip("/"):
        logit.error(
            "assistant.mcp",
            f"the REST API root resolves to {api_root!r}; refusing to register "
            f"a prefix OAuth resource at the site root, where it would cover "
            f"every path this host serves. Full API access (the `api` scope) "
            f"is not offered on this installation — set MOJO_PREFIX to mount "
            f"the API under a path.")
        return None
    return oauth_server.register_resource(
        api_root, ["mcp", "api"], enabled, registry=registry, prefix=True)
