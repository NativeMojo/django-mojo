"""
LLM Admin Assistant — extensible, permission-gated admin assistant.

External projects register tools by dropping an ``assistant_tools.py``
in any installed app and calling :func:`register_tool`.

    from mojo.apps.assistant import register_tool

    register_tool(
        name="query_orders",
        description="Query orders by status and date range",
        input_schema={...},
        handler=my_handler_func,
        permission="view_orders",
    )
"""

# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

_REGISTRY = {}

# Domain descriptions for load_tools listing
DOMAIN_DESCRIPTIONS = {
    "security": "Query and manage security incidents, events, tickets, rulesets, and IP blocking",
    "jobs": "Query, monitor, cancel, and retry background jobs; run jobs and scheduled tasks on demand",
    "users": "Query and manage users, permissions, rate limits, and activity",
    "groups": "Query and manage groups, members, and group activity",
    "metrics": "Discover, fetch, and explain time-series metrics and gauges across accounts, groups, and users. Covers API traffic, jobs, bouncer, logins, shortlinks, and any slug recorded via metrics.record(). Includes a single write tool for operational gauge toggles (maintenance_mode, feature flags).",
    "discovery": "List all available tools across every domain",
    "memory": "Read, write, and delete persistent assistant memories",
    "models": "Query and describe any Django model in the system",
    "docs": "Read framework documentation",
    "docit": "Search the documentation knowledge base (docit books and pages)",
    "web": "Fetch and read web pages",
    "logs": "Query the audit log trail",
    "files": "Query, view, and analyze uploaded files",
    "planning": "Create and track multi-step execution plans",
    "comms": "Send notifications via SMS, email, push, and in-app channels",
    "skills": "Save, find, and manage learned multi-step procedures",
}


def register_tool(name, description, input_schema, handler,
                  permission, mutates=False, domain="custom", core=False,
                  fresh_auth_seconds=None, requires_superuser=False,
                  requires_managed_infrastructure=False, summarize=None,
                  preview=None, authorize=None):
    """
    Register a tool that the LLM assistant can call.

    Args:
        name:         Unique tool name (e.g. ``query_orders``).
        description:  Human-readable description shown to the LLM.
        input_schema: JSON Schema ``dict`` describing the tool's parameters.
        handler:      Callable ``(params, user) -> dict/list``.
        permission:   Permission string required to execute this tool
                      (checked against ``user.has_permission``).
        mutates:      If True the tool changes data. A mutating tool NEVER runs
                      on the model's call — it produces a PendingAction the
                      operator must approve (see ``services/approvals.py``).
        domain:       Logical grouping (security, jobs, users, groups, metrics, custom).
        core:         If True, tool is always sent to the LLM. If False,
                      tool is only sent when its domain is loaded.

    Approval gates — mutating tools only. Passing any of these without
    ``mutates=True`` raises ``ValueError`` at import time, because a gate on a
    tool that never reaches the approval boundary is a silent no-op:

        fresh_auth_seconds: Recency window in seconds, mirroring
                      ``@md.requires_fresh_auth(seconds=N)`` on the matching
                      Admin endpoint. When set, the action can only be resolved
                      over REST (the WebSocket carries no per-message token).
        requires_superuser: AND-check for a live literal ``User.is_superuser``,
                      mirroring the hand-written superuser checks in the Admin
                      REST layer (``aws/rest/capacity.py``).
        requires_managed_infrastructure: Tool is hidden from the model and
                      refused at proposal and execution when
                      ``infrastructure.is_external()``.
        summarize:    ``(params, user) -> str``. One operator-facing sentence
                      for the approval card. Must contain no secret.
        preview:      ``(params, user) -> {"summary", "details", "revision"}``.
                      Read-only. ``revision`` is bound into the approval and
                      re-checked before execution. If it RAISES, the proposal is
                      refused as an ordinary tool error with no record created,
                      so a per-object authority check may live here and fail
                      closed.

    authorize:        ``(user) -> bool``, allowed on ANY tool (mutating or not).
                      Evaluated in ADDITION to ``user.has_permission(permission)``
                      everywhere that check runs — listing, dispatch, proposal and
                      execution. It exists because ``has_permission`` is global-only
                      and some tools need a compound or group-scoped rule. A False
                      result is indistinguishable from a missing permission; it is
                      never a substitute for ``permission``, which stays required.
    """
    if name in _REGISTRY:
        raise ValueError(f"Assistant tool '{name}' is already registered")

    gates = {
        "fresh_auth_seconds": fresh_auth_seconds,
        "requires_superuser": requires_superuser,
        "requires_managed_infrastructure": requires_managed_infrastructure,
        "summarize": summarize,
        "preview": preview,
    }
    if not mutates:
        declared = [key for key, value in gates.items() if value]
        if declared:
            raise ValueError(
                f"Assistant tool '{name}' declares approval gate(s) "
                f"{sorted(declared)} without mutates=True. Approval gates only "
                f"apply to mutating tools; a gate on a read-only tool never runs."
            )
    if fresh_auth_seconds is not None:
        if isinstance(fresh_auth_seconds, bool) or not isinstance(fresh_auth_seconds, int):
            raise ValueError(
                f"Assistant tool '{name}': fresh_auth_seconds must be a positive int or None")
        if fresh_auth_seconds <= 0:
            raise ValueError(
                f"Assistant tool '{name}': fresh_auth_seconds must be a positive int or None")
    for key in ("summarize", "preview"):
        if gates[key] is not None and not callable(gates[key]):
            raise ValueError(f"Assistant tool '{name}': {key} must be callable or None")
    if authorize is not None and not callable(authorize):
        raise ValueError(f"Assistant tool '{name}': authorize must be callable or None")

    _REGISTRY[name] = {
        "definition": {
            "name": name,
            "description": description,
            "input_schema": input_schema,
        },
        "handler": handler,
        "permission": permission,
        "mutates": mutates,
        "domain": domain,
        "core": core,
        "fresh_auth_seconds": fresh_auth_seconds,
        "requires_superuser": bool(requires_superuser),
        "requires_managed_infrastructure": bool(requires_managed_infrastructure),
        "summarize": summarize,
        "preview": preview,
        "authorize": authorize,
    }


