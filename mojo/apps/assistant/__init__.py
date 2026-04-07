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
    "jobs": "Query, monitor, cancel, and retry background jobs",
    "users": "Query and manage users, permissions, rate limits, and activity",
    "groups": "Query and manage groups, members, and group activity",
    "metrics": "Fetch time-series metrics, system health, and incident trends",
    "discovery": "List all available tools across every domain",
    "memory": "Read, write, and delete persistent assistant memories",
    "models": "Query and describe any Django model in the system",
    "docs": "Read framework documentation",
    "web": "Fetch and read web pages",
    "logs": "Query the audit log trail",
    "files": "Query, view, and analyze uploaded files",
    "planning": "Create and track multi-step execution plans",
    "comms": "Send notifications via SMS, email, push, and in-app channels",
    "skills": "Save, find, and manage learned multi-step procedures",
}


def register_tool(name, description, input_schema, handler,
                  permission, mutates=False, domain="custom", core=False):
    """
    Register a tool that the LLM assistant can call.

    Args:
        name:         Unique tool name (e.g. ``query_orders``).
        description:  Human-readable description shown to the LLM.
        input_schema: JSON Schema ``dict`` describing the tool's parameters.
        handler:      Callable ``(params, user) -> dict/list``.
        permission:   Permission string required to execute this tool
                      (checked against ``user.has_permission``).
        mutates:      If True the tool changes data (LLM is told to confirm).
        domain:       Logical grouping (security, jobs, users, groups, metrics, custom).
        core:         If True, tool is always sent to the LLM. If False,
                      tool is only sent when its domain is loaded.
    """
    if name in _REGISTRY:
        raise ValueError(f"Assistant tool '{name}' is already registered")

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
    }


def register_tools(tools):
    """Register multiple tools at once from a list of dicts."""
    for t in tools:
        register_tool(**t)


def tool(name, domain, permission, input_schema, description,
         mutates=False, core=False):
    """
    Decorator that registers a function as an assistant tool.

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
        )
        return func
    return decorator


def get_registry():
    """Return the full tool registry dict (read-only view)."""
    return _REGISTRY


def get_tools_for_user(user):
    """
    Return Claude-compatible tool definitions for ALL tools
    the user has permission to call. Used for backward compat
    with old conversations.
    """
    tools = []
    for entry in _REGISTRY.values():
        if user.has_permission(entry["permission"]):
            tools.append(entry["definition"])
    return tools


def get_core_tools_for_user(user):
    """
    Return Claude-compatible tool definitions for core tools only.
    These are always sent to the LLM on every turn.
    """
    tools = []
    for entry in _REGISTRY.values():
        if entry["core"] and user.has_permission(entry["permission"]):
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
    tools = []
    for entry in _REGISTRY.values():
        if entry["domain"] in domain_set and user.has_permission(entry["permission"]):
            tools.append(entry["definition"])
    return tools


def get_available_domains(user):
    """
    Return a dict of domains the user has access to, with tool count,
    description, and example tool names.
    """
    domains = {}
    for entry in _REGISTRY.values():
        if not user.has_permission(entry["permission"]):
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
            "examples": info["tools"][:3],
        }
    return result
