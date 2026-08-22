"""The MCP door end to end, against the running server, with the switch ON.

This is the only module that turns ``ASSISTANT_MCP_ENABLED`` on, and it does so
the way the OAuth wire module does: through ``th.server_settings()`` and the
deployment-file plane, never by writing a Setting row.

``settings.get`` is DATABASE-first, so a leftover global row for ``BASE_URL`` or
``ASSISTANT_MCP_ENABLED`` — written by any module, in any earlier run — silently
outranks the file-plane override and makes every assertion here lie. Both keys
are cleared through the queryset (``Setting.delete()`` refuses protected keys)
and out of the Redis settings hash, in setup and in a ``finally`` around every
override. Neither key is ever WRITTEN by these tests; the toggle test drives the
Redis hash directly and cleans it up in its own ``finally``.

``th.server_settings()`` contexts are process-wide serialized and must never
nest, so every test opens exactly one at a time.
"""
import uuid as uuid_module

from testit import helpers as th
from testit.helpers import assert_eq, assert_true


BASE = "https://oauth.testit.example"
MCP_PATH = "/api/assistant/mcp"
RESOURCE = BASE + MCP_PATH
PRM_URL = f"{BASE}/.well-known/oauth-protected-resource{MCP_PATH}"

ADMIN_USER = "mcp_wire_admin"
PLAIN_USER = "mcp_wire_plain"
TEST_PWORD = "wire##mojo99"

CLIENT_NAME = "testit mcp wire client"
CLIENT_ID = "testit-mcp-wire-client"
API_KEY_NAME = "testit mcp wire key"
API_KEY_GROUP = "testit mcp wire group"

BLOCK_IP = "198.51.100.81"
SHADOWING_KEYS = ("BASE_URL", "ASSISTANT_MCP_ENABLED")
RATE_BUCKET = "assistant_mcp"


def _clear_shadowing_rows():
    """Drop DB/Redis values that would out-rank the file-plane override."""
    from mojo.apps.account.models import Setting

    for key in SHADOWING_KEYS:
        Setting.objects.filter(key=key, group=None).delete()
        try:
            Setting._redis().hdel(Setting._redis_key(), key)
        except Exception:
            pass


def _headers(opts):
    return {k.lower(): v for k, v in opts.client.last_response.headers.items()}


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _rpc(msg_id, method, params=None):
    payload = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        payload["params"] = params
    return payload


def _rate_keys():
    from mojo.helpers.redis import get_connection

    return list(get_connection().scan_iter(f"rl:{RATE_BUCKET}:*"))


@th.django_unit_setup()
@th.requires_app("mojo.apps.assistant")
def setup_mcp_wire(opts):
    from mojo.decorators.limits import clear_rate_limits
    from mojo.apps.account.models import (
        ApiKey, GeoLocatedIP, Group, OAuthClient, User)
    from mojo.apps.account.services.oauth_server import tokens

    _clear_shadowing_rows()
    clear_rate_limits(ip="127.0.0.1", key="login")
    for bucket in (RATE_BUCKET, "assistant_action"):
        clear_rate_limits(ip="127.0.0.1", key=bucket)

    User.objects.filter(username__in=[ADMIN_USER, PLAIN_USER]).delete()
    OAuthClient.objects.filter(client_id=CLIENT_ID).delete()
    ApiKey.objects.filter(name=API_KEY_NAME).delete()
    Group.objects.filter(name=API_KEY_GROUP).delete()
    GeoLocatedIP.objects.filter(ip_address=BLOCK_IP).delete()

    admin = User(username=ADMIN_USER, display_name=ADMIN_USER,
                 email=f"{ADMIN_USER}@example.com")
    admin.save()
    admin.is_email_verified = True
    admin.save()
    admin.save_password(TEST_PWORD)
    admin.remove_all_permissions()
    for perm in ("view_admin", "assistant", "view_security", "manage_security"):
        admin.add_permission(perm)
    opts.admin = admin

    plain = User(username=PLAIN_USER, display_name=PLAIN_USER,
                 email=f"{PLAIN_USER}@example.com")
    plain.save()
    plain.is_email_verified = True
    plain.save()
    plain.save_password(TEST_PWORD)
    plain.remove_all_permissions()
    opts.plain = plain

    client = OAuthClient.objects.create(
        client_id=CLIENT_ID, kind="dcr", client_name=CLIENT_NAME,
        redirect_uris=["http://127.0.0.1:8500/cb"])
    opts.oauth_client = client

    opts.grant = tokens.create_grant(admin, client, ["mcp"], RESOURCE, 1700000000)
    opts.token = tokens.mint_access_token(opts.grant)
    opts.grant_noscope = tokens.create_grant(admin, client, [], RESOURCE, 1700000000)
    opts.token_noscope = tokens.mint_access_token(opts.grant_noscope)

    group = Group.objects.create(name=API_KEY_GROUP, kind="organization")
    _key, opts.api_key_token = ApiKey.create_for_group(
        group=group, name=API_KEY_NAME, permissions={"view_admin": True})

    assert_true(opts.client.login(ADMIN_USER, TEST_PWORD),
                "the wire admin must be able to sign in")
    opts.session_token = opts.client.access_token
    opts.client.logout()


