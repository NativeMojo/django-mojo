"""Admin Assistant capabilities for the built-in Admin portal.

The Assistant is a shell-level panel, not a navigation lane: it has no route
and no registry descriptor. This namespace exists so the shell can decide
whether to mount the panel at all, and what the panel may offer once mounted.
"""

from django.apps import apps


def describe(request, capabilities):
    # Both applications are required. The panel talks over the realtime
    # WebSocket and reads conversation history from the assistant REST surface;
    # with either missing there is nothing to offer.
    installed = (apps.is_installed("mojo.apps.assistant") and
                 apps.is_installed("mojo.apps.realtime"))
    values = {
        "view": bool(installed and capabilities.get("assistant")),
        # A fact about the installation, not about this caller: whether the
        # feature is enabled and a credential resolves. It rides in `values` so
        # the panel can render the not-configured state instead of a composer.
        "ready": bool(installed and capabilities.get("assistant_ready")),
        "setup": bool(installed and capabilities.get("assistant_setup")),
    }
    # `enabled` reads the authority value ALONE — never any(values.values()).
    # `ready` is installation state, so folding it in would mount the panel for
    # a caller the WebSocket handler will refuse on every message.
    return {"id": "assistant", "enabled": values["view"], "capabilities": values}
