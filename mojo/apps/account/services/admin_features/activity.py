"""Activity feature capabilities kept separate by source authority."""


def describe(request, capabilities):
    has = request.user.has_permission
    values = {
        "view_logs": has(["view_logs", "manage_logs", "security", "admin"]),
        "view_security": has(["view_security", "manage_security", "security", "admin"]),
        "manage_security": has(["manage_security", "security", "admin"]),
    }
    return {"id": "activity", "enabled": any(values.values()), "capabilities": values}