# ---------------------------------------------------------------------------
# Transport and challenges
# ---------------------------------------------------------------------------

@th.django_unit_test("the live door speaks MCP over HTTP and challenges every other credential")
def test_transport_and_challenges(opts):
    from mojo.apps.assistant.mcp import server

    _clear_shadowing_rows()
    for key in list(_rate_keys()):
        from mojo.helpers.redis import get_connection
        get_connection().delete(key)

    try:
        with th.server_settings(BASE_URL=BASE, ASSISTANT_MCP_ENABLED=True):
            # --- method ------------------------------------------------------
            for method, call in (("GET", lambda: opts.client.get(MCP_PATH)),
                                 ("DELETE", lambda: opts.client.delete(MCP_PATH))):
                resp = call()
                assert_eq(resp.status_code, 405,
                          f"{method} on the MCP door must be 405, got "
                          f"{resp.status_code}")
                assert_eq(_headers(opts).get("allow"), "POST",
                          f"{method} must be told what the door accepts, got "
                          f"{_headers(opts).get('allow')!r}")

            # --- credentials -------------------------------------------------
            expected = ('Bearer error="invalid_token", '
                        f'resource_metadata="{PRM_URL}"')
            for headers, why in (
                    (None, "an anonymous caller"),
                    (_auth(opts.session_token), "a browser session JWT"),
                    ({"Authorization": f"apikey {opts.api_key_token}"},
                     "an API key")):
                resp = opts.client.post(MCP_PATH, _rpc(1, "ping"), headers=headers)
                assert_eq(resp.status_code, 401,
                          f"{why} must be refused at the MCP door, got "
                          f"{resp.status_code}")
                assert_eq(_headers(opts).get("www-authenticate"), expected,
                          f"{why} must be told where to authenticate, got "
                          f"{_headers(opts).get('www-authenticate')!r}")

            resp = opts.client.post(MCP_PATH, _rpc(1, "ping"),
                                    headers=_auth(opts.token_noscope))
            assert_eq(resp.status_code, 403,
                      f"a grant without the mcp scope must be 403, got "
                      f"{resp.status_code}")
            challenge = _headers(opts).get("www-authenticate", "")
            for fragment in ('error="insufficient_scope"', 'scope="mcp"',
                             f'resource_metadata="{PRM_URL}"'):
                assert_true(fragment in challenge,
                            f"the scope challenge must carry {fragment}, got "
                            f"{challenge!r}")

            # --- protocol version -------------------------------------------
            resp = opts.client.post(
                MCP_PATH, _rpc(1, "ping"),
                headers=dict(_auth(opts.token),
                             **{"MCP-Protocol-Version": "1999-01-01"}))
            assert_eq(resp.status_code, 400,
                      f"an unsupported MCP-Protocol-Version must be refused, got "
                      f"{resp.status_code}")

            # --- initialize --------------------------------------------------
            resp = opts.client.post(
                MCP_PATH,
                _rpc(1, "initialize", {"protocolVersion": "2025-06-18",
                                       "capabilities": {},
                                       "clientInfo": {"name": "testit",
                                                      "version": "1"}}),
                headers=_auth(opts.token))
            assert_eq(resp.status_code, 200,
                      f"initialize must succeed for an mcp grant, got "
                      f"{resp.status_code} {resp.response}")
            assert_eq(resp.response.result.protocolVersion, "2025-06-18",
                      f"the negotiated revision must be echoed, got "
                      f"{resp.response.result}")
            headers = _headers(opts)
            assert_true("application/json" in headers.get("content-type", ""),
                        f"the door must answer plain JSON, never SSE, got "
                        f"{headers.get('content-type')!r}")
            assert_true("no-store" in headers.get("cache-control", ""),
                        f"an MCP response must not be cached, got "
                        f"{headers.get('cache-control')!r}")
            assert_true("MCP-Protocol-Version"
                        in headers.get("access-control-allow-headers", ""),
                        f"a browser-hosted client's preflight must pass, "
                        f"allow-headers is "
                        f"{headers.get('access-control-allow-headers')!r}")
            assert_true("mcp-session-id" not in headers,
                        "this server is stateless and must never issue a "
                        "session id")

            # --- notifications, malformed bodies, unknown methods, batches ---
            resp = opts.client.post(
                MCP_PATH, {"jsonrpc": "2.0", "method": "notifications/initialized"},
                headers=_auth(opts.token))
            assert_eq(resp.status_code, 202,
                      f"a notification must be accepted with 202, got "
                      f"{resp.status_code}")
            assert_true(not opts.client.last_response.body,
                        f"a 202 must carry no body, got "
                        f"{opts.client.last_response.body!r}")

            resp = opts.client.post(
                MCP_PATH, data=b"{nope",
                headers=dict(_auth(opts.token),
                             **{"Content-Type": "application/json"}))
            assert_eq(resp.status_code, 400,
                      f"an unparsable body is a transport failure, got "
                      f"{resp.status_code}")
            assert_eq(resp.response.error.code, -32700,
                      f"an unparsable body must answer -32700, got "
                      f"{resp.response}")

            resp = opts.client.post(MCP_PATH, _rpc(1, "nope"),
                                    headers=_auth(opts.token))
            assert_eq(resp.status_code, 200,
                      f"an unknown method is a protocol answer, got "
                      f"{resp.status_code}")
            assert_eq(resp.response.error.code, -32601,
                      f"an unknown method must answer -32601, got {resp.response}")

            resp = opts.client.post(
                MCP_PATH, [_rpc(1, "ping"), _rpc(2, "tools/list")],
                headers=_auth(opts.token))
            assert_eq(resp.status_code, 200,
                      f"a batch must be answered, got {resp.status_code}")
            assert_eq(len(resp.response), 2,
                      f"a two-request batch must answer two messages, got "
                      f"{resp.response}")
            assert_eq([msg["id"] for msg in resp.response], [1, 2],
                      "a batch must answer in the order it was sent")

            assert_true(bool(_rate_keys()),
                        "the live door must count against its own rate-limit "
                        "bucket")
            assert_true(bool(server.HIDDEN_TOOLS),
                        "the projection must actually hide something")
    finally:
        _clear_shadowing_rows()


