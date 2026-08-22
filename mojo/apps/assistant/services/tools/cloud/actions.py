"""Cloud domain — the mutating tools.

Seven thin wrappers, each mirroring exactly ONE Admin endpoint. No new
authority is created here: every tool declares the gates its twin declares, and
#2569's bound approval replaces the browser's typed echo (``confirm_resource`` /
``confirm_version``), which proves a human is looking at the resource — a model
retyping a string proves nothing. The SERVER-side half of each echo is kept:
the version must equal what this installation is offering, the target must be
one ``offered_target`` returned, the action must be one ``report()["actions"]``
currently offers, and the deployment's status must be one the Admin offers that
control for.

``preview`` is the fail-closed pre-flight: raising it refuses the proposal as an
ordinary tool error with NO record created, and refuses an already-approved
action as ``precondition_failed``. ``revision`` binds the facts the decision
rests on, so a fleet, release or status that moves between proposal and approval
fails closed.
"""

from mojo.apps.assistant import tool
from mojo.apps.aws.services import capacity as capacity_service
from mojo.apps.aws.services import maintenance as maintenance_service
from mojo.helpers import logit

from .common import (
    actor_request, audit, bounded, can_system_admin, maintenance_tier, refuse,
)
from .reads import project_deployment


logger = logit.get_logger("assistant", "assistant.log")

APPROVAL_NOTE = ("Requires operator approval: calling this tool creates an "
                 "approval card and does not execute.")

PLATFORM_WRITE_PERMS = ["manage_platform", "admin"]

# @md.requires_fresh_auth(seconds=600) sits on every endpoint mirrored here.
FRESH_AUTH = 600

RECONCILE_DEPLOY = (
    "An ambiguous outcome is NOT retried automatically. Re-read "
    "get_platform_overview(sections=['deployments']) for the authoritative "
    "state.")
RECONCILE_CAPACITY = (
    "Poll get_capacity_operation_status for the authoritative state. Nothing "
    "is retried automatically.")


# --- deployment recovery --------------------------------------------------

def _deployment(params):
    """Resolve the row, or raise the refusal ``preview`` turns into an error."""
    from mojo.apps.edge.services import platform_deploy

    row = platform_deploy.get(str(params.get("deployment") or "").strip())
    if row is None:
        raise ValueError("That platform deployment id is not on record.")
    return row


def _require_offered(row, action):
    """Refuse a control the Admin does not offer for this attempt's status.

    Stricter than the endpoint, deliberately: the endpoints enforce no status
    at all, but the Admin never OFFERS these controls in these states, and a
    tool that mirrors the Admin must mirror what it offers. An active attempt
    earns nothing — the orchestrator is driving it.
    """
    from mojo.apps.edge.services import platform_deploy

    offered = platform_deploy.actions_for_status(row.status)
    if action in offered:
        return
    if row.status in platform_deploy.ACTIVE_STATUSES:
        raise ValueError(
            f"This deployment is {row.status} — the orchestrator is still "
            f"driving it. Wait for it to settle, then re-read the deployments "
            f"section.")
    raise ValueError(
        f"The Admin does not offer '{action}' for a {row.status} deployment. "
        f"It offers: {', '.join(offered) or 'nothing'}.")


def _deploy_revision(row):
    """The bound facts: this attempt, in this state."""
    return f"{row.pk}:{row.status}"


def _deploy_details(row):
    from mojo.apps.edge.services import platform_deploy

    return {
        "deployment": str(row.pk), "sha": row.sha,
        "framework_version": row.framework_version, "status": row.status,
        "node_summary": platform_deploy._node_summary(row),
    }


def _preview_retry(params, user):
    row = _deployment(params)
    _require_offered(row, "retry")
    summary = (
        f"Redeploy commit {row.sha[:7]} across the fleet — the SAME commit this "
        f"failed attempt used. This starts a new deployment attempt; it does "
        f"not undo anything.")
    return {"summary": summary, "details": _deploy_details(row),
            "revision": _deploy_revision(row)}


def _preview_verify(params, user):
    row = _deployment(params)
    _require_offered(row, "verify")
    expected = len(row.frozen_roster or [])
    summary = (
        f"Ask each of the {expected} frozen roster nodes for live UUID/SHA "
        f"proof for deployment {row.sha[:7]} and record the result. Reads the "
        f"fleet; changes no code.")
    return {"summary": summary, "details": _deploy_details(row),
            "revision": _deploy_revision(row)}


