"""People feature capabilities."""


def describe(request, capabilities):
    values = {
        "users": bool(capabilities.get("people")),
        "groups": bool(capabilities.get("groups")),
        "manage_users": bool(capabilities.get("manage_users")),
        "manage_groups": bool(capabilities.get("manage_groups")),
        "manage_api_keys": bool(capabilities.get("manage_api_keys")),
        "view_logins": bool(capabilities.get("view_logins")),
        "view_logs": bool(capabilities.get("view_logs")),
        "view_events": bool(capabilities.get("view_events")),
        "view_incidents": bool(capabilities.get("view_incidents")),
        "view_tickets": bool(capabilities.get("view_tickets")),
    }
    return {"id": "people", "enabled": values["users"] or values["groups"],
            "capabilities": values}
