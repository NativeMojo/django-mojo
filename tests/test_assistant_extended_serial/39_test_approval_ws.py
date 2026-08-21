"""Approval resolution over the WebSocket — `assistant_approval`.

Opt-in and serial for the same reason as its neighbour
`35_test_ws_request_correlation.py` (maestro item #1839): it mock.patches the
shared settings singleton, `threading.Thread` and the realtime manager around an
in-process handler call, which is unsafe under the parallel default tier. The
pure-service assertions live in `tests/test_assistant/37_test_approval_gate.py`.

What the socket must prove here:
  * a non-step-up action resolves and publishes `assistant_approval_result`
  * a step-up action refuses with `reauth_required` and executes nothing — the
    socket authenticates once at connect and holds no per-message token
  * a message whose server-stamped `_bearer` is not "bearer" is refused
  * an unknown id gets the one generic failure
  * `assistant_action` still carries no authority, `action_id` and all
"""
import uuid as uuid_module
from unittest import mock

from testit import helpers as th
from testit.helpers import assert_eq, assert_true


TEST_EMAIL = "approval-ws-admin@example.com"
TEST_PASSWORD = "TestPass1!"
TEST_PERM = "testit_ws_approvals"
TEST_DOMAIN = "testit_ws_approvals"
REQUEST_ID = "0c9b7a41-9d5b-4a08-8ad1-4a0b1e63c9a7"

CALLS = []


def _tool_ws(params, user, approval=None):
    CALLS.append({"params": dict(params), "user_id": user.pk})
    return {"ok": True}


def _tool_ws_fresh(params, user, approval=None):
    CALLS.append({"params": dict(params), "user_id": user.pk, "fresh": True})
    return {"ok": True}


_SCHEMA = {
    "type": "object",
    "properties": {"target": {"type": "string"}},
    "required": ["target"],
}


def _register_ws_tools():
    from mojo.apps.assistant import get_registry, register_tool

    if "testit_ws_approval_run" in get_registry():
        return
    register_tool(
        name="testit_ws_approval_run", description="WS fixture mutating tool",
        input_schema=_SCHEMA, handler=_tool_ws, permission=TEST_PERM,
        mutates=True, domain=TEST_DOMAIN, core=False,
    )
    register_tool(
        name="testit_ws_approval_fresh", description="WS fixture step-up tool",
        input_schema=_SCHEMA, handler=_tool_ws_fresh, permission=TEST_PERM,
        mutates=True, domain=TEST_DOMAIN, core=False, fresh_auth_seconds=600,
    )


@th.django_unit_setup()
@th.requires_app("mojo.apps.assistant")
def setup_approval_ws(opts):
    from mojo.apps.account.models import User

    _register_ws_tools()
    User.objects.filter(email=TEST_EMAIL).delete()
    opts.admin = User.objects.create_user(
        username=TEST_EMAIL, email=TEST_EMAIL, password=TEST_PASSWORD)
    opts.admin.is_email_verified = True
    opts.admin.save()
    for perm in ["view_admin", "assistant", TEST_PERM]:
        opts.admin.add_permission(perm)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class ImmediateThread:
    """Run the "background" work inline so the assertions are deterministic."""

    def __init__(self, target, args, daemon=False):
        self.target = target
        self.args = args

    def start(self):
        self.target(*self.args)


def _conversation(opts, title):
    from mojo.apps.assistant.models import Conversation

    Conversation.objects.filter(user=opts.admin, title=title).delete()
    return Conversation.objects.create(user=opts.admin, title=title)


def _propose(opts, conversation, tool_name="testit_ws_approval_run"):
    from mojo.apps.assistant import get_registry
    from mojo.apps.assistant.services import approvals

    entry = get_registry()[tool_name]
    _payload, block = approvals.propose(
        opts.admin, conversation, tool_name, entry, {"target": "alpha"})
    return block


def _row(action_id):
    from mojo.apps.assistant.models import PendingAction

    return PendingAction.objects.filter(uuid=uuid_module.UUID(action_id)).first()


