"""Activity capabilities kept separate by source authority."""

from django.apps import apps


def describe(request, capabilities):
    has = request.user.has_permission
    incident = apps.is_installed("mojo.apps.incident")
    can_view_security = bool(incident and has([
        "view_security", "manage_security", "security", "admin"]))
    can_manage_security = bool(incident and has([
        "manage_security", "security", "admin"]))
    values = {
        "view_logs": has(["view_logs", "manage_logs", "security", "admin"]),
        # Compatibility keys keep Admin v1's four-lane Activity page intact.
        "view_security": can_view_security,
        "manage_security": can_manage_security,
        # v2 names the retained ticket lane explicitly.
        "view_tickets": can_view_security,
        "manage_tickets": can_manage_security,
    }
    return {"id": "activity", "enabled": any(values.values()), "capabilities": values}
