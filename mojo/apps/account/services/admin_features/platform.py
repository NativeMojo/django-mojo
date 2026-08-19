"""Platform health, deployment, and System Setup capabilities."""


def describe(request, capabilities):
    values = {
        "setup": bool(capabilities.get("setup")),
        # Rides in `values` and therefore into `enabled`, which is harmless:
        # attention is only ever true when `setup` is already true.
        "setup_attention": bool(capabilities.get("setup_attention")),
        "view": bool(capabilities.get("view_platform")),
        "manage": bool(capabilities.get("manage_platform")),
        "security": bool(capabilities.get("view_platform_security")),
        "advanced": bool(capabilities.get("view_advanced") or
                         capabilities.get("manage_advanced") or
                         capabilities.get("view_advanced_inventory") or
                         capabilities.get("view_advanced_security") or
                         capabilities.get("view_advanced_settings")),
        # CloudWatch charts read AWS directly, so the lane follows the AWS
        # grant rather than the platform-evidence grants above.
        "metrics": bool(capabilities.get("manage_aws")),
        # Maintenance reads AWS directly, so the lane follows the same grant as
        # Metrics. Applying an upgrade needs a platform tier on top of it; that
        # second gate lives on the endpoint, not on whether the page is offered.
        "maintenance": bool(capabilities.get("manage_aws")),
    }
    return {"id": "platform", "enabled": any(values.values()),
            "capabilities": values}