def _send(opts, data):
    """Deliver one WS message through the real assistant handler."""
    from mojo.apps.assistant.handler import handle_assistant_message
    from mojo.helpers.settings import settings

    events = []
    original_get = settings.get

    def settings_get(name, *args, **kwargs):
        if name == "LLM_ADMIN_ENABLED":
            return True
        return original_get(name, *args, **kwargs)

    with mock.patch.object(settings, "get", side_effect=settings_get):
        with mock.patch("mojo.helpers.llm.get_api_key", return_value="sk-test"):
            with mock.patch("threading.Thread", ImmediateThread):
                with mock.patch(
                    "mojo.apps.realtime.manager.send_event_to_user",
                    side_effect=lambda _t, _u, event: events.append(event),
                ):
                    response = handle_assistant_message(opts.admin, data)
    return response, events


def _approval_message(conversation, action_id, decision="approve", bearer="bearer",
                      request_id=REQUEST_ID):
    data = {
        "type": "assistant_approval",
        "conversation_id": conversation.pk,
        "action_id": action_id,
        "decision": decision,
    }
    if bearer is not None:
        data["_bearer"] = bearer
    if request_id is not None:
        data["request_id"] = request_id
    return data


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@th.django_unit_test("assistant_approval resolves a non-step-up action over the socket")
def test_ws_approval_resolves(opts):
    del CALLS[:]
    conv = _conversation(opts, "ws-approval-ok")
    block = _propose(opts, conv)

    response, events = _send(opts, _approval_message(conv, block["action_id"]))

    assert_eq(response["type"], "assistant_approval_ack",
              f"the socket must ack immediately, got {response}")
    assert_eq(response.get("action_id"), block["action_id"],
              "the ack must echo the action id")
    assert_eq(response.get("request_id"), REQUEST_ID,
              "the ack must echo the client request_id")
    assert_eq([e["type"] for e in events], ["assistant_approval_result"],
              f"expected one approval result event, got {[e['type'] for e in events]}")
    result = events[0]
    assert_eq(result["block"]["state"], "completed",
              f"the published block must be resolved, got {result['block']}")
    assert_eq(result.get("request_id"), REQUEST_ID,
              "the result event must echo the client request_id")
    assert_eq(len(CALLS), 1, f"the handler must run exactly once, ran {len(CALLS)}")


@th.django_unit_test("a step-up action refuses over the socket and executes nothing")
def test_ws_step_up_action_refuses(opts):
    del CALLS[:]
    conv = _conversation(opts, "ws-approval-freshauth")
    block = _propose(opts, conv, tool_name="testit_ws_approval_fresh")

    response, events = _send(opts, _approval_message(conv, block["action_id"]))

    assert_eq(response["type"], "assistant_approval_ack",
              f"the socket still acks first, got {response}")
    assert_eq([e["type"] for e in events], ["assistant_error"],
              f"expected one error event, got {[e['type'] for e in events]}")
    assert_eq(events[0].get("code"), "reauth_required",
              f"a step-up action must refuse with reauth_required, got {events[0]}")
    assert_eq(events[0].get("action_id"), block["action_id"],
              "the refusal must name the action so the client can retry over REST")
    assert_eq(len(CALLS), 0, "a step-up refusal must never reach the handler")
    assert_eq(_row(block["action_id"]).state, "pending",
              "the action must stay approvable after step-up over REST")


@th.django_unit_test("a non-bearer socket cannot resolve an approval")
def test_ws_non_bearer_refused(opts):
    del CALLS[:]
    conv = _conversation(opts, "ws-approval-nonbearer")
    block = _propose(opts, conv)

    for bearer in ("apikey", "grouptoken", None):
        response, events = _send(
            opts, _approval_message(conv, block["action_id"], bearer=bearer))
        assert_eq(response["type"], "assistant_error",
                  f"a {bearer!r} socket must be refused, got {response}")
        assert_eq(response.get("code"), "action_unavailable",
                  f"the refusal must be the generic failure, got {response}")
        assert_eq(events, [], "a refused message must publish nothing")

    assert_eq(len(CALLS), 0, "a non-bearer socket must never reach the handler")
    assert_eq(_row(block["action_id"]).state, "pending",
              "a refused non-bearer message must not consume the action")


