"""
AWS Maintenance REST Endpoints

    GET  /api/aws/maintenance/versions  - pending managed-service upgrades (?refresh=1)
    GET  /api/aws/maintenance/status    - live progress for one resource
    POST /api/aws/maintenance/apply     - request ONE engine-version upgrade

The reads need ``manage_aws``. The write needs ``manage_aws`` AND a platform
management tier (superuser, ``manage_platform``, or ``admin``) — an AND, not an
either/or. ``manage_aws`` alone is the grant that reads CloudWatch charts; it
is not the grant that reboots the production database. The in-body
``_require_manage_tier`` check is what makes it an AND: decorators compose the
listed permissions with OR.

The write is additionally denied to key-backed sessions and demands fresh
interactive auth, on the same reasoning as the platform deploy endpoints.

Confirmation is server-verified, not client-decorated: ``confirm_resource``
must echo the identifier exactly, and the mismatch is refused BEFORE any
provider call. ``apply_immediately`` has no default — "now" and "next
maintenance window" are different outage decisions, and a missing field is a
question that was never asked, not a "no".

``INFRASTRUCTURE_MODE`` is checked before any of that. It is a property of the
INSTALLATION, not of the caller — on an install whose AWS estate is applied by
an external IaC pipeline nobody may apply an upgrade here, however privileged.
So it is the first statement in the apply body (route decorators still run
first, being decorators), and no amount of extra grants changes the answer.
"""

from mojo import decorators as md
from mojo import errors as me
from mojo.helpers import infrastructure
from mojo.helpers import logit
from mojo.helpers.response import JsonResponse
from mojo.apps.aws.services import maintenance as maintenance_service


logger = logit.get_logger("aws_maintenance", "aws.log")


def _truthy(value):
    return str(value or "").strip().lower() in ("1", "true", "yes")


def _require_manage_tier(request):
    """The AND half of the write gate.

    ``requires_global_perms`` already proved ``manage_aws``. This proves the
    caller also holds platform authority, which the decorator cannot express
    because every permission it lists is satisfied on its own.
    """
    user = request.user
    if getattr(user, "is_superuser", False):
        return
    if user.has_permission(["manage_platform", "admin"]):
        return
    logger.error("maintenance apply denied: %s lacks a platform management tier",
                 getattr(user, "username", user))
    raise me.PermissionDeniedException(
        "Applying an infrastructure upgrade requires platform management authority "
        "in addition to manage_aws.")


def _error(error):
    return JsonResponse({
        "status": False,
        "error": error.message,
        "error_code": error.error_code,
        "data": error.data,
    }, status=error.status)


def _text(data, name):
    value = data.get(name)
    if not isinstance(value, str) or not value.strip():
        raise me.ValueException(f"{name} must be a nonempty string")
    return value.strip()


@md.GET("maintenance/versions")
@md.requires_global_perms("manage_aws")
def on_maintenance_versions(request):
    """Pending upgrades plus live status. ``refresh=1`` bypasses the cache."""
    refresh = _truthy(request.DATA.get("refresh"))
    try:
        return JsonResponse({"status": True,
                             "data": maintenance_service.report(refresh=refresh)})
    except maintenance_service.MaintenanceError as error:
        return _error(error)


@md.GET("maintenance/status")
@md.requires_global_perms("manage_aws")
def on_maintenance_status(request):
    """Live progress for one resource, polled while an upgrade runs."""
    data = request.DATA
    kind = _text(data, "kind")
    resource = _text(data, "resource")
    target = data.get("target_version") or data.get("target") or ""
    try:
        return JsonResponse({"status": True, "data": maintenance_service.resource_status(
            kind, resource, str(target).strip() or None)})
    except maintenance_service.MaintenanceError as error:
        return _error(error)


@md.POST("maintenance/apply")
@md.denies_key_backed_session()
@md.requires_fresh_auth(seconds=600)
@md.requires_global_perms("manage_aws")
def on_maintenance_apply(request):
    """Request one engine-version upgrade the server itself is offering."""
    denied = infrastructure.refuse("Applying an engine-version upgrade")
    if denied is not None:
        return denied
    _require_manage_tier(request)
    data = request.DATA
    kind = _text(data, "kind")
    resource = _text(data, "resource")
    target_version = _text(data, "target_version")

    # Typed confirmation, checked before anything reaches AWS. An operator who
    # cannot reproduce the identifier is not looking at the resource they think.
    if data.get("confirm_resource") != resource:
        raise me.ValueException(
            "confirm_resource must exactly match the resource identifier")

    if "apply_immediately" not in data:
        raise me.ValueException(
            "apply_immediately is required: choose the next maintenance window "
            "(false) or an immediate apply (true)")
    apply_immediately = data.get("apply_immediately")
    if type(apply_immediately) is not bool:
        raise me.ValueException("apply_immediately must be a boolean")

    try:
        result = maintenance_service.apply_upgrade(
            request.user, kind, resource, target_version, apply_immediately)
    except maintenance_service.MaintenanceError as error:
        return _error(error)
    from mojo.apps.account.services import admin_platform
    admin_platform.audit_after_commit(
        request.user, "aws_engine_upgrade",
        f"{kind}:{resource}:{result['target_version']}")
    return JsonResponse({"status": True, "data": result})
