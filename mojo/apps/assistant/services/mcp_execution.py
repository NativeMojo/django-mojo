"""Narrow MCP execution consent, independent of per-action human approvals.

The HTTP boundary authenticates the token. This service rechecks its live grant
before an ordinary mutation, and never manufactures a PendingAction or an
interactive request. Protected tool contracts stay on the approval path.
"""
import inspect
import uuid
from urllib.parse import urlsplit

from objict import objict

from mojo.helpers import dates, infrastructure
from mojo.apps.account.services.oauth_server.resources import MCP_EXECUTE_SCOPE


UNAVAILABLE = "Delegated execution authorization is unavailable; reconnect the agent."


def has_execution_scope(grant):
    scopes = getattr(grant, "scopes", None)
    return isinstance(scopes, list) and "mcp" in scopes and MCP_EXECUTE_SCOPE in scopes


def requires_approval(entry):
    """Conservative compatibility rule for existing and extension handlers."""
    if (entry.get("requires_operator_approval") or
            entry.get("owner_state") is not None or
            entry.get("fresh_auth_seconds") or entry.get("preview")):
        return True
    try:
        parameters = inspect.signature(entry["handler"]).parameters
        # **kwargs can hide an approval dependency; keep it protected too.
        return ("approval" in parameters or any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()))
    except (TypeError, ValueError):
        return True


def decision(entry, raw_args, user, grant, conversation, request_meta, _external=None):
    """Return a normalized, live authorized call, or a refusal/proposal decision.

    `grant` comes only from the authenticated MCP server, never tool arguments.
    Matching its current JTI also closes a refresh/revocation race after HTTP
    authentication. No credential value is returned to a handler or audit log.
    """
    from mojo.apps.account.models.oauth_grant import OAuthGrant
    from mojo.apps.assistant import user_can_use_tool
    from mojo.apps.assistant.mcp.auth import resource_path
    from mojo.apps.assistant.services import approvals

    result = objict(exempt=False, error=None, args=raw_args, user=user, audit="")
    if not has_execution_scope(grant):
        result.error = {"error": UNAVAILABLE}
        return result
    now = dates.utcnow()
    live = OAuthGrant.objects.select_related("user", "client").filter(
        pk=grant.pk, user_id=user.pk, is_active=True,
        client__is_active=True, user__is_active=True,
        access_jti=grant.access_jti, resource=grant.resource,
        access_expires__gt=now, refresh_expires__gt=now).first()
    metadata = getattr(conversation, "metadata", None) or {}
    if (live is None or not has_execution_scope(live) or
            live.scopes != grant.scopes or
            urlsplit(live.resource).path != resource_path() or
            getattr(request_meta, "path", None) != resource_path() or
            getattr(request_meta, "bearer", None) != "mcp" or
            getattr(request_meta, "key_backed", True) is not False or
            getattr(conversation, "user_id", None) != user.pk or
            metadata.get("transport") != "mcp" or metadata.get("grant") != live.pk):
        result.error = {"error": UNAVAILABLE}
        return result
    result.user = live.user
    if not user_can_use_tool(live.user, entry):
        result.error = {"error": "Permission denied for delegated execution."}
        return result
    external = infrastructure.is_external() if _external is None else _external
    if entry.get("requires_managed_infrastructure") and external:
        result.error = {"error": infrastructure.refusal_message("this tool")}
        return result
    if requires_approval(entry):
        return result
    try:
        result.args = approvals.normalize_args(
            entry["definition"].get("input_schema"), raw_args)
    except approvals.ArgumentError as error:
        result.error = {"error": str(error)}
        return result
    result.exempt = True
    result.audit = f"delegated MCP authority grant={live.pk} client={live.client.client_id}"
    return result


def record_attempt(tool_name, args, user, conversation, audit, reporter=None):
    """Record before dispatch; a failed audit write prevents the mutation.

    Reuse the approvals fingerprint's redaction so audit evidence cannot reveal
    low-entropy secrets by offline guessing. The invocation ID is server-owned.
    """
    from mojo.apps.assistant.services import approvals
    if reporter is None:
        from mojo.apps.incident import report_event
        reporter = report_event
    correlation = (f"{audit} invocation={uuid.uuid4().hex} "
                   f"fingerprint={approvals.fingerprint(tool_name, args)}")
    reporter(
        f"User {user.email} (id={user.pk}) attempting '{tool_name}' under "
        f"{correlation}. conv={conversation.pk}. Outcome not yet known.",
        title=f"Delegated MCP attempt: {tool_name}",
        category=f"assistant:execution:{tool_name}", level=5,
        uid=user.pk, model_name="account.User", model_id=user.pk)
    return correlation
