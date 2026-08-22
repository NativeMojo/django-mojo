"""The MCP resource server's protocol, projection and dispatch — in process.

No wire, no settings, no patching: every seam this module needs is a parameter.
``hide_infrastructure=`` stands in for the installation mode and ``_reporter=``
for the incident reporter, so the default tier can prove the whole surface
without touching anything another parallel module can see.

The fixture tools live in a private ``testit_mcp`` domain behind a permission no
other test user holds, so no existing tool count, domain listing or permission
assertion moves.
"""
import uuid as uuid_module

import ujson

from testit import helpers as th
from testit.helpers import assert_eq, assert_true


ADMIN_EMAIL = "mcp-server-admin@example.com"
LIMITED_EMAIL = "mcp-server-limited@example.com"
NOPERM_EMAIL = "mcp-server-noperm@example.com"
TEST_PASSWORD = "TestPass1!"
TEST_PERM = "testit_mcp"
TEST_DOMAIN = "testit_mcp"

CLIENT_NAME = "Testit MCP Client"
CLIENT_ID = "testit-mcp-client"
RESOURCE = "https://oauth.testit.example/api/assistant/mcp"

MCP_PATH = "/api/assistant/mcp"

# Handler call recorder. testit parallelizes at MODULE level and runs the tests
# inside a module sequentially, so a module-local list is safe here.
CALLS = []


def _tool_read(params, user, *, request_meta=None, conversation=None):
    CALLS.append({
        "tool": "testit_mcp_read",
        "params": dict(params),
        "user_id": user.pk,
        "request_meta": request_meta,
        "conversation_id": getattr(conversation, "pk", None),
        "conversation_metadata": dict(getattr(conversation, "metadata", None) or {}),
    })
    return {"read": True, "echo": params.get("target")}


def _tool_write(params, user, approval=None):
    CALLS.append({"tool": "testit_mcp_write", "params": dict(params)})
    return {"ok": True}


def _tool_infra(params, user, approval=None):
    CALLS.append({"tool": "testit_mcp_infra", "params": dict(params)})
    return {"ok": True}


_SCHEMA = {
    "type": "object",
    "properties": {"target": {"type": "string", "description": "What to act on"}},
    "required": ["target"],
}


def _register_test_tools():
    from mojo.apps.assistant import get_registry, register_tool

    if "testit_mcp_read" in get_registry():
        return
    register_tool(
        name="testit_mcp_read", description="Fixture MCP read-only tool",
        input_schema=_SCHEMA, handler=_tool_read, permission=TEST_PERM,
        mutates=False, domain=TEST_DOMAIN, core=False)
    register_tool(
        name="testit_mcp_write", description="Fixture MCP mutating tool",
        input_schema=_SCHEMA, handler=_tool_write, permission=TEST_PERM,
        mutates=True, domain=TEST_DOMAIN, core=False)
    register_tool(
        name="testit_mcp_infra", description="Fixture MCP managed-infra tool",
        input_schema=_SCHEMA, handler=_tool_infra, permission=TEST_PERM,
        mutates=True, domain=TEST_DOMAIN, core=False,
        requires_managed_infrastructure=True)


def _grant(user, client, scopes):
    from mojo.apps.account.services.oauth_server import tokens

    return tokens.create_grant(user, client, scopes, RESOURCE, 1700000000)


