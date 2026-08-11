"""Curated Settings feature capabilities."""


def describe(request, capabilities):
    values = {
        "view": bool(capabilities.get("settings")),
        "catalog_write": bool(capabilities.get("catalog_write")),
        "owner_display": bool(capabilities.get("settings_owner_display")),
        "owner_edit": bool(capabilities.get("settings_owner_edit")),
    }
    return {"id": "settings", "enabled": values["view"], "capabilities": values}