def _preview_converge(params, user):
    row = _deployment(params)
    _require_offered(row, "converge")
    summary = (
        f"Publish the already-desired hosting state to every pool, then re-read "
        f"UUID proof for deployment {row.sha[:7]}. Publishes existing desired "
        f"state; it does not choose a new commit.")
    return {"summary": summary, "details": _deploy_details(row),
            "revision": _deploy_revision(row)}


def _summarize_retry(params, user):
    return f"Retry platform deployment {params.get('deployment')} on the same commit."


def _summarize_verify(params, user):
    return f"Verify platform deployment {params.get('deployment')} against the fleet."


def _summarize_converge(params, user):
    return f"Converge platform deployment {params.get('deployment')} across the pools."


_DEPLOYMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "deployment": {
            "type": "string",
            "description": (
                "A platform deployment id from "
                "get_platform_overview(sections=['deployments'])."),
        },
    },
    "required": ["deployment"],
}


@tool(
    name="retry_platform_deployment",
    domain="cloud",
    permission=PLATFORM_WRITE_PERMS,
    description=(
        "Re-run a FAILED platform deployment on the same commit. Never picks a "
        "new commit and never undoes a deploy — new commits come from CI. "
        + APPROVAL_NOTE),
    input_schema=_DEPLOYMENT_SCHEMA,
    mutates=True,
    # Admin twin: POST /api/account/admin/platform/deploy/retry
    # (denies_key_backed_session + requires_fresh_auth(600), manage_platform |
    # admin). It does NOT call infrastructure.refuse(), so neither does this —
    # copying the Admin, not tightening it.
    fresh_auth_seconds=FRESH_AUTH,
    summarize=_summarize_retry,
    preview=_preview_retry,
)
def _tool_retry_platform_deployment(params, user, *, approval=None,
                                    conversation=None):
    from mojo.apps.account.services import admin_platform
    from mojo.apps.edge.services import deploy, platform_deploy

    row = platform_deploy.get(str(params.get("deployment") or "").strip())
    if row is None:
        return refuse("That platform deployment id is not on record.",
                      "unknown_resource")
    try:
        started, made = deploy.request_deploy(
            row.sha, actor=f"admin:{user.pk}", source="admin_retry",
            created_by=user,
            idempotency_key=str(approval.uuid) if approval is not None else None,
            retry_of=row, return_deployment=True)
    except deploy.DeploymentCoordinationError:
        return refuse(
            "Deploy coordination is unavailable, so nothing was started.",
            "coordination_unavailable")
    admin_platform.audit_after_commit(user, "retry_same_sha", made.pk)
    audit(user, "retry_same_sha", made.pk, conversation=conversation,
          model_name="edge.PlatformDeployment")
    return {
        "queued": bool(started),
        "deployment": project_deployment(platform_deploy.serialize(made)),
        "reconciliation": RECONCILE_DEPLOY,
    }


@tool(
    name="verify_platform_deployment",
    domain="cloud",
    permission=PLATFORM_WRITE_PERMS,
    description=(
        "Collect live UUID/SHA proof from each node on a deployment's frozen "
        "roster and record converged / partial / unknown. Reads the fleet. "
        + APPROVAL_NOTE),
    input_schema=_DEPLOYMENT_SCHEMA,
    mutates=True,
    # Admin twin: POST /api/account/admin/platform/deploy/verify — same gates,
    # and no infrastructure.refuse().
    fresh_auth_seconds=FRESH_AUTH,
    summarize=_summarize_verify,
    preview=_preview_verify,
)
def _tool_verify_platform_deployment(params, user, *, approval=None,
                                     conversation=None):
    from mojo.apps.account.services import admin_platform
    from mojo.apps.edge.services import platform_deploy

    identifier = str(params.get("deployment") or "").strip()
    result = platform_deploy.verify(identifier)
    if result is None:
        return refuse("That platform deployment id is not on record.",
                      "unknown_resource")
    admin_platform.audit_after_commit(user, "verify", result.pk)
    audit(user, "verify", result.pk, conversation=conversation,
          model_name="edge.PlatformDeployment")
    return {
        "deployment": project_deployment(platform_deploy.serialize(result)),
        "reconciliation": RECONCILE_DEPLOY,
    }


