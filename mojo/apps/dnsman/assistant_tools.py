"""Shared validation setup through the Assistant's ordinary approval gate.

The registry admits DNS operators; exact tenant and object authority is checked
by the shared service in both preview and handler. No tool grants hub trust,
accepts credentials, changes traffic DNS or releases certificate key material.
"""

import hashlib
import json

from mojo import errors as me
from mojo.apps.assistant import tool


DOMAIN = "dns"
PERMISSION = "view_admin"
REFUSAL = "No eligible DNS setup target is available to you."


def _service():
    from mojo.apps.dnsman.services import delegation_setup

    return delegation_setup


def _authorized(user):
    """Fail closed at listing as well as dispatch, proposal and execution."""
    try:
        if not getattr(user, "is_active", False):
            return False
        permissions = ["view_dns", "manage_dns", "security"]
        if user.has_permission(permissions):
            return True
        return user.get_groups_with_permission(permissions).exists()
    except Exception:
        return False


def _safe_error(error):
    service = _service()
    if isinstance(error, service.SetupError):
        output = {"error": str(error), "error_code": error.error_code}
        if error.data is not None:
            output["data"] = error.data
        return output
    if isinstance(error, me.PermissionDeniedException):
        return {"error": REFUSAL, "error_code": "permission_denied"}
    return {"error": "DNS validation setup could not complete safely.",
            "error_code": "setup_unavailable"}


def _target(params, user, write=False):
    return _service().resolve_target(
        domain=params.get("domain"), group=params.get("group"),
        name=params.get("name"), actor=user, write=write)


def _session_refusal(request_meta):
    # The service needs the underlying confined identity to enforce API-key
    # scope. Assistant metadata intentionally carries no such credential, so
    # a key-backed socket must use the tenant-gated REST setup endpoints.
    if request_meta is not None and request_meta.get("key_backed", True):
        return {"error": "DNS assistant tools require a signed-in user session; provisioned keys use the tenant-gated DNS setup API.",
                "error_code": "permission_denied"}
    return None


def _row_target(params, user):
    service = _service()
    row = service.resolve_connection(params.get("delegation"), actor=user, write=True)
    if row.domain_id is not None:
        target = service.resolve_target(domain=row.domain_id, actor=user, write=True)
    else:
        target = service.resolve_target(group=row.group_id, name=row.domain_name,
                                        actor=user, write=True)
    return row, target