@th.django_unit_test("an unknown action id gets the one generic failure")
def test_ws_unknown_action(opts):
    del CALLS[:]
    conv = _conversation(opts, "ws-approval-unknown")

    _response, events = _send(
        opts, _approval_message(conv, str(uuid_module.uuid4())))

    assert_eq([e["type"] for e in events], ["assistant_error"],
              f"expected one error event, got {[e['type'] for e in events]}")
    assert_eq(events[0].get("code"), "action_unavailable",
              f"an unknown id must be action_unavailable, got {events[0]}")
    assert_eq(events[0].get("error"), "This action is no longer available.",
              f"an unknown id must return the one generic message, got {events[0]}")


@th.django_unit_test("a malformed request_id is refused before any work happens")
def test_ws_malformed_request_id(opts):
    del CALLS[:]
    conv = _conversation(opts, "ws-approval-badreqid")
    block = _propose(opts, conv)

    response, events = _send(opts, _approval_message(
        conv, block["action_id"], request_id="not-a-uuid"))

    assert_eq(response["type"], "assistant_error",
              f"a malformed request_id must be refused, got {response}")
    assert_eq(events, [], "nothing may be published for a malformed request_id")
    assert_eq(len(CALLS), 0, "a malformed request_id must never reach the handler")
    assert_eq(_row(block["action_id"]).state, "pending",
              "a malformed request_id must not consume the action")


@th.django_unit_test("a bad decision value is refused generically")
def test_ws_bad_decision(opts):
    del CALLS[:]
    conv = _conversation(opts, "ws-approval-baddecision")
    block = _propose(opts, conv)

    response, events = _send(opts, _approval_message(
        conv, block["action_id"], decision="yes-please"))

    assert_eq(response["type"], "assistant_error",
              f"an unknown decision must be refused, got {response}")
    assert_eq(response.get("code"), "action_unavailable",
              f"the refusal must be the generic failure, got {response}")
    assert_eq(events, [], "nothing may be published for a bad decision")
    assert_eq(len(CALLS), 0, "a bad decision must never reach the handler")


@th.django_unit_test("assistant_action carries no authority, action_id and all")
def test_ws_assistant_action_carries_no_authority(opts):
    from mojo.apps.assistant.models import Message

    del CALLS[:]
    conv = _conversation(opts, "ws-approval-quickreply")
    block = _propose(opts, conv)

    with mock.patch(
        "mojo.apps.assistant.services.agent.run_assistant_ws",
        return_value={"message_id": 1, "response": "ok"},
    ):
        response, _events = _send(opts, {
            "type": "assistant_action",
            "conversation_id": conv.pk,
            "action_id": block["action_id"],
            "value": "approve",
            "_bearer": "bearer",
        })

    assert_eq(response["type"], "assistant_thinking",
              f"a quick reply must be replayed as an ordinary message, got {response}")
    assert_eq(len(CALLS), 0,
              "a quick-reply click must never execute a mutating handler")
    assert_eq(_row(block["action_id"]).state, "pending",
              "a quick-reply click must never resolve a PendingAction")
    assert_true(Message.objects.filter(
        conversation=conv, role="user", content="approve").exists(),
        "the quick reply's value must land as a plain user message")


@th.django_unit_test("the realtime consumer stamps _bearer over anything the client sent")
def test_realtime_consumer_stamps_bearer(opts):
    import asyncio

    from mojo.apps.realtime.handler import WebSocketHandler

    seen = {}

    class FakeUser:
        def on_realtime_message(self, data):
            seen.update(data)
            return None

    async def _no_waiters(data):
        return None

    handler = WebSocketHandler.__new__(WebSocketHandler)
    handler.user = FakeUser()
    handler.bearer_prefix = "apikey"
    handler.check_waiters = _no_waiters
    handler._log = lambda message: None
    handler._log_exception = lambda message: None

    # A client claiming "bearer" over an api-key socket is exactly the attack
    # the stamp exists for.
    asyncio.run(handler.handle_custom_message(
        {"type": "assistant_approval", "_bearer": "bearer"}))

    assert_eq(seen.get("_bearer"), "apikey",
              f"the server stamp must overwrite a client-supplied _bearer, got {seen}")

    handler.bearer_prefix = "bearer"
    seen.clear()
    asyncio.run(handler.handle_custom_message({"type": "assistant_approval"}))
    assert_eq(seen.get("_bearer"), "bearer",
              f"a genuine bearer socket must be stamped 'bearer', got {seen}")
