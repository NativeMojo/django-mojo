"""Platform health, deployment, and System Setup capabilities."""


def describe(request, capabilities):
    values = {
        "setup": bool(capabilities.get("setup")),
        "view": bool(capabilities.get("view_platform")),
        "manage": bool(capabilities.get("manage_platform")),
        "security": bool(capabilities.get("view_platform_security")),
        "advanced": bool(capabilities.get("view_advanced") or
                         capabilities.get("manage_advanced") or
                         capabilities.get("view_advanced_inventory") or
                         capabilities.get("view_advanced_security") or
                         capabilities.get("view_advanced_settings")),
    }
    return {"id": "platform", "enabled": any(values.values()),
            "capabilities": values}