@tool(
    name="converge_platform_deployment",
    domain="cloud",
    permission=PLATFORM_WRITE_PERMS,
    description=(
        "Publish the already-desired hosting state to every pool and re-read "
        "deployment proof. Does not choose a new commit. " + APPROVAL_NOTE),
    input_schema=_DEPLOYMENT_SCHEMA,
    mutates=True,
    # Admin twin: POST /api/account/admin/platform/deploy/converge — same
    # gates, and no infrastructure.refuse().
    fresh_auth_seconds=FRESH_AUTH,
    summarize=_summarize_converge,
    preview=_preview_converge,
)
def _tool_converge_platform_deployment(params, user, *, approval=None,
                                       conversation=None):
    from mojo.apps.account.services import admin_platform
    from mojo.apps.edge.services import platform_deploy

    identifier = str(params.get("deployment") or "").strip()
    result = platform_deploy.converge(identifier)
    if result is None:
        return refuse("That platform deployment id is not on record.",
                      "unknown_resource")
    admin_platform.audit_after_commit(user, "converge", result.pk)
    audit(user, "converge", result.pk, conversation=conversation,
          model_name="edge.PlatformDeployment")
    return {
        "deployment": project_deployment(platform_deploy.serialize(result)),
        "reconciliation": RECONCILE_DEPLOY,
    }


# --- framework update -----------------------------------------------------

def _framework_facts(params, user):
    from mojo.apps.account.services import admin_platform
    from mojo.apps.edge.services import platform_deploy

    overview = admin_platform.framework_overview(actor_request(user))
    if not overview.get("can_update"):
        raise ValueError(
            f"A framework update is not available right now "
            f"({overview.get('blocked_reason')}).")
    version = str(params.get("version") or "").strip()
    if not version or version != (overview.get("latest") or ""):
        raise ValueError(
            f"This installation is offering {overview.get('latest')}. "
            f"Re-read get_framework_status and try again.")
    row = platform_deploy.last_converged_deployment()
    if row is None:
        raise ValueError(
            "No deployment has ever converged on this fleet, so there is no "
            "proven commit to redeploy.")
    return overview, row


def _preview_framework_update(params, user):
    overview, row = _framework_facts(params, user)
    nodes = len(row.frozen_roster or [])
    pin = overview.get("pin") or {}
    summary = (
        f"Move the fleet from django-mojo {overview.get('installed')} to "
        f"{overview.get('latest')} by redeploying the last converged commit "
        f"{row.sha[:7]} across {nodes} nodes; a version pin, if set, is "
        f"cleared first.")
    return {
        "summary": summary,
        "details": {
            "installed": overview.get("installed"),
            "latest": overview.get("latest"),
            "pin_mode": pin.get("mode"), "pin_value": pin.get("value"),
            "commit": row.sha[:7], "converged_deployment": str(row.pk),
            "nodes": nodes,
            "reconciliation": RECONCILE_DEPLOY,
        },
        # The offered release AND the converged commit. A newer release or a
        # different converged commit between proposal and approval fails closed
        # — the same refusal the endpoint spells as "This installation is
        # offering X".
        "revision": f"{overview.get('latest')}:{row.pk}",
    }


def _summarize_framework_update(params, user):
    return f"Update the fleet's django-mojo to {params.get('version')}."


