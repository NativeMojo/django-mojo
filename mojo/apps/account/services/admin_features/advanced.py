"""Advanced raw-network feature capabilities."""


def describe(request, capabilities):
    values = {
        "view": bool(capabilities.get("network") or capabilities.get("view_advanced")),
        "manage": bool(capabilities.get("manage_network") or capabilities.get("manage_advanced")),
        "inventory": bool(capabilities.get("view_advanced_inventory")),
        "security": bool(capabilities.get("view_advanced_security")),
        "settings": bool(capabilities.get("view_advanced_settings")),
    }
    return {"id": "advanced", "enabled": any(values.values()),
            "capabilities": values}
