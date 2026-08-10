"""People feature capabilities."""


def describe(request, capabilities):
    values = {
        "users": bool(capabilities.get("people")),
        "groups": bool(capabilities.get("groups")),
    }
    return {"id": "people", "enabled": any(values.values()),
            "capabilities": values}
