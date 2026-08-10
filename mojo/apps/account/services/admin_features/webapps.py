"""WebApps feature capabilities."""


def describe(request, capabilities):
    values = {
        "view": bool(capabilities.get("webapps")),
        "manage": bool(capabilities.get("manage_webapps")),
    }
    return {"id": "webapps", "enabled": values["view"],
            "capabilities": values}
