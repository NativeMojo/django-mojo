"""Platform/network feature capabilities."""


def describe(request, capabilities):
    values = {
        "view": bool(capabilities.get("network")),
        "manage": bool(capabilities.get("manage_network")),
    }
    return {"id": "platform", "enabled": values["view"],
            "capabilities": values}
