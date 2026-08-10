"""Platform/System Setup feature capabilities."""


def describe(request, capabilities):
    enabled = bool(capabilities.get("setup"))
    return {"id": "platform", "enabled": enabled,
            "capabilities": {"setup": enabled}}