@tool(
    name="apply_framework_update",
    domain="cloud",
    permission=PLATFORM_WRITE_PERMS,
    description=(
        "Move the whole fleet to the newest published django-mojo by clearing "
        "any version pin and redeploying the last converged commit. The "
        "version must be exactly what this installation is offering — read it "
        "from get_framework_status. " + APPROVAL_NOTE),
    input_schema={
        "type": "object",
        "properties": {
            "version": {
                "type": "string",
                "description": (
                    "The published version get_framework_status reports as "
                    "'latest'."),
            },
        },
        "required": ["version"],
    },
    mutates=True,
    # Admin twin: POST /api/account/admin/platform/framework/update —
    # denies_key_backed_session + requires_fresh_auth(600), manage_platform |
    # admin, and infrastructure.refuse() as its FIRST statement.
    fresh_auth_seconds=FRESH_AUTH,
    requires_managed_infrastructure=True,
    summarize=_summarize_framework_update,
    preview=_preview_framework_update,
)
def _tool_apply_framework_update(params, user, *, approval=None,
                                 conversation=None):
    from mojo import errors as merrors
    from mojo.apps.account.services import admin_platform

    try:
        result = admin_platform.apply_framework_update(
            user, str(params["version"]).strip(),
            idempotency_key=str(approval.uuid) if approval is not None else None)
    except merrors.PermissionDeniedException as exc:
        return refuse(str(exc)[:280], "permission_denied")
    except merrors.ValueException as exc:
        return refuse(str(exc)[:280], "update_refused")
    # apply_framework_update files its own admin_platform audit rows; this adds
    # only the conversation link.
    audit(user, "framework_update",
          f"{result.get('version')}:{(result.get('deployment') or {}).get('id')}",
          conversation=conversation, model_name="edge.PlatformDeployment")
    return {
        "requested": result.get("requested"),
        "version": result.get("version"),
        "cleared_pin": result.get("cleared_pin"),
        "deployment": project_deployment(result.get("deployment") or {}),
        "reconciliation": RECONCILE_DEPLOY,
    }


# --- managed engine upgrade ----------------------------------------------

def _upgrade_facts(params):
    kind = params["kind"]
    resource = str(params.get("resource") or "").strip()
    if not resource:
        raise ValueError("resource is required.")
    report = maintenance_service.report()
    row = maintenance_service.finding(kind, resource, report)
    offered = maintenance_service.offered_target(kind, resource, report)
    if row is None or not offered:
        raise ValueError(
            f"No upgrade is currently offered for {kind} {resource}. Re-read "
            f"get_managed_upgrades.")
    target = str(params.get("target_version") or "").strip()
    if target != offered:
        raise ValueError(
            f"This installation is offering {offered} for that resource, not "
            f"{target or 'nothing'}. Re-read get_managed_upgrades.")
    return row, offered


def _preview_managed_upgrade(params, user):
    row, offered = _upgrade_facts(params)
    immediate = bool(params.get("apply_immediately"))
    when = ("NOW — the engine reboots as soon as this is approved"
            if immediate else "in the next maintenance window")
    summary = (
        f"Upgrade {params['kind']} {params['resource']} from "
        f"{row.get('current_version')} to {offered}, applied {when}.")
    return {
        "summary": summary,
        "details": {
            "kind": params["kind"], "resource": params["resource"],
            "engine": row.get("engine"),
            "from_version": row.get("current_version"),
            "target_version": offered,
            "apply_immediately": immediate,
            "deadline": row.get("deadline"),
            "note": row.get("note"),
            "reconciliation": (
                "Poll get_upgrade_status for this resource. 'upgraded' is the "
                "only success signal — 'settled' means AWS finished, not that "
                "the engine moved."),
        },
        "revision": (f"{params['kind']}:{params['resource']}:"
                     f"{row.get('current_version')}:{offered}"),
    }


def _summarize_managed_upgrade(params, user):
    return (f"Upgrade {params.get('kind')} {params.get('resource')} to "
            f"{params.get('target_version')}"
            + (" immediately." if params.get("apply_immediately")
               else " in the next maintenance window."))