def _preview(action, params, user, request_meta=None):
    """Hash every authority/binding fact; omit observation timestamps."""
    refusal = _session_refusal(request_meta)
    if refusal:
        raise PermissionError(refusal["error"])
    service = _service()
    try:
        if action == "connect":
            target = _target(params, user, write=True)
            row = None
        else:
            row, target = _row_target(params, user)
        readiness = service.plan(target, allocation_ref=params.get("allocation_ref"),
                                 actor=user)
        connection = readiness.get("connection") or {}
        if row is not None and connection.get("id") != row.pk:
            raise ValueError("This connection is stale; inspect the current plan.")
        capability = "can_restart" if params.get("restart", False) else f"can_{action}"
        if not (readiness.get("capabilities") or {}).get(capability):
            raise ValueError("The DNS setup prerequisites changed; inspect the current plan.")
        binding = {
            "action": action,
            "domain": readiness.get("domain"),
            "consumer_project_uuid": (readiness.get("hub") or {}).get("consumer_project_uuid"),
            "connection": connection or None,
            "allocation_ref": params.get("allocation_ref") or connection.get("allocation_ref"),
            "restart": params.get("restart", False),
            "profile": readiness.get("profile"),
            "trust": readiness.get("trust"),
            "alias": {key: (readiness.get("alias") or {}).get(key)
                      for key in ("state", "source", "target", "observed_target", "one_hop")},
        }
        revision = hashlib.sha256(json.dumps(
            binding, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
        return {
            "summary": _summary(action, params, user),
            "details": {"domain": readiness.get("domain"),
                        "consumer_project_uuid": binding["consumer_project_uuid"],
                        "allocation_ref": binding["allocation_ref"],
                        "client_ref": connection.get("client_ref"),
                        "restart": binding["restart"],
                        "profile": binding["profile"],
                        "next_action": readiness.get("next_action")},
            "revision": revision,
        }
    except (ValueError, PermissionError):
        raise
    except Exception as error:
        raise ValueError(_safe_error(error)["error"]) from None


def _summary(action, params, user):
    descriptions = {
        "connect": "Prepare or connect this project's shared wildcard validation request",
        "check": "Verify this project's shared wildcard validation connection",
        "request": "Request this project's apex-plus-wildcard certificate using its own local key",
    }
    return descriptions[action] + "."


def _execute(action, params, user, request_meta=None):
    refusal = _session_refusal(request_meta)
    if refusal:
        return refusal
    service = _service()
    try:
        if action == "connect":
            target = _target(params, user, write=True)
            return service.connect(target, params.get("allocation_ref"), actor=user,
                                   restart=params.get("restart", False))
        row, _target_value = _row_target(params, user)
        return getattr(service, action)(row, actor=user)
    except Exception as error:
        return _safe_error(error)


TARGET_PROPERTIES = {
    "domain": {"type": "integer", "description": "Existing local Domain id; use alone"},
    "group": {"type": "integer", "description": "Eligible local group id; use with name"},
    "name": {"type": "string", "description": "Apex domain name; use with group"},
}


@tool(
    name="list_dns_setup_groups", domain=DOMAIN, permission=PERMISSION,
    authorize=_authorized,
    description="List active local groups where you have DNS setup authority. Returns minimal group choices and never reads hub credentials.",
    input_schema={"type": "object", "properties": {
        "id": {"type": "integer"}, "search": {"type": "string"},
        "start": {"type": "integer"}, "size": {"type": "integer"}}},
)
def list_dns_setup_groups(params, user, *, request_meta=None):
    refusal = _session_refusal(request_meta)
    if refusal:
        return refusal
    try:
        return _service().group_choices(user, **{
            key: params[key] for key in ("id", "search", "start", "size") if key in params})
    except Exception as error:
        return _safe_error(error)


@tool(
    name="get_dns_validation_plan", domain=DOMAIN, permission=PERMISSION,
    authorize=_authorized,
    description="Read shared wildcard validation and renewal readiness. Use an existing domain id OR group and apex name. This creates no connection, certificate, account or job; it cannot grant trust.",
    input_schema={"type": "object", "properties": dict(
        TARGET_PROPERTIES, allocation_ref={"type": "string", "description": "Optional existing immutable allocation target FQDN"})},
)
def get_dns_validation_plan(params, user, *, request_meta=None):
    refusal = _session_refusal(request_meta)
    if refusal:
        return refusal
    try:
        return _service().plan(_target(params, user),
                               allocation_ref=params.get("allocation_ref"), actor=user)
    except Exception as error:
        return _safe_error(error)


@tool(
    name="connect_dns_validation", domain=DOMAIN, permission=PERMISSION,
    authorize=_authorized, mutates=True,
    summarize=lambda params, user: _summary("connect", params, user),
    preview=lambda params, user, request_meta=None: _preview("connect", params, user, request_meta),
    description="Prepare a durable public connection request for an EXISTING allocation, or replay connect after the allocation controller grants that exact request. Requires operator approval. It never allocates a target or changes the CNAME. restart=true explicitly prepares a fresh ref only for an eligible revoked/unjoined connection.",
    input_schema={"type": "object", "properties": dict(
        TARGET_PROPERTIES, allocation_ref={"type": "string"}, restart={"type": "boolean"}),
        "required": ["allocation_ref"]},
)
def connect_dns_validation(params, user, approval=None, *, request_meta=None):
    return _execute("connect", params, user, request_meta)


@tool(
    name="check_dns_validation", domain=DOMAIN, permission=PERMISSION,
    authorize=_authorized, mutates=True,
    summarize=lambda params, user: _summary("check", params, user),
    preview=lambda params, user, request_meta=None: _preview("check", params, user, request_meta),
    description="Persist verification of an existing shared connection after checking current ownership, grant and the exact one-hop CNAME. Requires operator approval; does not request a certificate.",
    input_schema={"type": "object", "properties": {"delegation": {"type": "integer"}},
                  "required": ["delegation"]},
)
def check_dns_validation(params, user, approval=None, *, request_meta=None):
    return _execute("check", params, user, request_meta)


@tool(
    name="request_dns_wildcard_certificate", domain=DOMAIN, permission=PERMISSION,
    authorize=_authorized, mutates=True,
    summarize=lambda params, user: _summary("request", params, user),
    preview=lambda params, user, request_meta=None: _preview("request", params, user, request_meta),
    description="Idempotently request the exact apex-plus-wildcard certificate for a verified shared connection. Reuses an existing current or due certificate lineage and keeps its key local. Requires operator approval; issuance and renewal run through the existing worker.",
    input_schema={"type": "object", "properties": {"delegation": {"type": "integer"}},
                  "required": ["delegation"]},
)
def request_dns_wildcard_certificate(params, user, approval=None, *, request_meta=None):
    return _execute("request", params, user, request_meta)
