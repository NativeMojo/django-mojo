"""The MCP methods, the projected tool registry, and tool dispatch.

Everything an external AI client can reach over MCP passes through here, and
one rule shapes the whole module: **there is no second dispatch path**. A
registry tool called over MCP is executed by ``agent._execute_tool`` — the same
function both agent loops use — so the approval gate, the permission gate, the
``assistant:*`` incident events and the tool-result serialization are the chat
path's behaviour rather than a re-implementation that could drift from it.

What IS specific to this transport:

* a PROJECTION of the registry. ``_execute_tool`` takes the registry as a
  parameter, so handing it a filtered dict makes a hidden or
  infrastructure-hidden name fail as "Unknown tool" with the chat path's own
  string and event, exactly like a name that was never registered. The
  projection is load-bearing rather than cosmetic: a READ-ONLY
  ``requires_managed_infrastructure`` tool is not refused by ``_execute_tool``
  at all (only ``approvals.propose`` checks that gate), so this filter is the
  only thing keeping such a tool off an external installation.
* a per-grant ``Conversation``, created lazily, which gives ``propose`` its
  dedupe/supersede scope, gives the Admin somewhere to see the client's cards,
  and BOUNDS what the two MCP-only read tools may hand back.
* two MCP-only read tools, scoped to that conversation, so a client can poll
  the approvals it proposed — and only those.

Nothing here is stateful across requests: no session id, no cache, no
process-local registry. Every node behind a load balancer answers identically.
"""
import uuid

import ujson
from objict import objict

import mojo
from mojo.helpers import infrastructure, logit
from mojo.helpers.settings import settings
from mojo.apps.assistant.services import agent, approvals

from . import protocol

logger = logit.get_logger(__name__, "assistant.log")


# Conversation meta-tools plus the writers that shape the CHAT assistant's own
# state. `load_tools`, `create_plan` and `update_plan` steer an agent loop that
# does not exist here; the memory and skill writers would put approval cards for
# the operator's own assistant memory in front of an external client for no
# benefit. The readers (`read_memory`, `find_skill`, `list_skills`,
# `list_tools`, `add_context`) stay. Derived from `agent.META_TOOLS` so a new
# meta-tool is hidden the day it is added.
HIDDEN_TOOLS = frozenset(agent.META_TOOLS) | frozenset({
    "write_memory", "delete_memory",
    "save_skill", "update_skill", "delete_skill",
})

INSTRUCTIONS = (
    "Tools run with the connected operator's own permissions. A tool that "
    "changes data never executes here: it returns an approval card (status "
    "approval_required) that the operator resolves in the Admin. Poll "
    "get_pending_action with the action_id to learn the outcome."
)

READ_ONLY_ANNOTATIONS = {"readOnlyHint": True, "destructiveHint": False}

# The two tools that exist only on this transport. Both are reads, both are
# scoped to the calling grant's own conversation.
MCP_ONLY_TOOLS = (
    {
        "name": "list_pending_actions",
        "description": (
            "The approval cards this connection proposed, oldest first (50 "
            "max), with their current state. Cards are resolved by the "
            "operator in the Admin, never here."
        ),
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": dict(READ_ONLY_ANNOTATIONS),
    },
    {
        "name": "get_pending_action",
        "description": (
            "One approval card this connection proposed, by id, with its "
            "current state and result."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action_id": {
                    "type": "string",
                    "description": (
                        "The action_id returned when a tool required approval"
                    ),
                },
            },
            "required": ["action_id"],
        },
        "annotations": dict(READ_ONLY_ANNOTATIONS),
    },
)

MCP_ONLY_NAMES = frozenset(tool["name"] for tool in MCP_ONLY_TOOLS)


def server_name():
    """What this installation calls itself to a connecting client.

    The System Setup installation slug is the only platform-name fact the
    framework holds; an unconfigured installation is simply "mojo".
    """
    slug = settings.get("MOJO_INSTALLATION_SLUG", "") or "mojo"
    return f"{slug}-assistant"


def _hide_infrastructure(hide_infrastructure=None):
    if hide_infrastructure is None:
        return infrastructure.is_external()
    return bool(hide_infrastructure)


