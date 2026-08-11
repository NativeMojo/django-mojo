from copy import deepcopy


NAME = "advanced"


def describe(capabilities):
    values = {"view": capabilities["network"] or capabilities["view_advanced"],
              "manage": capabilities["manage_network"] or capabilities["manage_advanced"],
              "inventory": capabilities["view_advanced_inventory"],
              "security": capabilities["view_advanced_security"],
              "settings": capabilities["view_advanced_settings"]}
    return {"id": NAME, "enabled": any(values.values()), "capabilities": values}


def reset(handler, fixtures, **options):
    for name in ("records", "credentials", "vhosts", "routes"):
        setattr(handler, name, deepcopy(fixtures[name]))


def get(handler, parsed):
    if parsed.path != "/api/account/admin/advanced":
        return None
    now = "2026-08-10T18:00:00Z"
    section = lambda data: {"status": "healthy", "observed_at": now,
                            "stale_after": "2026-08-10T18:10:00Z", "data": data}
    return 200, {"schema_version": 1, "sections": {
        "hosting": section({"domains": {"active": 3}, "certificates": 2, "vhosts": 3, "upstreams": 3, "routes": 2}),
        "aws_inventory": section({"configured": True, "resources": {"ec2": [{"id": "i-preview", "state": "running"}], "rds": [], "redis": []}}),
        "network_security": section({"SECURE_SSL_REDIRECT": {"configured": True}}),
    }}


def post(handler, path, payload):
    if path != "/api/account/admin/advanced/settings":
        return None
    return 200, {"schema_version": 1, "saved": True, "value": payload}