@tool(
    name="apply_managed_upgrade",
    domain="cloud",
    permission="manage_aws",
    description=(
        "Apply ONE managed-engine (RDS / ElastiCache) major-version upgrade "
        "this installation is offering. 'apply_immediately' has no default: "
        "true reboots the engine now, false waits for the next maintenance "
        "window — ask the operator which. " + APPROVAL_NOTE),
    input_schema={
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": list(maintenance_service.KINDS),
                     "description": "Which managed service the resource is."},
            "resource": {"type": "string",
                         "description": "The resource identifier, exactly as "
                                        "get_managed_upgrades reports it."},
            "target_version": {"type": "string",
                               "description": "Must equal the offered target."},
            "apply_immediately": {
                "type": "boolean",
                "description": (
                    "True reboots the engine now; false defers to the next "
                    "maintenance window. Required — there is no default."),
            },
        },
        "required": ["kind", "resource", "target_version", "apply_immediately"],
    },
    mutates=True,
    # Admin twin: POST /api/aws/maintenance/apply — denies_key_backed_session +
    # requires_fresh_auth(600), manage_aws AND a platform management tier
    # (_require_manage_tier), and infrastructure.refuse() first.
    fresh_auth_seconds=FRESH_AUTH,
    requires_managed_infrastructure=True,
    authorize=maintenance_tier,
    summarize=_summarize_managed_upgrade,
    preview=_preview_managed_upgrade,
)
def _tool_apply_managed_upgrade(params, user, *, approval=None,
                                conversation=None):
    from mojo.apps.account.services import admin_platform

    try:
        result = maintenance_service.apply_upgrade(
            user, params["kind"], str(params["resource"]).strip(),
            str(params["target_version"]).strip(),
            bool(params["apply_immediately"]))
    except maintenance_service.MaintenanceError as error:
        return refuse(error.message, error.error_code)
    target = f"{params['kind']}:{params['resource']}:{result['target_version']}"
    admin_platform.audit_after_commit(user, "aws_engine_upgrade", target)
    audit(user, "aws_engine_upgrade", target, conversation=conversation,
          model_name="aws.ManagedUpgrade")
    return bounded({
        "requested": result.get("requested"),
        "kind": result.get("kind"), "resource": result.get("resource"),
        "from_version": result.get("from_version"),
        "target_version": result.get("target_version"),
        "apply_immediately": result.get("apply_immediately"),
        "operation": result.get("operation"),
        "reconciliation": (
            "Poll get_upgrade_status for this resource. Nothing is retried "
            "automatically."),
    }, depth=3)


# --- capacity -------------------------------------------------------------
#
# Both capacity tools carry the strictest gate set in this domain, because
# these actions CREATE and DESTROY infrastructure. `requires_superuser` is the
# AND-half the Admin writes by hand (aws/rest/capacity.py::_require_superuser);
# `authorize=can_system_admin` is the listing-time twin of it, so the tools are
# never even OFFERED to a manage_aws holder who is not a superuser.

SIZE_KEYS = ("small", "medium", "large", "xlarge")

# Fleet-wide actions have no resource of their own — the server derives the
# targets. Mirrors FLEET_ACTIONS in mojo/apps/aws/rest/capacity.py.
FLEET_ACTIONS = (capacity_service.ACTION_ADD_NODE,
                 capacity_service.ACTION_ENABLE_STABLE_IPS,
                 capacity_service.ACTION_DISABLE_STABLE_IPS)


def _capacity_params(action, params):
    """The conditional fields the REST layer demands, enforced the same way.

    ``count``, ``size`` and ``apply_immediately`` have no defaults: a missing
    field is a question that was never asked, not a "no". ``assign`` is
    deliberately not accepted at all — it is a free-form instance→allocation
    map the server derives when omitted.
    """
    if action == capacity_service.ACTION_SET_CACHE_REPLICAS:
        count = params.get("count")
        if type(count) is not int or type(count) is bool:
            raise ValueError(
                "count is required: state the number of replicas the group "
                "should have when this finishes.")
        immediate = params.get("apply_immediately")
        if type(immediate) is not bool:
            raise ValueError(
                "apply_immediately is required: ElastiCache applies a "
                "replica-count change immediately and offers no maintenance "
                "window.")
        return {"count": count, "apply_immediately": immediate}
    if action in (capacity_service.ACTION_RESIZE_CACHE,
                  capacity_service.ACTION_RESIZE_DATABASE):
        size = str(params.get("size") or "").strip()
        if size not in SIZE_KEYS:
            raise ValueError(f"size is required, one of: {', '.join(SIZE_KEYS)}.")
        immediate = params.get("apply_immediately")
        if type(immediate) is not bool:
            raise ValueError(
                "apply_immediately is required: a resize applied now is an "
                "observed change with a settle check, and a deferral to the "
                "maintenance window is a different decision with no default.")
        return {"size": size, "apply_immediately": immediate}
    return {}