@th.django_unit_setup()
@th.requires_app("mojo.apps.assistant")
@th.requires_app("mojo.apps.account")
def setup_mcp_server(opts):
    from mojo.apps.account.models import OAuthClient, User

    _register_test_tools()

    # Deleting the users cascades to their conversations, pending actions and
    # grants, so no MCP conversation survives from an earlier run.
    User.objects.filter(
        email__in=[ADMIN_EMAIL, LIMITED_EMAIL, NOPERM_EMAIL]).delete()
    OAuthClient.objects.filter(client_id=CLIENT_ID).delete()

    opts.admin = User.objects.create_user(
        username=ADMIN_EMAIL, email=ADMIN_EMAIL, password=TEST_PASSWORD)
    opts.admin.is_email_verified = True
    opts.admin.save()
    for perm in ("view_admin", "assistant", "view_security", "manage_security",
                 "users", "manage_users", TEST_PERM):
        opts.admin.add_permission(perm)

    opts.limited = User.objects.create_user(
        username=LIMITED_EMAIL, email=LIMITED_EMAIL, password=TEST_PASSWORD)
    opts.limited.is_email_verified = True
    opts.limited.save()
    for perm in ("view_admin", "assistant"):
        opts.limited.add_permission(perm)

    opts.noperm = User.objects.create_user(
        username=NOPERM_EMAIL, email=NOPERM_EMAIL, password=TEST_PASSWORD)
    opts.noperm.is_email_verified = True
    opts.noperm.save()
    opts.noperm.remove_all_permissions()

    opts.client_row = OAuthClient.objects.create(
        client_id=CLIENT_ID, kind="dcr", client_name=CLIENT_NAME,
        redirect_uris=["http://127.0.0.1:8500/cb"])

    opts.grant = _grant(opts.admin, opts.client_row, ["mcp"])
    opts.grant_noscope = _grant(opts.admin, opts.client_row, [])
    opts.limited_grant = _grant(opts.limited, opts.client_row, ["mcp"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _recorder():
    """A call-local incident reporter: a list plus the callable that fills it."""
    events = []

    def report(details, **kwargs):
        row = dict(kwargs)
        row["details"] = details
        events.append(row)

    return events, report


def _request_meta(opts, user=None, grant=None):
    from mojo.apps.assistant.services import agent

    request = th.get_mock_request(
        user=user or opts.admin, path=MCP_PATH, method="POST")
    request.oauth_grant = grant or opts.grant
    return agent._build_request_meta(request)


def _handle(opts, payload, user=None, grant=None, hide_infrastructure=None,
            reporter=None):
    from mojo.apps.assistant.mcp import server

    raw = payload if isinstance(payload, (bytes, str)) else ujson.dumps(payload)
    return server.handle(
        raw, user or opts.admin, grant or opts.grant,
        _request_meta(opts, user=user, grant=grant), "testit-assistant",
        hide_infrastructure=hide_infrastructure, _reporter=reporter)


def _call(opts, name, arguments=None, user=None, grant=None,
          hide_infrastructure=None, reporter=None):
    from mojo.apps.assistant.mcp import server

    return server.call_tool(
        name, arguments if arguments is not None else {},
        user or opts.admin, grant or opts.grant,
        _request_meta(opts, user=user, grant=grant),
        hide_infrastructure=hide_infrastructure, _reporter=reporter)


def _text(result):
    return ujson.loads(result["content"][0]["text"])


def _mcp_conversations(opts, user=None):
    from mojo.apps.assistant.models import Conversation

    return Conversation.objects.filter(
        user=user or opts.admin, metadata__transport="mcp")


def _parse_error(raw):
    from mojo.apps.assistant.mcp import protocol

    try:
        protocol.parse_body(raw)
    except protocol.JsonRpcError as err:
        return err.code
    return None


def _classify_error(msg):
    from mojo.apps.assistant.mcp import protocol

    try:
        return protocol.classify(msg)
    except protocol.JsonRpcError as err:
        return err.code


# ---------------------------------------------------------------------------
# Framing
# ---------------------------------------------------------------------------

@th.django_unit_test("JSON-RPC framing separates transport errors from protocol errors")
def test_parse_body_and_classify(opts):
    from mojo.apps.assistant.mcp import protocol

    messages, is_batch = protocol.parse_body(
        ujson.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}))
    assert_eq(is_batch, False, "a single object body must not be a batch")
    assert_eq(len(messages), 1,
              f"a single object body must yield one message, got {len(messages)}")

    messages, is_batch = protocol.parse_body(ujson.dumps(
        [{"jsonrpc": "2.0", "id": 1, "method": "ping"},
         {"jsonrpc": "2.0", "method": "notifications/initialized"}]))
    assert_eq(is_batch, True, "an array body must be a batch")
    assert_eq(len(messages), 2,
              f"a two-element batch must yield two messages, got {len(messages)}")

    for raw, why in ((b"", "an empty body"), (b"{nope", "unparsable JSON"),
                     (b"\xff\xfe{}", "a non-UTF-8 body")):
        assert_eq(_parse_error(raw), protocol.PARSE_ERROR,
                  f"{why} must be a parse error, got {_parse_error(raw)}")

    oversize = ujson.dumps(
        [{"jsonrpc": "2.0", "method": "ping"}] * (protocol.MAX_BATCH + 1))
    for raw, why in ((b"null", "a JSON null"), (b"3", "a JSON scalar"),
                     (b'"x"', "a JSON string"), (b"[]", "an empty batch"),
                     (oversize, "an oversized batch")):
        assert_eq(_parse_error(raw), protocol.INVALID_REQUEST,
                  f"{why} must be an invalid request, got {_parse_error(raw)}")

    assert_eq(_classify_error({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
              "request", "an object with a method and an id is a request")
    assert_eq(_classify_error({"jsonrpc": "2.0", "method": "notifications/x"}),
              "notification", "an object with a method and no id is a notification")
    assert_eq(_classify_error({"jsonrpc": "2.0", "id": 1, "result": {}}),
              "response", "an object carrying a result is a client response")

    for msg, why in (
            ({"id": 1, "method": "ping"}, "a message with no jsonrpc version"),
            ({"jsonrpc": "1.0", "id": 1, "method": "ping"}, "jsonrpc 1.0"),
            ({"jsonrpc": "2.0", "id": 1, "method": ""}, "an empty method"),
            ({"jsonrpc": "2.0", "id": 1, "method": 7}, "a non-string method"),
            ({"jsonrpc": "2.0"}, "an object with neither method nor result"),
            ({"jsonrpc": "2.0", "id": None, "method": "ping"}, "a null id"),
            ({"jsonrpc": "2.0", "id": True, "method": "ping"}, "a boolean id"),
            ({"jsonrpc": "2.0", "id": 1.5, "method": "ping"}, "a float id"),
            ({"jsonrpc": "2.0", "id": {"a": 1}, "method": "ping"}, "an object id")):
        assert_eq(_classify_error(msg), protocol.INVALID_REQUEST,
                  f"{why} must be an invalid request, got {_classify_error(msg)}")

    assert_eq(_classify_error({"jsonrpc": "2.0", "id": 1, "method": "ping",
                               "params": [1, 2]}),
              protocol.INVALID_PARAMS,
              "a request whose params are not an object must be invalid params")
    assert_eq(_classify_error({"jsonrpc": "2.0", "method": "notifications/x",
                               "params": [1, 2]}),
              "notification",
              "a notification has no response slot, so bad params are dropped")

    assert_eq(protocol.message_id({"id": 0}), 0,
              "id 0 is a legitimate id and must round-trip")
    assert_true(protocol.message_id({"id": None}) is None,
                "a null id must never be echoed")


# ---------------------------------------------------------------------------
# Methods
# ---------------------------------------------------------------------------

@th.django_unit_test("initialize negotiates a supported revision and advertises tools only")
def test_initialize_negotiation(opts):
    import mojo
    from mojo.apps.assistant.mcp import protocol, server

    for requested in protocol.SUPPORTED_PROTOCOL_VERSIONS:
        status, payload = _handle(opts, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": requested}})
        assert_eq(status, 200, f"initialize must answer 200, got {status}")
        assert_eq(payload["result"]["protocolVersion"], requested,
                  f"a supported requested revision must be echoed, got "
                  f"{payload['result']['protocolVersion']!r}")

    status, payload = _handle(opts, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2099-01-01"}})
    result = payload["result"]
    assert_eq(result["protocolVersion"], protocol.PROTOCOL_VERSION,
              f"an unsupported requested revision must answer this server's own, "
              f"got {result['protocolVersion']!r}")
    assert_eq(result["capabilities"], {"tools": {"listChanged": False}},
              f"the server must advertise tools and nothing else, got "
              f"{result['capabilities']!r}")
    assert_eq(result["serverInfo"]["version"], mojo.__version__,
              f"serverInfo must carry the framework version, got "
              f"{result['serverInfo']['version']!r}")
    assert_eq(result["serverInfo"]["name"], "testit-assistant",
              f"serverInfo must carry the installation's server name, got "
              f"{result['serverInfo']['name']!r}")
    assert_true(bool(result.get("instructions")),
                "initialize must tell the client that mutating tools never run here")
    assert_true("approval" in result["instructions"],
                f"the instructions must name the approval hand-off, got "
                f"{result['instructions']!r}")
    assert_true(server.server_name().endswith("-assistant"),
                f"the server name must be derived from the installation slug, "
                f"got {server.server_name()!r}")


@th.django_unit_test("ping, notifications, batches and unknown methods answer correctly")
def test_ping_notifications_and_unknown_method(opts):
    from mojo.apps.assistant.mcp import protocol

    status, payload = _handle(opts, {"jsonrpc": "2.0", "id": 7, "method": "ping"})
    assert_eq(status, 200, f"ping must answer 200, got {status}")
    assert_eq(payload["result"], {}, f"ping must answer an empty result, got {payload}")
    assert_eq(payload["id"], 7, f"the request id must be echoed, got {payload['id']}")

    status, payload = _handle(opts, {"jsonrpc": "2.0", "id": 7, "method": "nope/method"})
    assert_eq(status, 200,
              f"a well-formed request with an unknown method is a protocol "
              f"answer, not a transport failure, got {status}")
    assert_eq(payload["error"]["code"], protocol.METHOD_NOT_FOUND,
              f"an unknown method must answer -32601, got {payload['error']}")
    assert_eq(payload["id"], 7, "the id must be echoed on a method error")

    status, payload = _handle(
        opts, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert_eq((status, payload), (202, None),
              f"a notification must be accepted with nothing to answer, got "
              f"{(status, payload)}")

    status, payload = _handle(opts, {"jsonrpc": "2.0", "id": 3, "result": {"ok": 1}})
    assert_eq((status, payload), (202, None),
              f"a client response object must be accepted and ignored, got "
              f"{(status, payload)}")

    status, payload = _handle(opts, [
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": "a", "method": "ping"}])
    assert_eq(status, 200, f"a batch must answer 200, got {status}")
    assert_eq([msg["id"] for msg in payload], [1, "a"],
              f"a batch must answer only its requests, in order, got {payload}")

    status, payload = _handle(opts, [
        {"jsonrpc": "2.0", "id": 1, "method": "ping"}, "garbage"])
    assert_eq(status, 200,
              f"one bad element must not fail the whole batch, got {status}")
    assert_eq(payload[1]["error"]["code"], protocol.INVALID_REQUEST,
              f"the bad element's slot must carry -32600, got {payload[1]}")
    assert_true(payload[1]["id"] is None,
                f"an unidentifiable element must answer with a null id, got "
                f"{payload[1]['id']!r}")

    status, payload = _handle(opts, {"jsonrpc": "2.0", "id": None, "method": "ping"})
    assert_eq(status, 400,
              f"a single malformed message is a transport failure, got {status}")
    assert_eq(payload["error"]["code"], protocol.INVALID_REQUEST,
              f"a null id must be refused as an invalid request, got {payload}")


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------

@th.django_unit_test("tools/list projects the registry and adds the two MCP-only reads")
def test_tools_list_projection(opts):
    from mojo.apps.assistant import get_registry
    from mojo.apps.assistant.mcp import server

    status, payload = _handle(
        opts, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        hide_infrastructure=False)
    assert_eq(status, 200, f"tools/list must answer 200, got {status}")
    tools = payload["result"]["tools"]
    names = [tool["name"] for tool in tools]

    for hidden in sorted(server.HIDDEN_TOOLS):
        assert_true(hidden not in names,
                    f"{hidden} is a conversation/chat-state tool and must never "
                    f"be offered over MCP")
    for kept in ("read_memory", "find_skill", "list_skills"):
        assert_true(kept in names,
                    f"{kept} is a read and must stay available over MCP, got "
                    f"{sorted(names)[:20]}")

    registry = get_registry()
    for tool in tools:
        assert_eq(sorted(tool.keys()),
                  ["annotations", "description", "inputSchema", "name"],
                  f"{tool['name']} must expose exactly the MCP Tool keys, got "
                  f"{sorted(tool.keys())}")
        if tool["name"] in registry:
            mutates = bool(registry[tool["name"]]["mutates"])
            assert_eq(tool["annotations"]["readOnlyHint"], not mutates,
                      f"{tool['name']} readOnlyHint must be the inverse of mutates")
            assert_eq(tool["annotations"]["destructiveHint"], mutates,
                      f"{tool['name']} destructiveHint must equal mutates")

    assert_eq(names[-2:], ["list_pending_actions", "get_pending_action"],
              f"the two MCP-only reads must be listed last, got {names[-3:]}")
    for tool in tools[-2:]:
        assert_eq(tool["annotations"]["readOnlyHint"], True,
                  f"{tool['name']} must be advertised read-only")

    limited = server.list_tools(opts.limited, hide_infrastructure=False)
    limited_names = [tool["name"] for tool in limited]
    assert_true("testit_mcp_read" not in limited_names,
                "a user without the tool's permission must not see it listed")
    assert_true("testit_mcp_read" in names,
                "the admin holds testit_mcp and must see the fixture tool")

    visible = [tool["name"] for tool in
               server.list_tools(opts.admin, hide_infrastructure=True)]
    assert_true("testit_mcp_infra" not in visible,
                "a managed-infrastructure tool must be hidden on an external "
                "installation")
    assert_true("testit_mcp_infra" not in server.projected_registry(True),
                "the projection itself must drop managed-infrastructure tools")
    assert_true("testit_mcp_infra" in server.projected_registry(False),
                "a managed installation must keep its infrastructure tools")


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

@th.django_unit_test("a read tool runs through _execute_tool on a lazy per-grant conversation")
def test_call_read_tool_round_trip(opts):
    del CALLS[:]

    _handle(opts, {"jsonrpc": "2.0", "id": 1, "method": "ping"})
    _handle(opts, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    listed = _call(opts, "list_pending_actions")
    assert_eq(_text(listed), {"actions": []},
              f"an unused connection has no cards, got {_text(listed)}")
    assert_eq(_mcp_conversations(opts).count(), 0,
              "ping, tools/list and list_pending_actions must not create a "
              "conversation")

    result = _call(opts, "testit_mcp_read", {"target": "alpha"})
    assert_eq(result["isError"], False,
              f"a successful read must not be an error result, got {result}")
    body = _text(result)
    assert_eq(body, {"read": True, "echo": "alpha"},
              f"the handler's output must be the text content, got {body}")
    assert_eq(result["structuredContent"], body,
              "structuredContent must carry the same object as the text")

    assert_eq(len(CALLS), 1, f"the handler must run exactly once, ran {len(CALLS)}")
    call = CALLS[0]
    assert_eq(call["request_meta"].bearer, "mcp",
              f"a tool must be able to tell it is talking to a remote agent, got "
              f"{call['request_meta'].bearer!r}")
    assert_eq(call["request_meta"].key_backed, False,
              "an OAuth grant is not a key-backed session")
    assert_eq(call["conversation_metadata"],
              {"transport": "mcp", "grant": opts.grant.pk},
              f"the conversation must be flagged for this grant, got "
              f"{call['conversation_metadata']}")

    conversations = _mcp_conversations(opts)
    assert_eq(conversations.count(), 1,
              f"the first registry call must create one conversation, got "
              f"{conversations.count()}")
    conversation = conversations.first()
    assert_eq(conversation.title, f"MCP: {CLIENT_NAME}",
              f"the conversation must be titled from the client, got "
              f"{conversation.title!r}")

    _call(opts, "testit_mcp_read", {"target": "beta"})
    assert_eq(CALLS[1]["conversation_id"], conversation.pk,
              "a second call must reuse the same conversation")
    assert_eq(_mcp_conversations(opts).count(), 1,
              "a second call must not create a second conversation")


@th.django_unit_test("a mutating tool returns an approval card the client can poll")
def test_call_mutating_tool_returns_card(opts):
    from mojo.apps.assistant import get_registry
    from mojo.apps.assistant.models import Conversation, PendingAction
    from mojo.apps.assistant.services import approvals

    del CALLS[:]
    result = _call(opts, "testit_mcp_write", {"target": "gamma"},
                   hide_infrastructure=False)
    assert_eq(result["isError"], False,
              f"an approval card is a successful tool result, got {result}")
    card = result["structuredContent"]
    assert_eq(card["status"], "approval_required",
              f"a mutating tool must propose, never execute, got {card}")
    action_id = card["action_id"]
    assert_true(bool(action_id), "the proposal must carry an action id")
    assert_eq(len(CALLS), 0,
              f"the mutating handler must NOT run, ran {len(CALLS)} times")

    conversation = _mcp_conversations(opts).first()
    row = PendingAction.objects.filter(uuid=uuid_module.UUID(action_id)).first()
    assert_true(row is not None, "the proposal must have created a record")
    assert_eq(row.state, "pending", f"the record must be pending, got {row.state}")
    assert_eq(row.user_id, opts.admin.pk, "the record must bind the calling operator")
    assert_eq(row.conversation_id, conversation.pk,
              "the record must be bound to this grant's conversation")

    again = _call(opts, "testit_mcp_write", {"target": "gamma"},
                  hide_infrastructure=False)
    assert_eq(again["structuredContent"]["action_id"], action_id,
              "identical arguments must dedupe onto the same pending card")

    # A card the operator has pending in the CHAT panel belongs to a different
    # conversation and must be invisible to the external client.
    Conversation.objects.filter(user=opts.admin, title="mcp-chat-scope").delete()
    chat = Conversation.objects.create(user=opts.admin, title="mcp-chat-scope")
    _payload, chat_block = approvals.propose(
        opts.admin, chat, "testit_mcp_write",
        get_registry()["testit_mcp_write"], {"target": "chat-only"})
    assert_true(chat_block is not None, "the chat proposal must have created a card")

    listed = _text(_call(opts, "list_pending_actions"))["actions"]
    ids = [block["action_id"] for block in listed]
    assert_true(action_id in ids,
                f"the client's own card must be listed, got {ids}")
    assert_true(chat_block["action_id"] not in ids,
                "a card proposed in the operator's chat must never be visible "
                "to the external client")

    fetched = _call(opts, "get_pending_action", {"action_id": action_id})
    assert_eq(fetched["isError"], False,
              f"the client's own card must be readable, got {fetched}")
    assert_eq(fetched["structuredContent"]["type"], "approval",
              f"get_pending_action must return the approval block, got "
              f"{fetched['structuredContent']}")

    events, reporter = _recorder()
    for bad, why in ((chat_block["action_id"], "the operator's chat card"),
                     (str(uuid_module.uuid4()), "an unknown id"),
                     ("garbage", "a malformed id")):
        refused = _call(opts, "get_pending_action", {"action_id": bad},
                        reporter=reporter)
        assert_eq(refused["isError"], True,
                  f"{why} must be refused, got {refused}")
        assert_eq(_text(refused), {"error": approvals.GENERIC_UNAVAILABLE},
                  f"{why} must return the ONE non-oracular refusal, got "
                  f"{_text(refused)}")

    denials = [event for event in events
               if event.get("category") == "assistant:approval:denied"]
    assert_eq(len(denials), 3,
              f"every refused lookup on a live connection must feed the "
              f"id-guessing budget, got {len(denials)}")

    other = _call(opts, "get_pending_action", {"action_id": action_id},
                  user=opts.limited, grant=opts.limited_grant, reporter=reporter)
    assert_eq(other["isError"], True,
              f"another user's card must be refused, got {other}")
    assert_eq(_text(other), {"error": approvals.GENERIC_UNAVAILABLE},
              f"another user's card must be refused identically, got {_text(other)}")


@th.django_unit_test("hidden, unknown, infrastructure and unpermitted names all refuse")
def test_call_refusals(opts):
    del CALLS[:]
    events, reporter = _recorder()

    unknown = _call(opts, "nope", reporter=reporter)
    assert_eq(unknown["isError"], True, f"an unknown tool must be an error result")
    assert_eq(_text(unknown), {"error": "Unknown tool: nope"},
              f"an unknown tool must use the chat path's own string, got "
              f"{_text(unknown)}")

    hidden = _call(opts, "load_tools", {"domains": ["security"]}, reporter=reporter)
    assert_eq(_text(hidden), {"error": "Unknown tool: load_tools"},
              f"a hidden tool must be indistinguishable from an unregistered "
              f"one, got {_text(hidden)}")
    conversation = _mcp_conversations(opts).first()
    assert_true(conversation is not None,
                "the refused calls must still have opened this grant's conversation")
    conversation.refresh_from_db()
    assert_true("active_domains" not in (conversation.metadata or {}),
                f"load_tools must never run over MCP, got metadata "
                f"{conversation.metadata}")

    infra = _call(opts, "testit_mcp_infra", {"target": "x"},
                  hide_infrastructure=True, reporter=reporter)
    assert_eq(_text(infra), {"error": "Unknown tool: testit_mcp_infra"},
              f"a managed-infrastructure tool must not exist on an external "
              f"installation, got {_text(infra)}")

    denied = _call(opts, "testit_mcp_read", {"target": "x"},
                   user=opts.limited, grant=opts.limited_grant, reporter=reporter)
    assert_eq(denied["isError"], True, "a permission miss must be an error result")
    assert_eq(_text(denied), {
        "error": "Permission denied. You need 'testit_mcp' to use testit_mcp_read."},
        f"a permission miss must use the chat path's own string, got {_text(denied)}")
    assert_eq(len(CALLS), 0,
              f"no refused call may reach a handler, ran {len(CALLS)} times")

    levels = [(event.get("level"), event.get("category")) for event in events]
    assert_eq(levels.count((6, "assistant:permission_denied")), 3,
              f"each unknown/hidden/infrastructure name must fire the level-6 "
              f"event, got {levels}")
    assert_eq(levels.count((5, "assistant:permission_denied")), 1,
              f"a permission miss must fire the level-5 event, got {levels}")

    from mojo.apps.assistant.mcp import protocol

    for params, why in (
            ({}, "a call with no name"),
            ({"name": ""}, "a call with an empty name"),
            ({"name": 7}, "a call with a non-string name"),
            ({"name": "testit_mcp_read", "arguments": [1, 2]},
             "a call whose arguments are not an object")):
        status, payload = _handle(opts, {
            "jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": params})
        assert_eq(status, 200,
                  f"{why} is a protocol error, not a transport failure, got {status}")
        assert_eq(payload["error"]["code"], protocol.INVALID_PARAMS,
                  f"{why} must answer -32602, got {payload}")
