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
import re
import uuid

import ujson
from django.db import transaction
from objict import objict

import mojo
from mojo.helpers import infrastructure, logit
from mojo.helpers.settings import settings
from mojo.apps.assistant.services import agent, approvals

from . import protocol

logger = logit.get_logger(__name__, "assistant.log")


# A tool NAME is chosen by the client, and it reaches a log line, an incident
# title and an incident body. Bound and charset-restrict it at the door so a
# 200 KB name, a newline-forged log line or a control character can never get
# that far. Anything outside this is `-32602`, answered before dispatch, and
# files nothing.
TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,128}$")

# Incident volume one machine client may generate: at most one event per
# category per user per window. See `suppressed_reporter`.
SUPPRESSION_WINDOW = 3600
SUPPRESSION_BUDGET = 50


# --- what this transport does not offer ------------------------------------
#
# Every name here is dropped from `projected_registry`, so `tools/list` never
# shows it and `tools/call` answers "Unknown tool" — indistinguishable from a
# name that was never registered. Each entry has a reason, and the reason is
# the contract: a tool is hidden because of what it IS, not because of who is
# calling.

# 1. Conversation meta-tools. They steer an agent loop that does not exist on
#    this transport (the client runs its own model). Derived from
#    `agent.META_TOOLS` so a new meta-tool is hidden the day it is added.
#    `list_tools` joins them: MCP has `tools/list`, and a discovery tool that
#    reads the RAW registry would hand back exactly the names the projection
#    exists to withhold. `add_context` joins them too — it is a chat-context
#    builder whose per-pk validation makes it a model-row existence oracle, and
#    the context it builds has nowhere to go here.
_META_TOOLS = frozenset(agent.META_TOOLS) | frozenset({
    "list_tools", "add_context",
})

# 2. Writers of the CHAT assistant's own state. Exposing them would put
#    approval cards for the operator's own assistant memory and skills in front
#    of an external client for no benefit. The READERS — `read_memory`,
#    `find_skill`, `list_skills` — stay.
_CHAT_STATE_WRITERS = frozenset({
    "write_memory", "delete_memory",
    "save_skill", "update_skill", "delete_skill",
})

# 3. Reads that SPEND the platform LLM credential. `analyze_image` sends an
#    image plus a client-chosen prompt to `helpers.llm` on
#    `LLM_HANDLER_API_KEY`, and read-only tools carry no approval gate — so
#    over MCP it is an unmetered spend of the installation's own credential,
#    driven by a remote client, with a free-text prompt. The rule is general:
#    a read tool whose handler calls the platform LLM is not exposed over MCP.
_LLM_SPENDING_READS = frozenset({
    "analyze_image",
})

HIDDEN_TOOLS = _META_TOOLS | _CHAT_STATE_WRITERS | _LLM_SPENDING_READS

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


def _find_conversation(user, grant):
    """The read path: the lowest-pk conversation flagged for this grant."""
    from mojo.apps.assistant.models import Conversation

    return Conversation.objects.filter(
        user=user, metadata__transport="mcp", metadata__grant=grant.pk,
    ).order_by("pk").first()


def conversation_for_grant(user, grant, create=True):
    """This grant's own conversation — the scope of everything it can see.

    CREATION IS SERIALIZED on the grant row. Two concurrent first calls used to
    be able to create two rows: harmless for reads (the lower pk wins every
    later lookup) but NOT harmless for approvals, because the loser's card is
    proposed into a conversation nothing ever looks at again — the operator sees
    it in the Admin, the client can never poll it, and it silently expires. One
    `select_for_update` on the grant the caller already authenticated with costs
    a single locked read on the first call of a connection and nothing
    afterwards, since the fast path returns before the transaction opens.
    """
    from mojo.apps.account.models.oauth_grant import OAuthGrant
    from mojo.apps.assistant.models import Conversation

    conversation = _find_conversation(user, grant)
    if conversation is not None or not create:
        return conversation

    client = grant.client
    label = client.client_name or client.client_id
    with transaction.atomic():
        # Lock the grant, then look again: whoever held the lock first has
        # already committed its row by the time we read.
        OAuthGrant.objects.select_for_update().filter(pk=grant.pk).first()
        conversation = _find_conversation(user, grant)
        if conversation is not None:
            return conversation
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


def suppression_key(user):
    """The unit an MCP client's incidents are suppressed on: the operator."""
    return f"mcp:{getattr(user, 'pk', None) or 0}"


def suppressed_reporter(user, _reporter=None):
    """The incident reporter every MCP dispatch files through.

    The chat path reports unsuppressed because a PERSON chose the tool name. On
    this transport the CLIENT chooses it, and `_execute_tool` files a level-6
    ``assistant:permission_denied`` for every unregistered name — so an
    unsuppressed reporter here is one incident row per request, for free, from a
    machine, forever. Everything an MCP dispatch reports is therefore keyed on
    the calling operator and filed at most once per category per window, and
    fail-CLOSED: a Redis outage must drop these, not open the floodgate.

    ``_reporter`` replaces ``report_event_suppressed`` itself (same signature),
    which is the seam the tests inject; production passes ``None``.
    """
    key = suppression_key(user)

    def report(details, **kwargs):
        reporter = _reporter
        if reporter is None:
            from mojo.apps.incident.reporter import report_event_suppressed

            reporter = report_event_suppressed
        kwargs.setdefault("window", SUPPRESSION_WINDOW)
        kwargs.setdefault("budget", SUPPRESSION_BUDGET)
        kwargs.setdefault("fail_open", False)
        return reporter(details, key=key, **kwargs)

    return report


def call_tool(name, arguments, user, grant, request_meta,
              hide_infrastructure=None, _reporter=None):
    """Run one ``tools/call`` and return its ``CallToolResult``.

    ``name`` is assumed VALIDATED (``TOOL_NAME_RE``) — ``_dispatch`` refuses
    anything else with ``-32602`` before reaching here, so nothing unbounded or
    newline-bearing ever reaches a log line or an incident title.

    Never the arguments and never the result in the log line: this endpoint is
    labelled ``assistant_mcp`` precisely so neither reaches the request log.
    """
    logger.info("MCP tools/call tool=%s user=%s grant=%s",
                name, getattr(user, "pk", None), getattr(grant, "pk", None))

    reporter = suppressed_reporter(user, _reporter)

    if name in MCP_ONLY_NAMES:
        return _call_result(
            _pending_action_result(name, arguments, user, grant, _reporter=reporter))

    block = {
        "type": "tool_use",
        "id": "mcp-" + uuid.uuid4().hex,
        "name": name,
        "input": arguments,
    }
    outcome = agent._execute_tool(
        block, projected_registry(hide_infrastructure), user,
        conversation_for_grant(user, grant), [], None, [],
        request_meta=request_meta, _reporter=reporter, pending_actions=[])
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
        # Refused BEFORE dispatch, so an unbounded or newline-bearing name never
        # reaches a log line, an incident title or an incident body. A real tool
        # name always matches; nothing legitimate is lost.
        if not isinstance(name, str) or not TOOL_NAME_RE.match(name):
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
