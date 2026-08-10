"""Advanced/System Setup feature capabilities."""


def describe(request, capabilities):
    enabled = bool(capabilities.get("setup"))
    return {"id": "advanced", "enabled": enabled,
            "capabilities": {"setup": enabled}}