def _capacity_wording(action, resource, call_params, envelope):
    """The server's own words and price for one change, where it has them."""
    if action in capacity_service.BATCH_ACTIONS:
        step = {"action": action, "resource": resource, "params": call_params}
        description, notes = capacity_service._describe_step(step, envelope)
        delta, cost_notes = capacity_service._step_cost(step, envelope)
        return description, list(notes) + list(cost_notes), delta
    # The stable-ips pair is fleet-wide and deliberately outside BATCH_ACTIONS,
    # so it is worded here from the egress facts the report already carries.
    egress = envelope.get("egress") or {}
    per_address = egress.get("monthly_usd_per_address")
    if action == capacity_service.ACTION_ENABLE_STABLE_IPS:
        to_allocate = int(egress.get("to_allocate") or 0)
        delta = (to_allocate * per_address) if per_address is not None else None
        return ("Give every fleet node a stable outbound address",
                [f"{to_allocate} address(es) to allocate",
                 "an attached address replaces the node's auto-assigned one, "
                 "so enabling is close to cost-neutral"], delta)
    attached = int(egress.get("attached_count") or len(
        egress.get("attached") or []))
    delta = (attached * per_address) if per_address is not None else None
    return ("Stop using stable outbound addresses on the fleet",
            ["a kept reservation bills beside the node's new auto-assigned "
             "address"], delta)


def _preview_capacity_change(params, user):
    action = params["action"]
    resource = "" if action in FLEET_ACTIONS else str(
        params.get("resource") or "").strip()
    if action not in FLEET_ACTIONS and not resource:
        raise ValueError(f"resource is required for '{action}'.")
    call_params = _capacity_params(action, params)
    try:
        envelope = capacity_service.report()
    except capacity_service.CapacityError as error:
        raise ValueError(error.message)
    offer = (envelope.get("actions") or {}).get(action) or {}
    if not offer.get("offered"):
        raise ValueError(
            f"The server is not offering '{action}' right now "
            f"({offer.get('blocked_reason')}). Re-read get_fleet_capacity.")
    description, notes, delta = _capacity_wording(
        action, resource, call_params, envelope)
    summary = description + (
        f" · about ${delta:+.2f}/month" if isinstance(delta, (int, float))
        else " · cost not listed")
    return {
        "summary": summary,
        "details": {
            "action": action, "resource": resource or "fleet",
            "params": call_params, "notes": notes,
            "monthly_delta_usd": (round(delta, 2)
                                  if isinstance(delta, (int, float)) else None),
            "reconciliation": RECONCILE_CAPACITY,
        },
        # The fleet fingerprint, not a per-resource echo: an unrelated
        # concurrent change fails this closed, which is the point. Read from
        # the 120s report cache here; the handler re-reads it live.
        #
        # FINGERPRINT FIRST, and nothing may be appended after it. The registry
        # stores str(revision)[:128] into a max_length=128 column, so anything
        # composed AFTER the 64-char digest is what gets clipped when the
        # prefix is long (an RDS identifier alone can be 63 characters). A
        # clipped digest still round-trips through _require_bound_revision —
        # both sides truncate identically — so the row would be claimed and
        # then fail _fleet_moved forever with a false "the fleet changed".
        "revision": f"{capacity_service.fleet_revision()}:{action}:{resource}",
    }


def _summarize_capacity_change(params, user):
    return (f"Capacity change '{params.get('action')}' on "
            f"{params.get('resource') or 'the fleet'}.")


def _fingerprint_of(revision):
    """The fleet fingerprint half of a bound capacity revision.

    Read from the FRONT: the registry stores ``str(revision)[:128]`` into a
    128-character column, so only what is composed after the 64-character
    digest can ever be clipped. A bare fingerprint (what ``apply_capacity_plan``
    binds) has no separator and comes back whole.
    """
    return str(revision or "").split(":", 1)[0]


def _fleet_moved(approval):
    """Re-derive the fingerprint LIVE and compare it to the bound one.

    ``preview`` reads the 120-second report cache, which is right at proposal
    time — the operator is looking at that same picture. At execution the only
    honest answer comes from a fresh provider read, so the handler asks again.

    The fingerprint is the FIRST field of every revision this module builds
    (see ``_preview_capacity_change``), so it is read from the front and can
    never be the part that the 128-character column clips. A bare fingerprint —
    what ``apply_capacity_plan`` binds — has no separator and survives this
    unchanged.
    """
    if approval is None or not approval.revision:
        return None
    bound = _fingerprint_of(approval.revision)
    try:
        live = capacity_service.fleet_revision(refresh=True)
    except capacity_service.CapacityError as error:
        return refuse(error.message, error.error_code)
    if live != bound:
        return refuse(
            "The fleet changed since this was proposed, so it was NOT "
            "applied. Re-read get_fleet_capacity and ask again.",
            "fleet_changed")
    return None


