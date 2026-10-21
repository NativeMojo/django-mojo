"""A separately consented MCP execution grant, using test-owned identities."""
import json

from testit import helpers as th


TESTIT_TIER = "core"
EMAIL = "testit-mcp-execution@example.com"
CLIENT_ID = "testit-mcp-execution"
PERMISSION = "testit_mcp_execution"
RESOURCE = "https://oauth.testit.example/api/assistant/mcp"
CALLS = []


def _write(params, user):
    CALLS.append({"params": dict(params), "user": user.pk})
    return {"changed": True}


def _approval_bound(params, user, approval=None):
    CALLS.append({"unexpected_approval_handler": True})
    return {"changed": True}


def _decline(params, user):
    return {"error": "fixture declined the change"}


def _preview(params, user):
    return {"revision": "testit-execution-revision", "summary": "Test preview"}


@th.django_unit_setup()
def setup_execution(opts):
    from mojo.apps.account.models import OAuthClient, User
    from mojo.apps.assistant import get_registry, register_tool

    User.objects.filter(email=EMAIL).delete()
    OAuthClient.objects.filter(client_id=CLIENT_ID).delete()
    opts.operator = User.objects.create_user(
        username=EMAIL, email=EMAIL, password="TestPass1!")
    for name in ["view_admin", "assistant", PERMISSION]:
        opts.operator.add_permission(name)
    opts.oauth_client = OAuthClient.objects.create(
        client_id=CLIENT_ID, kind="dcr", client_name="Delegated test client",
        redirect_uris=["http://127.0.0.1:8999/callback"])
    if "testit_delegation_write" not in get_registry():
        register_tool(
            name="testit_delegation_write", domain="testit_execution",
            permission=PERMISSION, mutates=True, handler=_write,
            description="A test-owned ordinary mutation.",
            input_schema={"type": "object", "properties": {
                "target": {"type": "string"}}, "required": ["target"]})
    schema = {"type": "object", "properties": {
        "target": {"type": "string"}}, "required": ["target"]}
    for suffix, handler, gates in [
            ("bound", _approval_bound, {}),
            ("fresh", _write, {"fresh_auth_seconds": 300}),
            ("preview", _write, {"preview": _preview}),
            ("operator", _write, {"requires_operator_approval": True}),
            ("superuser", _write, {"requires_superuser": True}),
            ("infra", _write, {"requires_managed_infrastructure": True}),
            ("decline", _decline, {})]:
        name = "testit_delegation_" + suffix
        if name not in get_registry():
            register_tool(name=name, domain="testit_execution",
                          permission=PERMISSION, mutates=True, handler=handler,
                          description="A test-owned mutation.", input_schema=schema, **gates)


def _grant(opts, scopes=None):
    from mojo.apps.account.services.oauth_server import tokens
    grant = tokens.create_grant(
        opts.operator, opts.oauth_client,
        scopes if scopes is not None else ["mcp", "mcp:execute"],
        RESOURCE, 1700000000)
    tokens.mint_access_token(grant)
    return grant


def _call(opts, grant, args=None, name="testit_delegation_write"):
    from mojo.apps.assistant.mcp import server
    from mojo.apps.assistant.services import agent
    request = th.get_mock_request(
        user=opts.operator, path="/api/assistant/mcp", method="POST")
    request.oauth_grant = grant
    events = []
    result = server.call_tool(
        name, {"target": "owned"} if args is None else args,
        opts.operator, grant, agent._build_request_meta(request),
        hide_infrastructure=False,
        _reporter=lambda details, **kwargs: events.append({
            "details": details, **kwargs}))
    return json.loads(result["content"][0]["text"]), events


@th.django_unit_test("explicitly delegated ordinary mutations execute without a second approval")
def test_execution_runs_handler_without_pending_action(opts):
    from mojo.apps.assistant.models import PendingAction
    grant = _grant(opts)
    before = len(CALLS)
    result, events = _call(opts, grant)
    th.assert_eq(result, {"changed": True},
                 "a separately consented execution grant must run an ordinary tool")
    th.assert_eq(len(CALLS), before + 1, "the handler must run exactly once per call")
    th.assert_true(not PendingAction.objects.filter(
        conversation__metadata__grant=grant.pk).exists(),
        "delegated execution must not manufacture a per-action human approval")
    writes = [event for event in events
              if event.get("category") == "assistant:tool:testit_delegation_write"]
    th.assert_eq(len(writes), 1, "each executed mutation needs its own audit evidence")
    th.assert_true(str(grant.pk) in writes[0]["details"] and
                   CLIENT_ID in writes[0]["details"],
                   "execution audit must identify the OAuth grant and client")


@th.django_unit_test("legacy consent and client trust flags do not grant execution")
def test_legacy_consent_proposes(opts):
    before = len(CALLS)
    grant = _grant(opts, ["mcp"])
    result, events = _call(opts, grant, {"target": "owned", "mcp_execute": True})
    th.assert_eq(result.get("status"), "approval_required", "old consent remains a proposal")
    th.assert_eq(len(CALLS), before, "a client argument cannot authorize execution")
    th.assert_true("approval_location" in result,
                   "a remaining handoff must identify the actual application surface")


@th.django_unit_test("protected handlers keep their real approval contract")
def test_protected_contracts_propose(opts):
    grant = _grant(opts)
    before = len(CALLS)
    for suffix in ["bound", "fresh", "preview", "operator"]:
        result, events = _call(opts, grant, name="testit_delegation_" + suffix)
        th.assert_eq(result.get("status"), "approval_required",
                     f"{suffix} must retain its per-action protection")
    th.assert_eq(len(CALLS), before, "no protected handler may run with missing proof")