def register_tools(tools):
    """Register multiple tools at once from a list of dicts."""
    for t in tools:
        register_tool(**t)


def tool(name, domain, permission, input_schema, description,
         mutates=False, core=False, fresh_auth_seconds=None,
         requires_superuser=False, requires_managed_infrastructure=False,
         summarize=None, preview=None, authorize=None):
    """
    Decorator that registers a function as an assistant tool.

    Takes the same approval-gate arguments as :func:`register_tool`.

    Usage::

        from mojo.apps.assistant import tool

        @tool(
            name="query_orders",
            domain="orders",
            permission="view_orders",
            description="Query orders by status and date range",
            input_schema={"type": "object", "properties": {...}},
        )
        def _tool_query_orders(params, user):
            ...

    The decorated function is registered immediately on import.
    External apps can use this in any module that gets imported at startup.
    """
    def decorator(func):
        register_tool(
            name=name,
            description=description,
            input_schema=input_schema,
            handler=func,
            permission=permission,
            mutates=mutates,
            domain=domain,
            core=core,
            fresh_auth_seconds=fresh_auth_seconds,
            requires_superuser=requires_superuser,
            requires_managed_infrastructure=requires_managed_infrastructure,
            summarize=summarize,
            preview=preview,
            authorize=authorize,
        )
        return func
    return decorator


def get_registry():
    """Return the full tool registry dict (read-only view)."""
    return _REGISTRY


def user_can_use_tool(user, entry):
    """The single authorization predicate for a registry entry.

    ``permission`` is always required; ``authorize`` is an ADDITIONAL gate for
    rules ``has_permission`` cannot express (compound grants, group-scoped
    authority). An ``authorize`` that raises is treated as a refusal — a broken
    predicate must not open a tool.
    """
    if not user.has_permission(entry["permission"]):
        return False
    authorize = entry.get("authorize")
    if authorize is None:
        return True
    try:
        return bool(authorize(user))
    except Exception:
        _logger().exception(
            "authorize() raised for tool '%s' — treating as denied",
            entry["definition"]["name"])
        return False


def _logger():
    from mojo.helpers import logit

    return logit.get_logger("assistant", "assistant.log")


def _infrastructure_hidden():
    """True when this installation may not offer managed-infrastructure tools.

    One settings read per list build — ``INFRASTRUCTURE_MODE`` is file-only, so
    it never costs a database round-trip.
    """
    from mojo.helpers import infrastructure

    return infrastructure.is_external()


def _entry_is_listable(entry, hide_infrastructure):
    return not (hide_infrastructure and entry["requires_managed_infrastructure"])


def get_tools_for_user(user):
    """
    Return Claude-compatible tool definitions for ALL tools
    the user has permission to call. Used for backward compat
    with old conversations.
    """
    hide = _infrastructure_hidden()
    tools = []
    for entry in _REGISTRY.values():
        if _entry_is_listable(entry, hide) and user_can_use_tool(user, entry):
            tools.append(entry["definition"])
    return tools


def get_core_tools_for_user(user):
    """
    Return Claude-compatible tool definitions for core tools only.
    These are always sent to the LLM on every turn.
    """
    hide = _infrastructure_hidden()
    tools = []
    for entry in _REGISTRY.values():
        if (entry["core"] and _entry_is_listable(entry, hide)
                and user_can_use_tool(user, entry)):
            tools.append(entry["definition"])
    return tools


def get_domain_tools_for_user(user, domains):
    """
    Return Claude-compatible tool definitions for tools in the
    specified domains, filtered by user permission.
    """
    if not domains:
        return []
    domain_set = set(domains) if isinstance(domains, list) else {domains}
    hide = _infrastructure_hidden()
    tools = []
    for entry in _REGISTRY.values():
        if (entry["domain"] in domain_set and _entry_is_listable(entry, hide)
                and user_can_use_tool(user, entry)):
            tools.append(entry["definition"])
    return tools


def get_available_domains(user):
    """
    Return a dict of domains the user has access to, with tool count,
    description, and example tool names.
    """
    hide = _infrastructure_hidden()
    domains = {}
    for entry in _REGISTRY.values():
        if not _entry_is_listable(entry, hide):
            continue
        if not user_can_use_tool(user, entry):
            continue
        domain = entry["domain"]
        if domain not in domains:
            domains[domain] = {"count": 0, "tools": []}
        domains[domain]["count"] += 1
        domains[domain]["tools"].append(entry["definition"]["name"])

    result = {}
    for domain, info in sorted(domains.items()):
        # Skip domains that only have core tools (they're already loaded)
        has_non_core = any(
            _REGISTRY[t]["domain"] == domain and not _REGISTRY[t]["core"]
            for t in info["tools"]
        )
        if not has_non_core:
            continue
        result[domain] = {
            "count": info["count"],
            "description": DOMAIN_DESCRIPTIONS.get(domain, ""),
            "tools": info["tools"],
        }
    return result
