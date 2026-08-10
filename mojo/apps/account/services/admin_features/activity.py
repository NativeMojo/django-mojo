"""Reserved Activity feature capabilities."""


def describe(request, capabilities):
    return {"id": "activity", "enabled": False, "capabilities": {"view": False}}