@tool(
    name="apply_capacity_change",
    domain="cloud",
    permission="manage_aws",
    description=(
        "Request ONE capacity change the server is currently offering — add / "
        "drain / terminate an app node, add or remove a database reader, set "
        "cache replicas, resize the cache or a database, or turn stable "
        "outbound addresses on or off. Only ever propose an action whose "
        "'offered' flag is true in get_fleet_capacity. " + APPROVAL_NOTE),
    input_schema={
        "type": "object",
        "properties": {
            "action": {"type": "string",
                       "enum": list(capacity_service.ACTIONS),
                       "description": "The capacity action to request."},
            "resource": {
                "type": "string",
                "description": (
                    "The resource identifier. Omit for add_node, "
                    "enable_stable_ips and disable_stable_ips — those are "
                    "fleet-wide and the server derives the targets."),
            },
            "count": {"type": "integer",
                      "description": "set_cache_replicas: the replica count "
                                     "the group should have when this finishes."},
            "size": {"type": "string", "enum": list(SIZE_KEYS),
                     "description": "resize_cache / resize_database: the "
                                    "curated size to move to."},
            "apply_immediately": {
                "type": "boolean",
                "description": (
                    "Required for set_cache_replicas and the two resizes. No "
                    "default — ask the operator."),
            },
        },
        "required": ["action"],
    },
    mutates=True,
    # Admin twin: POST /api/aws/capacity/apply — denies_key_backed_session +
    # requires_fresh_auth(600), manage_aws AND a literal superuser
    # (_require_superuser), and infrastructure.refuse() first.
    fresh_auth_seconds=FRESH_AUTH,
    requires_superuser=True,
    requires_managed_infrastructure=True,
    authorize=can_system_admin,
    summarize=_summarize_capacity_change,
    preview=_preview_capacity_change,
)
def _tool_apply_capacity_change(params, user, *, approval=None,
                                conversation=None):
    from mojo.apps.account.services import admin_platform
    from mojo.apps.aws.rest import capacity as capacity_rest

    action = params["action"]
    resource = "" if action in FLEET_ACTIONS else str(
        params.get("resource") or "").strip()
    moved = _fleet_moved(approval)
    if moved is not None:
        return moved
    try:
        call_params = _capacity_params(action, params)
    except ValueError as exc:
        return refuse(str(exc)[:280], "invalid_request")
    try:
        result = capacity_service.apply(user, action, resource, **call_params)
    except capacity_service.CapacityError as error:
        return refuse(error.message, error.error_code)
    target = f"{resource or 'fleet'}:{result.get('id')}"
    admin_platform.audit_after_commit(
        user, capacity_rest.AUDIT_ACTIONS.get(action, "aws_capacity"), target)
    audit(user, capacity_rest.AUDIT_ACTIONS.get(action, "aws_capacity"),
          target, conversation=conversation, model_name="aws.CapacityOperation")
    return {
        "operation": result.get("id"),
        "phase": result.get("phase"),
        "state": result.get("state"),
        "message": result.get("message"),
        "reconciliation": RECONCILE_CAPACITY,
    }


