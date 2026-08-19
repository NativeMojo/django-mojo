"""WebApps feature capabilities."""


def describe(request, capabilities):
    values = {
        "view": bool(capabilities.get("webapps")),
        "manage": bool(capabilities.get("manage_webapps")),
        "onboard": bool(capabilities.get("manage_webapps")),
    }
    # Same ordering care as the platform provider: read `enabled` from the
    # authority value before the installation-wide flag joins `values`, so a
    # flag that is true everywhere can never open the lane.
    enabled = values["view"]
    values["infrastructure_managed"] = bool(
        capabilities.get("infrastructure_managed"))
    return {"id": "webapps", "enabled": enabled,
            "capabilities": values,
            "contracts": {"onboarding": 1, "summary": 1}}
