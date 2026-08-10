"""Dashboard feature capabilities."""


def describe(request, capabilities):
    return {"id": "dashboard", "enabled": True, "capabilities": {"view": True}}
