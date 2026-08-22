"""The Assistant's MCP (Model Context Protocol) resource server.

Three modules, deliberately layered so the wire format, the door's acceptance
checks and the tool projection can each be read — and tested — on their own:

``protocol``
    JSON-RPC 2.0 framing. No Django imports, no registry, no models.
``server``
    The MCP methods (``initialize``, ``ping``, ``tools/list``, ``tools/call``),
    the projected tool registry and the per-grant conversation. Every registry
    tool call goes through ``agent._execute_tool``, so the approval gate, the
    permission gate and the ``assistant:*`` incident events are the chat path's,
    not a second implementation.
``auth``
    The checks the framework chokepoint cannot make: that a credential was
    presented at all, that it is an MCP grant rather than some other bearer, and
    that its scope and the operator's permissions allow this door.

The HTTP surface is ``mojo/apps/assistant/rest/mcp.py``. Token issuance,
audience confinement and the discovery documents belong to
``mojo/apps/account/services/oauth_server``.
"""
