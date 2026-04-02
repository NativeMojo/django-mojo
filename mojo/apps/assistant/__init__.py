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


def register_tool(name, description, input_schema, handler,
                  permission, mutates=False, domain="custom"):
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
    }


def register_tools(tools):
    """Register multiple tools at once from a list of dicts."""
    for tool in tools:
        register_tool(**tool)


def get_registry():
    """Return the full tool registry dict (read-only view)."""
    return _REGISTRY


def get_tools_for_user(user):
    """
    Return Claude-compatible tool definitions filtered to tools
    the *user* has permission to call.
    """
    tools = []
    for entry in _REGISTRY.values():
        if user.has_permission(entry["permission"]):
            tools.append(entry["definition"])
    return tools
