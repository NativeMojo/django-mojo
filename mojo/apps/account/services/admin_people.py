"""Versioned, lossless high-level permission bundles for Admin People."""

from mojo import errors as merrors
from mojo.apps.account.models import User


PERMISSION_BUNDLE_VERSION = 1
PERMISSION_BUNDLES = {
    "people": {
        "label": "People",
        "permissions": ("view_users", "manage_users", "view_groups", "manage_groups"),
    },
    "platform": {
        "label": "Platform",
        "permissions": ("view_admin", "view_global", "manage_aws"),
    },
    "network_hosting": {
        "label": "Network & Hosting",
        "permissions": ("view_dns", "manage_dns"),
    },
    "deployments": {
        "label": "Deployments",
        "permissions": ("manage_webapp", "manage_deploy", "view_github", "manage_github"),
    },
    "security_incidents": {
        "label": "Security & Incidents",
        "permissions": ("view_security", "manage_security"),
    },
    "logs_metrics": {
        "label": "Logs & Metrics",
        "permissions": ("view_logs", "manage_logs", "view_metrics", "manage_metrics"),
    },
    "system_administration": {
        "label": "System Administration",
        "permissions": ("manage_settings", "view_jobs", "manage_jobs", "view_taskqueue",
                        "admin_compliance", "admin_verify"),
    },
}


def _target(user_id):
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        raise merrors.ValueException("user must be an integer")
    user = User.objects.filter(pk=user_id).first()
    if user is None:
        raise merrors.ValueException("User not found")
    return user


def _selected(user):
    permissions = user.permissions if isinstance(user.permissions, dict) else {}
    return [name for name, spec in PERMISSION_BUNDLES.items()
            if all(bool(permissions.get(key)) for key in spec["permissions"])]


def describe(user_id):
    user = _target(user_id)
    return {
        "version": PERMISSION_BUNDLE_VERSION,
        "user": user.pk,
        "selected": _selected(user),
        "bundles": [
            {"id": name, "label": spec["label"],
             "permissions": list(spec["permissions"])}
            for name, spec in PERMISSION_BUNDLES.items()
        ],
    }


def apply(request, user_id, version, selected):
    try:
        version = int(version)
    except (TypeError, ValueError):
        raise merrors.ValueException("version must be an integer")
    if version != PERMISSION_BUNDLE_VERSION:
        raise merrors.ValueException("Permission bundle version is stale")
    if not isinstance(selected, list) or any(
            not isinstance(name, str) or name not in PERMISSION_BUNDLES
            for name in selected):
        raise merrors.ValueException("selected must contain known permission bundle ids")

    user = _target(user_id)
    wanted = set()
    for name in selected:
        wanted.update(PERMISSION_BUNDLES[name]["permissions"])
    managed = {permission for spec in PERMISSION_BUNDLES.values()
               for permission in spec["permissions"]}
    current = user.permissions if isinstance(user.permissions, dict) else {}
    changes = {permission: True if permission in wanted else False
               for permission in managed if bool(current.get(permission)) != (permission in wanted)}
    if changes:
        user.set_permissions(changes)
        user.save(update_fields=["permissions", "modified"])
        user.log(
            f"Permission bundles updated by administrator {request.user.pk}",
            "permission:bundles_updated")
    return describe(user.pk)