def projected_registry(hide_infrastructure=None):
    """The registry this transport dispatches from.

    NOT permission-filtered: ``_execute_tool`` keeps its own permission gate and
    its own denial event, and duplicating the check here would only make the two
    able to disagree.
    """
    from mojo.apps.assistant import get_registry

    hide = _hide_infrastructure(hide_infrastructure)
    projected = {}
    for name, entry in get_registry().items():
        if name in HIDDEN_TOOLS:
            continue
        if hide and entry.get("requires_managed_infrastructure"):
            continue
        projected[name] = entry
    return projected


def _mcp_tool(entry):
    """One registry entry in MCP `Tool` shape.

    ``annotations`` are hints, not a security boundary — the boundary is the
    approval gate — but a client that renders "this will change data" from
    ``destructiveHint`` should be told the truth.
    """
    definition = entry["definition"]
    mutates = bool(entry.get("mutates"))
    return {
        "name": definition["name"],
        "description": definition.get("description", ""),
        "inputSchema": definition.get("input_schema"),
        "annotations": {
            "readOnlyHint": not mutates,
            "destructiveHint": mutates,
        },
    }


def list_tools(user, hide_infrastructure=None):
    """Everything this user may call over MCP, in registry order."""
    from mojo.apps.assistant import user_can_use_tool

    tools = []
    for entry in projected_registry(hide_infrastructure).values():
        if user_can_use_tool(user, entry):
            tools.append(_mcp_tool(entry))
    tools.extend(dict(tool) for tool in MCP_ONLY_TOOLS)
    return tools


def conversation_for_grant(user, grant, create=True):
    """This grant's own conversation — the scope of everything it can see.

    Two concurrent first calls can each create a row. Both are flagged with the
    same metadata and the lower pk wins every later lookup, so the loser is an
    orphan with no messages rather than a correctness problem — not worth a lock
    on the hot path.
    """
    from mojo.apps.assistant.models import Conversation

    conversation = Conversation.objects.filter(
        user=user, metadata__transport="mcp", metadata__grant=grant.pk,
    ).order_by("pk").first()
    if conversation is not None or not create:
        return conversation
    client = grant.client
    label = client.client_name or client.client_id
    return Conversation.objects.create(
        user=user,
        title=f"MCP: {label}"[:255],
        metadata={"transport": "mcp", "grant": grant.pk})


def _call_result(text):
    """The MCP ``CallToolResult`` for one serialized tool result.

    ``isError`` mirrors the chat path's own convention: every refusal a tool
    handler or the dispatch gate produces is a JSON object carrying ``error``.
    ``structuredContent`` is sent alongside the text because the result already
    IS an object; no ``outputSchema`` is declared, so nothing is promised.
    """
    try:
        parsed = ujson.loads(text)
    except Exception:
        parsed = None
    result = {
        "content": [{"type": "text", "text": text}],
        "isError": isinstance(parsed, dict) and "error" in parsed,
    }
    if isinstance(parsed, dict):
        result["structuredContent"] = parsed
    return result


def _pending_action_result(name, arguments, user, grant, _reporter=None):
    """The serialized result of one MCP-only read tool.

    Both are scoped to the grant's own conversation. A card the operator has
    pending in the Admin chat panel is invisible here even though it belongs to
    the same user: ``render_block`` ships redacted ``args`` and up to 8 KB of
    ``preview``, which an external client has no business reading for work it
    did not propose. With no conversation yet there is nothing to read and every
    id is a guess, so the generic refusal is the whole answer.
    """
    conversation = conversation_for_grant(user, grant, create=False)
    if name == "list_pending_actions":
        result = {"actions": approvals.states_for_conversation(conversation)}
    elif conversation is None:
        result = {"error": approvals.GENERIC_UNAVAILABLE}
    else:
        try:
            result = approvals.state_for_action(
                user, arguments.get("action_id"),
                conversation_id=conversation.pk, _reporter=_reporter)
        except approvals.ApprovalRefused:
            result = {"error": approvals.GENERIC_UNAVAILABLE}
    return agent._dumps_tool_result(
        result, user=user, conversation=conversation, tool_name=name,
        _reporter=_reporter)