# ---------------------------------------------------------------------------
# Tools, approvals, and the resolution boundary
# ---------------------------------------------------------------------------

@th.django_unit_test("a mutating call over MCP becomes the real card only the operator can resolve")
def test_tool_flow(opts):
    from mojo.apps.account.models import GeoLocatedIP
    from mojo.apps.assistant.mcp import server
    from mojo.apps.assistant.models import Conversation, PendingAction

    _clear_shadowing_rows()
    try:
        with th.server_settings(BASE_URL=BASE, ASSISTANT_MCP_ENABLED=True):
            resp = opts.client.post(MCP_PATH, _rpc(1, "tools/list"),
                                    headers=_auth(opts.token))
            assert_eq(resp.status_code, 200,
                      f"tools/list must succeed, got {resp.status_code} "
                      f"{resp.response}")
            names = [tool["name"] for tool in resp.response.result.tools]
            for hidden in sorted(server.HIDDEN_TOOLS):
                assert_true(hidden not in names,
                            f"{hidden} must never be offered over MCP, got "
                            f"{sorted(names)[:20]}")
            assert_true("list_pending_actions" in names,
                        f"the MCP-only poll tool must be listed, got "
                        f"{sorted(names)[:20]}")

            resp = opts.client.post(
                MCP_PATH,
                _rpc(2, "tools/call", {"name": "list_permissions",
                                       "arguments": {}}),
                headers=_auth(opts.token))
            assert_eq(resp.status_code, 200,
                      f"a read tool must succeed, got {resp.status_code}")
            result = resp.response.result
            assert_eq(result.isError, False,
                      f"a read tool must not be an error result, got {result}")
            assert_true(isinstance(result.structuredContent.permissions, list),
                        f"list_permissions must return a list, got "
                        f"{result.structuredContent}")

            resp = opts.client.post(
                MCP_PATH,
                _rpc(3, "tools/call", {
                    "name": "block_ip",
                    "arguments": {"ip": BLOCK_IP,
                                  "reason": "testit mcp wire"}}),
                headers=_auth(opts.token))
            assert_eq(resp.status_code, 200,
                      f"a mutating call must answer 200 with a card, got "
                      f"{resp.status_code}")
            card = resp.response.result.structuredContent
            assert_eq(card.status, "approval_required",
                      f"a mutating tool must propose, never execute, got {card}")
            action_id = card.action_id
            assert_true(bool(action_id), "the card must carry an action id")

            row = PendingAction.objects.filter(
                uuid=uuid_module.UUID(action_id)).first()
            assert_true(row is not None, "the proposal must have created a record")
            conversation = Conversation.objects.filter(pk=row.conversation_id).first()
            assert_eq((conversation.metadata or {}).get("transport"), "mcp",
                      f"the card must be bound to an MCP conversation, got "
                      f"{conversation.metadata}")
            blocked = GeoLocatedIP.objects.filter(
                ip_address=BLOCK_IP, is_blocked=True).exists()
            assert_true(not blocked,
                        "proposing must NOT have blocked the IP")

            resp = opts.client.post(
                MCP_PATH,
                _rpc(4, "tools/call", {"name": "list_pending_actions",
                                       "arguments": {}}),
                headers=_auth(opts.token))
            listed = resp.response.result.structuredContent.actions
            assert_true(action_id in [block["action_id"] for block in listed],
                        f"the client must be able to poll its own card, got "
                        f"{listed}")

            resp = opts.client.post(
                MCP_PATH,
                _rpc(5, "tools/call", {"name": "get_pending_action",
                                       "arguments": {"action_id": action_id}}),
                headers=_auth(opts.token))
            fetched = resp.response.result.structuredContent
            assert_eq(fetched.state, "pending",
                      f"the card must still be pending, got {fetched}")

            # The MCP token proposes and can never resolve.
            resp = opts.client.post(
                "/api/assistant/action",
                {"action_id": action_id, "decision": "approve"},
                headers=_auth(opts.token))
            assert_eq(resp.status_code, 401,
                      f"an mcp token must not authenticate at the resolution "
                      f"endpoint, got {resp.status_code}")
            blocked = GeoLocatedIP.objects.filter(
                ip_address=BLOCK_IP, is_blocked=True).exists()
            assert_true(not blocked,
                        "a refused resolution must not have blocked the IP")

            # The operator resolves it in the Admin, over an ordinary session.
            assert_true(opts.client.login(ADMIN_USER, TEST_PWORD),
                        "the operator must be able to sign in to approve")
            try:
                resp = opts.client.post(
                    "/api/assistant/action",
                    {"action_id": action_id, "decision": "approve"})
                assert_eq(resp.status_code, 200,
                          f"the bound operator must be able to approve, got "
                          f"{resp.status_code} {resp.response}")
                assert_eq(resp.response.data.action.state, "completed",
                          f"the approved action must complete, got "
                          f"{resp.response.data.action}")
            finally:
                opts.client.logout()

            assert_true(GeoLocatedIP.objects.filter(
                ip_address=BLOCK_IP, is_blocked=True).exists(),
                "the card an MCP client proposes must be the real, resolvable "
                "card — approving it must actually block the IP")
    finally:
        _clear_shadowing_rows()


