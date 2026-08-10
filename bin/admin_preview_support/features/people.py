NAME = "people"


def describe(capabilities):
    values = {
        "users": capabilities["people"],
        "groups": capabilities["groups"],
        "manage_users": capabilities["manage_users"],
        "manage_groups": capabilities["manage_groups"],
        "manage_api_keys": capabilities["manage_api_keys"],
        "view_logins": capabilities["view_logins"],
        "view_logs": capabilities["view_logs"],
        "view_events": capabilities["view_events"],
        "view_incidents": capabilities["view_incidents"],
        "view_tickets": capabilities["view_tickets"],
    }
    return {"id": NAME, "enabled": any(values.values()), "capabilities": values}


def reset(handler, fixtures, **options):
    handler.users = [dict(row) for row in fixtures["users"]]
    handler.groups = [dict(row) for row in fixtures["groups"]]
    handler.members = [dict(row) for row in fixtures["members"]]
    handler.api_keys = [dict(row) for row in fixtures["api_keys"]]
    handler.permission_bundles = {1: ["people", "platform"], 2: ["people"]}
