"""Text messages (SMS) feature capabilities for the built-in Admin portal."""

from django.apps import apps


def describe(request, capabilities):
    installed = apps.is_installed("mojo.apps.phonehub")
    values = {
        "view": bool(installed and capabilities.get("messaging_sms")),
        "system_write": bool(
            installed and capabilities.get("messaging_sms_system_write")),
    }
    return {"id": "sms", "enabled": values["view"], "capabilities": values}
