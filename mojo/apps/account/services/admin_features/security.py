"""Fail-closed Admin Security feature capability provider."""

from django.apps import apps


def describe(request, capabilities):
    available = apps.is_installed("mojo.apps.incident")
    values = {
        "view": bool(available and capabilities.get("view_security")),
        "manage": bool(available and capabilities.get("manage_security")),
    }
    return {
        "id": "security",
        "enabled": values["view"],
        "capabilities": values,
    }
