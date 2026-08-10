"""Advanced raw-network feature capabilities."""


def describe(request, capabilities):
    values = {
        "view": bool(capabilities.get("network")),
        "manage": bool(capabilities.get("manage_network")),
    }
    return {"id": "advanced", "enabled": values["view"],
            "capabilities": values}