@th.django_unit_test("delegation validates inputs before an ordinary handler")
def test_normalization_and_rejection(opts):
    grant = _grant(opts)
    before = len(CALLS)
    for args in [{}, {"target": []}, {"target": 42}]:
        result, events = _call(opts, grant, args)
        th.assert_true("error" in result, f"malformed arguments must be refused: {args}")
    th.assert_eq(len(CALLS), before, "bad arguments must never reach the handler")
    result, events = _call(opts, grant, {"target": "owned", "unexpected": "discard"})
    th.assert_eq(CALLS[-1]["params"], {"target": "owned"},
                 "direct execution must use the normalized schema arguments")


@th.django_unit_test("a stale authenticated grant cannot execute after revocation or expiry")
def test_live_authority_is_rechecked(opts):
    from mojo.apps.account.models.oauth_grant import OAuthGrant
    from mojo.helpers import dates
    before = len(CALLS)
    for changes in [
            {"is_active": False},
            {"access_expires": dates.subtract(seconds=1)},
            {"refresh_expires": dates.subtract(seconds=1)},
            {"access_jti": "rotated-testit-execution-jti"},
            {"scopes": ["mcp"]},
            {"resource": "https://oauth.testit.example/api"}]:
        grant = _grant(opts)
        OAuthGrant.objects.filter(pk=grant.pk).update(**changes)
        result, events = _call(opts, grant)
        th.assert_true("error" in result, f"live authority change must refuse: {changes.keys()}")
    th.assert_eq(len(CALLS), before, "a stale authorization cannot mutate anything")


@th.django_unit_test("permission, literal-superuser and inactive-principal checks survive delegation")
def test_live_principal_authority(opts):
    from mojo.apps.account.models import OAuthClient, User
    before = len(CALLS)
    result, events = _call(opts, _grant(opts), name="testit_delegation_superuser")
    th.assert_true("error" in result, "delegation cannot manufacture literal superuser authority")
    live = User.objects.get(pk=opts.operator.pk)
    try:
        live.remove_permission(PERMISSION)
        result, events = _call(opts, _grant(opts))
        th.assert_true("error" in result, "the live user's lost permission must be enforced")
    finally:
        opts.operator.refresh_from_db()
        opts.operator.add_permission(PERMISSION)
    for model, pk in [(OAuthClient, opts.oauth_client.pk), (User, opts.operator.pk)]:
        grant = _grant(opts)
        try:
            model.objects.filter(pk=pk).update(is_active=False)
            result, events = _call(opts, grant)
            th.assert_true("error" in result, "an inactive user or client must refuse execution")
        finally:
            model.objects.filter(pk=pk).update(is_active=True)
    th.assert_eq(len(CALLS), before, "none of the denied principals may run a handler")


@th.django_unit_test("forged scope and wrong-owner grants cannot execute")
def test_forged_scope_and_binding(opts):
    grant = _grant(opts, ["mcp"])
    grant.scopes = ["mcp", "mcp:execute"]
    before = len(CALLS)
    result, events = _call(opts, grant)
    th.assert_true("error" in result, "an in-memory scope claim cannot outrank stored consent")
    from mojo.apps.account.models import User
    from mojo.apps.account.models.oauth_grant import OAuthGrant
    foreign_email = "testit-mcp-execution-foreign@example.com"
    User.objects.filter(email=foreign_email).delete()
    foreign = User.objects.create_user(username=foreign_email, email=foreign_email)
    grant = _grant(opts)
    OAuthGrant.objects.filter(pk=grant.pk).update(user=foreign)
    result, events = _call(opts, grant)
    th.assert_true("error" in result, "execution authority must belong to this operator")
    th.assert_eq(len(CALLS), before, "forged or foreign authority cannot run a handler")
    foreign.delete()


@th.django_unit_test("delegated errors do not report a successful write")
def test_declined_handler_has_no_success_audit(opts):
    result, events = _call(opts, _grant(opts), name="testit_delegation_decline")
    th.assert_true("error" in result, "the handler's refusal must reach the caller")
    th.assert_true(not any(event.get("category", "").startswith("assistant:tool:")
                           for event in events), "a refused change has no success audit")


@th.django_unit_test("infrastructure ownership remains a runtime condition")
def test_runtime_infrastructure_refusal(opts):
    from mojo.apps.assistant import get_registry
    from mojo.apps.assistant.mcp import server
    from mojo.apps.assistant.services import agent, mcp_execution
    grant = _grant(opts)
    request = th.get_mock_request(user=opts.operator, path="/api/assistant/mcp", method="POST")
    request.oauth_grant = grant
    result = mcp_execution.decision(
        get_registry()["testit_delegation_infra"], {"target": "owned"},
        opts.operator, grant, server.conversation_for_grant(opts.operator, grant),
        agent._build_request_meta(request), _external=True)
    th.assert_true(result.error is not None and not result.exempt,
                   "delegated authority cannot take over externally managed infrastructure")


@th.django_unit_test("discovery describes the grant's actual execution mode")
def test_discovery_modes(opts):
    from mojo.apps.assistant.mcp import server
    grant = _grant(opts)
    definitions = {row["name"]: row for row in server.list_tools(opts.operator, False, grant)}
    th.assert_true("Executes immediately" in definitions["testit_delegation_write"]["description"],
                   "ordinary tool metadata must describe direct execution")
    th.assert_true("Executes immediately" not in definitions["testit_delegation_bound"]["description"],
                   "protected tool metadata must not advertise direct execution")
    raw = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    status, response = server.handle(raw, opts.operator, grant, None, "testit")
    th.assert_true("explicit delegated" in response["result"]["instructions"],
                   "initialize must state the execution consent actually present")
