"""Platform health, deployment, and System Setup capabilities."""


def describe(request, capabilities):
    values = {
        "setup": bool(capabilities.get("setup")),
        "view": bool(capabilities.get("view_platform")),
        "manage": bool(capabilities.get("manage_platform")),
        "security": bool(capabilities.get("view_platform_security")),
    }
    return {"id": "platform", "enabled": any(values.values()),
            "capabilities": values}
