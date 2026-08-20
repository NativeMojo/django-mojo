"""
AWS Capacity REST Endpoints

    GET  /api/aws/capacity         - the fleet, reader and replica picture (?refresh=1)
    GET  /api/aws/capacity/status  - live progress for one capacity operation
    POST /api/aws/capacity/apply   - request ONE capacity change

The reads need ``manage_aws``. The write needs ``manage_aws`` AND a literal
superuser — a stricter gate than the Maintenance apply next door, and
deliberately so. An engine-version upgrade changes a resource this
installation already owns; these actions CREATE and DESTROY infrastructure:
a node that will serve production traffic, a database instance that bills by
the hour, a cache replica that is somebody's failover. ``manage_platform`` is
the grant that redeploys the fleet. It is not the grant that terminates it.

The superuser check is in the body rather than in a decorator because
``requires_global_perms`` composes what it lists with OR — every listed
permission satisfies the gate on its own, so no combination of decorator
arguments can express "manage_aws AND is_superuser".

Confirmation is server-verified, not client-decorated: ``confirm_resource``
must echo the identifier exactly, and the mismatch is refused BEFORE any
provider call. ``count`` and ``apply_immediately`` have no defaults — a
missing field is a question that was never asked, not a "no".

``INFRASTRUCTURE_MODE`` is checked before all of it. It is a property of the
INSTALLATION, not of the caller — on an install whose AWS estate is applied by
an external IaC pipeline, nobody adds a node here, however privileged. So it is
the first statement in the apply body (route decorators still run first, being
decorators), and no amount of extra grants changes the answer.
"""

from mojo import decorators as md
from mojo import errors as me
from mojo.helpers import infrastructure
from mojo.helpers import logit
from mojo.helpers.response import JsonResponse
from mojo.apps.aws.services import capacity as capacity_service


logger = logit.get_logger("aws_capacity", "aws.log")

# The audit action recorded per capacity action. Named individually rather than
# interpolated so the audit trail cannot be steered by a request body.
AUDIT_ACTIONS = {
    capacity_service.ACTION_ADD_NODE: "aws_capacity_add_node",
    capacity_service.ACTION_DRAIN_NODE: "aws_capacity_drain_node",
    capacity_service.ACTION_TERMINATE_NODE: "aws_capacity_terminate_node",
    capacity_service.ACTION_ADD_READER: "aws_capacity_add_reader",
    capacity_service.ACTION_REMOVE_READER: "aws_capacity_remove_reader",
    capacity_service.ACTION_SET_CACHE_REPLICAS: "aws_capacity_set_cache_replicas",
    capacity_service.ACTION_ENABLE_STABLE_IPS: "aws_capacity_enable_stable_ips",
    capacity_service.ACTION_DISABLE_STABLE_IPS: "aws_capacity_disable_stable_ips",
}

# Actions that operate on the whole fleet rather than one named resource. The
# typed echo for these is the ACTION WORD the panel showed the operator.
FLEET_ACTIONS = (capacity_service.ACTION_ADD_NODE,
                 capacity_service.ACTION_ENABLE_STABLE_IPS,
                 capacity_service.ACTION_DISABLE_STABLE_IPS)


def _truthy(value):
    return str(value or "").strip().lower() in ("1", "true", "yes")


def _require_superuser(request):
    """The AND half of the write gate.

    ``requires_global_perms`` already proved ``manage_aws``. This proves the
    caller is a literal superuser, which the decorator cannot express.
    """
    if getattr(request.user, "is_superuser", False):
        return
    logger.error("capacity apply denied: %s is not a superuser",
                 getattr(request.user, "username", request.user))
    raise me.PermissionDeniedException(
        "Creating or destroying infrastructure requires a superuser account in "
        "addition to manage_aws.")


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


@md.GET("capacity")
@md.requires_global_perms("manage_aws")
def on_capacity(request):
    """The fleet, reader and replica picture. ``refresh=1`` bypasses the cache.

    A READ, available in every infrastructure mode. An install whose estate is
    applied externally still gets to see what it has — the mode is reported in
    the envelope so the panel hides the controls rather than the facts.
    """
    refresh = _truthy(request.DATA.get("refresh"))
    try:
        return JsonResponse({"status": True,
                             "data": capacity_service.report(refresh=refresh)})
    except capacity_service.CapacityError as error:
        return _error(error)


@md.GET("capacity/status")
@md.requires_global_perms("manage_aws")
def on_capacity_status(request):
    """Live progress for one capacity operation, polled while it runs."""
    operation = _text(request.DATA, "operation")
    try:
        return JsonResponse({
            "status": True,
            "data": capacity_service.operation_status(operation)})
    except capacity_service.CapacityError as error:
        return _error(error)


@md.POST("capacity/apply")
@md.denies_key_backed_session()
@md.requires_fresh_auth(seconds=600)
@md.requires_global_perms("manage_aws")
def on_capacity_apply(request):
    """Request ONE capacity change the server itself is offering."""
    denied = infrastructure.refuse("Changing capacity")
    if denied is not None:
        return denied
    _require_superuser(request)
    data = request.DATA
    action = _text(data, "action")
    if action not in capacity_service.ACTIONS:
        raise me.ValueException(f"Unknown capacity action '{action}'")

    # Fleet-wide actions have no resource of their own — the server derives
    # the targets — so their typed echo is ALWAYS the action word the panel
    # showed. A caller-supplied resource is ignored outright: honoring it
    # would let the caller choose their own echo and steer the audit subject.
    resource = "" if action in FLEET_ACTIONS else _text(data, "resource")
    expected = resource or action

    # Typed confirmation, checked before anything reaches AWS. An operator who
    # cannot reproduce the identifier is not looking at the resource they think.
    if data.get("confirm_resource") != expected:
        raise me.ValueException(
            "confirm_resource must exactly match the resource identifier")

    params = {}
    if action == capacity_service.ACTION_ENABLE_STABLE_IPS:
        assign = data.get("assign")
        if assign is not None:
            if (not isinstance(assign, dict) or not assign or any(
                    not isinstance(key, str) or not key.strip()
                    or not isinstance(value, str) or not value.strip()
                    for key, value in assign.items())):
                raise me.ValueException(
                    "assign must be a non-empty object mapping instance ids "
                    "to Elastic IP allocation ids")
            params = {"assign": {key.strip(): value.strip()
                                 for key, value in assign.items()}}
    if action == capacity_service.ACTION_SET_CACHE_REPLICAS:
        if "count" not in data:
            raise me.ValueException(
                "count is required: state the number of replicas the group "
                "should have when this finishes")
        count = data.get("count")
        if type(count) is not int or type(count) is bool:
            raise me.ValueException("count must be a whole number")
        if "apply_immediately" not in data:
            raise me.ValueException(
                "apply_immediately is required: ElastiCache applies a "
                "replica-count change immediately and offers no maintenance "
                "window")
        apply_immediately = data.get("apply_immediately")
        if type(apply_immediately) is not bool:
            raise me.ValueException("apply_immediately must be a boolean")
        params = {"count": count, "apply_immediately": apply_immediately}

    try:
        result = capacity_service.apply(request.user, action, resource, **params)
    except capacity_service.CapacityError as error:
        return _error(error)
    from mojo.apps.account.services import admin_platform
    admin_platform.audit_after_commit(
        request.user, AUDIT_ACTIONS.get(action, "aws_capacity"),
        f"{resource or 'fleet'}:{result.get('id')}")
    return JsonResponse({"status": True, "data": result})