def _preview_capacity_plan(params, user):
    steps = params.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError("steps must be a non-empty list of capacity steps.")
    if len(steps) > capacity_service.MAX_BATCH_STEPS:
        raise ValueError(
            f"A batch holds at most {capacity_service.MAX_BATCH_STEPS} steps; "
            f"this one has {len(steps)}.")
    try:
        # This preview WRITES: plan_batch stores the plan under PLAN_TTL. It is
        # the identical bounded cache write the Admin performs on every
        # debounced stepper tweak, it touches no provider and no durable state,
        # and rendering the SERVER's own worded, priced plan is the whole point
        # of the card.
        plan = capacity_service.plan_batch(user, steps)
    except capacity_service.CapacityError as error:
        raise ValueError(error.message)
    rendered = [{
        "index": step.get("index"), "action": step.get("action"),
        "resource": step.get("resource") or "fleet",
        "description": step.get("description"),
        "warnings": list(step.get("warnings") or [])[:8],
        "monthly_delta_usd": step.get("monthly_delta_usd"),
    } for step in (plan.get("steps") or [])]
    total = plan.get("total_monthly_delta_usd")
    summary = (
        f"Apply {len(rendered)} capacity step(s) in the server's order"
        + (f" · about ${total:+.2f}/month" if isinstance(total, (int, float))
           else " · cost not fully listed") + ".")
    return {
        "summary": summary,
        "details": {
            "steps": rendered,
            "total_monthly_delta_usd": total,
            "estimate_complete": plan.get("estimate_complete"),
            "order_note": plan.get("order_note"),
            "reconciliation": RECONCILE_CAPACITY,
        },
        # The fleet fingerprint, NOT the plan id: PLAN_TTL is 300s and the
        # approval window is 600s, so a bound plan id would routinely expire
        # before the operator answers. The fingerprint is what the plan's
        # safety actually depends on.
        "revision": capacity_service.fleet_revision(),
    }


def _summarize_capacity_plan(params, user):
    steps = params.get("steps") or []
    return f"Apply a {len(steps)}-step capacity plan to the fleet."


@tool(
    name="apply_capacity_plan",
    domain="cloud",
    permission="manage_aws",
    description=(
        "Plan and apply an ORDERED batch of capacity steps. The server orders, "
        "words and prices the plan; the approval card shows exactly that plan, "
        "and execution re-plans from the approved steps — you cannot alter it "
        "at apply time. " + APPROVAL_NOTE),
    input_schema={
        "type": "object",
        "properties": {
            "steps": {
                "type": "array",
                "description": (
                    "1..20 steps. Each is {action, resource, and the "
                    "conditional fields that action needs: count / size / "
                    "apply_immediately}."),
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string",
                                   "enum": list(capacity_service.BATCH_ACTIONS)},
                        "resource": {"type": "string"},
                        "count": {"type": "integer"},
                        "size": {"type": "string", "enum": list(SIZE_KEYS)},
                        "apply_immediately": {"type": "boolean"},
                    },
                    "required": ["action"],
                },
            },
        },
        "required": ["steps"],
    },
    mutates=True,
    # Admin twin: POST /api/aws/capacity/plan + /plan/apply — both
    # denies_key_backed_session + requires_fresh_auth(600), manage_aws AND a
    # literal superuser, and infrastructure.refuse() first.
    fresh_auth_seconds=FRESH_AUTH,
    requires_superuser=True,
    requires_managed_infrastructure=True,
    authorize=can_system_admin,
    summarize=_summarize_capacity_plan,
    preview=_preview_capacity_plan,
)
def _tool_apply_capacity_plan(params, user, *, approval=None,
                              conversation=None):
    from mojo.apps.account.services import admin_platform
    from mojo.apps.aws.rest import capacity as capacity_rest

    moved = _fleet_moved(approval)
    if moved is not None:
        return moved
    try:
        # Re-planned from the STORED steps, then applied back to back, so the
        # plan id being confirmed is seconds old. apply_batch re-reads the
        # fleet itself and refuses `plan_stale` on any mismatch.
        plan = capacity_service.plan_batch(user, params["steps"])
        result = capacity_service.apply_batch(user, plan["id"])
    except capacity_service.CapacityError as error:
        return refuse(error.message, error.error_code)
    target = f"{result.get('id')}:{len(result.get('steps') or [])} steps"
    admin_platform.audit_after_commit(
        user, capacity_rest.BATCH_AUDIT_ACTION, target)
    audit(user, capacity_rest.BATCH_AUDIT_ACTION, target,
          conversation=conversation, model_name="aws.CapacityBatch")
    return {
        "batch": result.get("id"),
        "state": result.get("state"),
        "message": result.get("message"),
        "steps": [{
            "index": step.get("index"), "action": step.get("action"),
            "resource": step.get("resource"), "state": step.get("state"),
            "description": step.get("description"),
        } for step in (result.get("steps") or [])[:20]],
        "reconciliation": RECONCILE_CAPACITY,
    }