def call_tool(name, arguments, user, grant, request_meta,
              hide_infrastructure=None, _reporter=None):
    """Run one ``tools/call`` and return its ``CallToolResult``.

    Never the arguments and never the result in the log line: this endpoint is
    labelled ``assistant_mcp`` precisely so neither reaches the request log.
    """
    logger.info("MCP tools/call tool=%s user=%s grant=%s",
                name, getattr(user, "pk", None), getattr(grant, "pk", None))

    if name in MCP_ONLY_NAMES:
        return _call_result(
            _pending_action_result(name, arguments, user, grant, _reporter=_reporter))

    block = {
        "type": "tool_use",
        "id": "mcp-" + uuid.uuid4().hex,
        "name": name,
        "input": arguments,
    }
    outcome = agent._execute_tool(
        block, projected_registry(hide_infrastructure), user,
        conversation_for_grant(user, grant), [], None, [],
        request_meta=request_meta, _reporter=_reporter, pending_actions=[])
    return _call_result(outcome["content"])


def _dispatch(msg, ctx):
    """One JSON-RPC request -> its ``result``, or raise ``JsonRpcError``."""
    method = msg.get("method")
    params = msg.get("params") or {}

    if method == "initialize":
        requested = params.get("protocolVersion")
        version = (requested if requested in protocol.SUPPORTED_PROTOCOL_VERSIONS
                   else protocol.PROTOCOL_VERSION)
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": ctx.server_name, "version": mojo.__version__},
            "instructions": INSTRUCTIONS,
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": list_tools(ctx.user, ctx.hide_infrastructure)}
    if method == "tools/call":
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise protocol.JsonRpcError(
                protocol.INVALID_PARAMS, protocol.INVALID_PARAMS_MESSAGE)
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise protocol.JsonRpcError(
                protocol.INVALID_PARAMS, protocol.INVALID_PARAMS_MESSAGE)
        return call_tool(
            name, arguments, ctx.user, ctx.grant, ctx.request_meta,
            hide_infrastructure=ctx.hide_infrastructure, _reporter=ctx.reporter)

    raise protocol.JsonRpcError(
        protocol.METHOD_NOT_FOUND, f"Method not found: {method}")


def _answer(msg, ctx):
    """``(message_or_None, is_invalid_request)`` for one incoming message."""
    try:
        kind = protocol.classify(msg)
    except protocol.JsonRpcError as err:
        if err.code == protocol.INVALID_PARAMS:
            return protocol.error_message(
                protocol.message_id(msg), err.code, err.message), False
        # JSON-RPC 2.0: when the id could not be determined reliably the error
        # response carries a null id. An element this malformed has not proven
        # its id is one this server would echo.
        return protocol.error_message(None, err.code, err.message), True

    if kind != "request":
        return None, False

    msg_id = msg.get("id")
    try:
        return protocol.result_message(msg_id, _dispatch(msg, ctx)), False
    except protocol.JsonRpcError as err:
        return protocol.error_message(msg_id, err.code, err.message), False
    except Exception:
        logger.exception("MCP dispatch failed for method %r", msg.get("method"))
        return protocol.error_message(
            msg_id, protocol.INTERNAL_ERROR, protocol.INTERNAL_ERROR_MESSAGE), False


def handle(raw, user, grant, request_meta, server_name,
           hide_infrastructure=None, _reporter=None):
    """``(http_status, payload)`` for one raw request body.

    ``202`` with a ``None`` payload means "accepted, nothing to answer" — the
    body held only notifications and/or client responses.
    """
    try:
        messages, is_batch = protocol.parse_body(raw)
    except protocol.JsonRpcError as err:
        return 400, protocol.error_message(None, err.code, err.message)

    ctx = objict(
        user=user, grant=grant, request_meta=request_meta,
        server_name=server_name, hide_infrastructure=hide_infrastructure,
        reporter=_reporter)

    answers = []
    invalid = False
    for msg in messages:
        answer, bad = _answer(msg, ctx)
        if bad:
            invalid = True
        if answer is not None:
            answers.append(answer)

    if not answers:
        return 202, None
    if is_batch:
        return 200, answers
    return (400 if invalid else 200), answers[0]