# ---------------------------------------------------------------------------
# The switch
# ---------------------------------------------------------------------------

@th.django_unit_test("the switch takes effect on the very next request, in both directions")
def test_toggle_without_reload_then_disabled(opts):
    from mojo.apps.account.models import Setting

    _clear_shadowing_rows()
    try:
        with th.server_settings(BASE_URL=BASE, ASSISTANT_MCP_ENABLED=True):
            resp = opts.client.post(MCP_PATH, _rpc(1, "ping"),
                                    headers=_auth(opts.token))
            assert_eq(resp.status_code, 200,
                      f"the door must be live inside the override, got "
                      f"{resp.status_code}")

            # While the door is LIVE an anonymous caller is challenged — that is
            # what the flip below has to change, on the very next request.
            resp = opts.client.post(MCP_PATH, _rpc(1, "ping"))
            assert_eq(resp.status_code, 401,
                      f"a live door challenges an anonymous caller, got "
                      f"{resp.status_code}")
            assert_true(bool(_headers(opts).get("www-authenticate")),
                        "a live door must advertise where to authenticate")

            # The view reads settings.get on EVERY request and Setting.resolve is
            # Redis-first, so shadowing the key closes the door with no reload.
            try:
                Setting._redis().hset(
                    Setting._redis_key(), "ASSISTANT_MCP_ENABLED", "false")
                resp = opts.client.post(MCP_PATH, _rpc(1, "ping"))
                assert_eq(resp.status_code, 404,
                          f"the very next request after the switch flips must "
                          f"see a closed door, got {resp.status_code}")
                assert_true(_headers(opts).get("www-authenticate") is None,
                            "a closed door must stop advertising itself")
                # A PRESENTED token never reaches the view at all: the framework
                # chokepoint refuses a token for a dormant resource first, and
                # that refusal is equally immediate.
                resp = opts.client.post(MCP_PATH, _rpc(1, "ping"),
                                        headers=_auth(opts.token))
                assert_eq(resp.status_code, 401,
                          f"a token for a resource that just went dormant must "
                          f"be refused, got {resp.status_code}")
                assert_true(_headers(opts).get("www-authenticate") is None,
                            "a dormant resource must not advertise itself")
            finally:
                Setting._redis().hdel(
                    Setting._redis_key(), "ASSISTANT_MCP_ENABLED")

            resp = opts.client.post(MCP_PATH, _rpc(1, "ping"),
                                    headers=_auth(opts.token))
            assert_eq(resp.status_code, 200,
                      f"clearing the shadow must reopen the door immediately, "
                      f"got {resp.status_code}")
    finally:
        _clear_shadowing_rows()

    # A separate context — server_settings must never nest. DEBUG is turned off
    # so Django routes an unresolved path to the project's handler404 instead of
    # its own technical page, which is what makes the byte comparison meaningful.
    _clear_shadowing_rows()
    try:
        with th.server_settings(BASE_URL=BASE, ASSISTANT_MCP_ENABLED=False,
                                DEBUG=False):
            resp = opts.client.post(MCP_PATH, _rpc(1, "ping"),
                                    headers=_auth(opts.token))
            assert_eq(resp.status_code, 401,
                      f"a token for a dormant resource must be refused, got "
                      f"{resp.status_code}")
            assert_true(_headers(opts).get("www-authenticate") is None,
                        "a switched-off resource is not a live door and must "
                        "not advertise itself")

            unknown_app = opts.client.post("/api/assistant/no-such-route", {})
            unknown_app_body = opts.client.last_response.body
            unknown_root = opts.client.post("/no-such-route", {})
            unknown_root_body = opts.client.last_response.body

            disabled_post = opts.client.post(MCP_PATH, _rpc(1, "ping"))
            disabled_post_body = opts.client.last_response.body
            disabled_get = opts.client.get(MCP_PATH)

            assert_eq(disabled_post.status_code, 404,
                      f"an anonymous POST to a disabled door must 404, got "
                      f"{disabled_post.status_code}")
            assert_eq(disabled_get.status_code, 404,
                      f"a GET to a disabled door must 404 — not the 405 a live "
                      f"door answers, got {disabled_get.status_code}")
            assert_eq(unknown_app.status_code, 404,
                      f"the comparison route must itself be a 404, got "
                      f"{unknown_app.status_code}")
            assert_eq(disabled_post_body, unknown_app_body,
                      f"a disabled door must be indistinguishable from an "
                      f"unknown app route, got {disabled_post_body!r} vs "
                      f"{unknown_app_body!r}")
            assert_eq(disabled_post_body, unknown_root_body,
                      f"a disabled door must be indistinguishable from an "
                      f"unknown root route, got {disabled_post_body!r} vs "
                      f"{unknown_root_body!r}")
            assert_eq(unknown_root.status_code, 404,
                      "the root comparison route must itself be a 404")
    finally:
        _clear_shadowing_rows()
