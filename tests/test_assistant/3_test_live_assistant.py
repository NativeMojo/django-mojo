"""
End-to-end tests for the admin assistant — real LLM calls via REST and WebSocket.

These tests require LLM_HANDLER_API_KEY to be set (in .env or environment).
They exercise the full round-trip: client → server → Claude API → response.

Skipped automatically when no API key is configured.
"""
import time
from testit import helpers as th
from testit.helpers import assert_true, assert_eq
from testit.ws_client import WsClient

TEST_EMAIL_ADMIN = 'assistant-live-admin@example.com'
TEST_EMAIL_NOPERM = 'assistant-live-noperm@example.com'
TEST_PASSWORD = 'TestPass1!'


def _has_api_key():
    """Check if LLM API key is available (test process side)."""
    from mojo.helpers import llm
    return bool(llm.get_api_key())


def _get_assistant_settings():
    """Build server_settings dict including API key + feature flag."""
    import os
    overrides = {"LLM_ADMIN_ENABLED": True}
    key = os.environ.get("LLM_HANDLER_API_KEY")
    if key:
        overrides["LLM_HANDLER_API_KEY"] = key
    return overrides


def _ws_connect_and_auth(opts, username, password):
    """Helper: login via REST, connect WS, authenticate, return (ws, uid)."""
    assert opts.client.login(username, password), f"REST login failed for {username}"
    uid = opts.client.jwt_data.uid

    ws_url = WsClient.build_url_from_host(opts.host, path="ws/realtime/")
    ws = WsClient(ws_url, logger=opts.logger)
    ws.connect(timeout=10.0)
    auth = ws.authenticate(opts.client.access_token, wait=True, timeout=10.0)
    assert auth.get("type") == "auth_success", f"WS auth failed: {auth}"
    return ws, uid


def _ws_collect_assistant_events(ws, timeout=60.0):
    """
    Collect assistant events until we get a final response or error.

    Events arrive directly as {"type": "assistant_*", ...} thanks to
    send_event_to_user (no "message" wrapper).

    Returns (tool_calls, final_event) where tool_calls is a list of
    tool_call event dicts and final_event is the response or error dict.
    """
    tool_calls = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        remaining = max(1.0, deadline - time.time())
        try:
            msg = ws.wait_for_types(
                {"assistant_tool_call", "assistant_response", "assistant_error"},
                timeout=remaining,
            )
            msg_type = msg.data.get("type")
            if msg_type == "assistant_tool_call":
                tool_calls.append(msg.data)
            elif msg_type in ("assistant_response", "assistant_error"):
                return tool_calls, msg.data
        except TimeoutError:
            break
    return tool_calls, None


@th.django_unit_setup()
@th.requires_app("mojo.apps.assistant")
@th.requires_extra("slow")
def setup_live_users(opts):
    from mojo.apps.account.models import User
    from mojo.apps.assistant.models import Conversation

    if not _has_api_key():
        opts.skip_live = True
        return
    opts.skip_live = False

    # Clean up
    User.objects.filter(email__in=[TEST_EMAIL_ADMIN, TEST_EMAIL_NOPERM]).delete()

    # Admin user with full perms
    admin = User.objects.create_user(
        username=TEST_EMAIL_ADMIN, email=TEST_EMAIL_ADMIN, password=TEST_PASSWORD,
    )
    admin.is_email_verified = True
    admin.save()
    for perm in ["view_admin", "view_security", "manage_security",
                 "view_jobs", "manage_jobs", "view_groups"]:
        admin.add_permission(perm)
    opts.admin_id = admin.id

    # User without view_admin
    noperm = User.objects.create_user(
        username=TEST_EMAIL_NOPERM, email=TEST_EMAIL_NOPERM, password=TEST_PASSWORD,
    )
    noperm.is_email_verified = True
    noperm.save()

    # Clean up any stale conversations
    Conversation.objects.filter(user__in=[admin, noperm]).delete()


# ---------------------------------------------------------------------------
# REST endpoint tests
# ---------------------------------------------------------------------------

@th.django_unit_test()
@th.requires_extra("slow")
def test_rest_simple_question(opts):
    """POST /api/assistant with a simple question returns a real LLM response."""
    if opts.skip_live:
        return

    with th.server_settings(**_get_assistant_settings()):
        opts.client.login(TEST_EMAIL_ADMIN, TEST_PASSWORD)
        resp = opts.client.post('/api/assistant', {
            "message": "What tools do you have available? Just list the tool names briefly."
        })
        assert_eq(resp.status_code, 200,
                  f"Expected 200, got {resp.status_code}: {resp.json}")
        data = resp.json.data
        assert_true(data.response, "Expected non-empty response from LLM")
        assert_true(data.conversation_id, "Expected conversation_id in response")


@th.django_unit_test()
@th.requires_extra("slow")
def test_rest_tool_use(opts):
    """POST /api/assistant with a query that triggers tool use."""
    if opts.skip_live:
        return

    with th.server_settings(**_get_assistant_settings()):
        opts.client.login(TEST_EMAIL_ADMIN, TEST_PASSWORD)
        resp = opts.client.post('/api/assistant', {
            "message": "How many open incidents are there right now? Use the query_incidents tool to check."
        })
        assert_eq(resp.status_code, 200,
                  f"Expected 200, got {resp.status_code}: {resp.json}")
        data = resp.json.data
        assert_true(data.response, "Expected non-empty response")
        assert_true(data.conversation_id, "Expected conversation_id")
        assert_true(isinstance(data.tool_calls_made, list),
                    f"Expected tool_calls_made list, got {type(data.tool_calls_made)}")


@th.django_unit_test()
@th.requires_extra("slow")
def test_rest_multi_turn(opts):
    """Multi-turn conversation maintains context across requests."""
    if opts.skip_live:
        return

    with th.server_settings(**_get_assistant_settings()):
        opts.client.login(TEST_EMAIL_ADMIN, TEST_PASSWORD)

        # First message — establish context
        resp1 = opts.client.post('/api/assistant', {
            "message": "Remember this number for our conversation: 42."
        })
        assert_eq(resp1.status_code, 200,
                  f"First message failed: {resp1.status_code}: {resp1.json}")
        conv_id = resp1.json.data.conversation_id

        # Second message — reference the context
        resp2 = opts.client.post('/api/assistant', {
            "message": "What number did I just ask you to remember?",
            "conversation_id": conv_id,
        })
        assert_eq(resp2.status_code, 200,
                  f"Second message failed: {resp2.status_code}: {resp2.json}")
        assert_true("42" in resp2.json.data.response,
                    f"Expected '42' in response, got: {resp2.json.data.response[:200]}")


@th.django_unit_test()
@th.requires_extra("slow")
def test_rest_conversation_history_persisted(opts):
    """Messages from a REST round-trip are persisted in the conversation."""
    if opts.skip_live:
        return

    with th.server_settings(**_get_assistant_settings()):
        opts.client.login(TEST_EMAIL_ADMIN, TEST_PASSWORD)

        resp = opts.client.post('/api/assistant', {
            "message": "Say exactly: HELLO_TEST_MARKER"
        })
        assert_eq(resp.status_code, 200, f"POST failed: {resp.json}")
        conv_id = resp.json.data.conversation_id

        # Fetch conversation history
        hist = opts.client.get(f'/api/assistant/conversation/{conv_id}')
        assert_eq(hist.status_code, 200, f"GET history failed: {hist.json}")
        messages = hist.json.data.messages
        assert_true(len(messages) >= 2,
                    f"Expected at least 2 messages (user + assistant), got {len(messages)}")

        roles = [m.role for m in messages]
        assert_true("user" in roles, f"Expected 'user' role in messages, got {roles}")
        assert_true("assistant" in roles, f"Expected 'assistant' role in messages, got {roles}")


@th.django_unit_test()
@th.requires_extra("slow")
def test_rest_conversation_history_includes_blocks(opts):
    """GET conversation/<pk> returns parsed blocks on assistant messages."""
    if opts.skip_live:
        return

    with th.server_settings(**_get_assistant_settings()):
        opts.client.login(TEST_EMAIL_ADMIN, TEST_PASSWORD)

        # Ask for something that produces a stat block
        resp = opts.client.post('/api/assistant', {
            "message": (
                "Use get_system_health and present results as a stat block."
            ),
        })
        assert_eq(resp.status_code, 200, f"POST failed: {resp.json}")
        conv_id = resp.json.data.conversation_id

        # Fetch conversation history — should have blocks pre-parsed
        hist = opts.client.get(f'/api/assistant/conversation/{conv_id}')
        assert_eq(hist.status_code, 200, f"GET history failed: {hist.json}")
        messages = hist.json.data.messages

        # Find assistant messages with blocks
        assistant_msgs = [m for m in messages if m.role == "assistant"]
        blocks_found = any(
            hasattr(m, "blocks") and m.blocks for m in assistant_msgs
        )
        assert_true(blocks_found,
                    "Expected at least one assistant message with pre-parsed blocks in history")

        # Find tool interaction messages with tool_calls
        tool_msgs = [m for m in messages if m.role in ("tool_use", "tool_result") or
                     (hasattr(m, "tool_calls") and m.tool_calls)]
        assert_true(len(tool_msgs) > 0,
                    "Expected tool interaction messages with tool_calls in history")


@th.django_unit_test()
@th.requires_extra("slow")
def test_rest_disabled_returns_error(opts):
    """POST /api/assistant returns error when LLM_ADMIN_ENABLED is False."""
    if opts.skip_live:
        return

    # Default: LLM_ADMIN_ENABLED is False (not set)
    opts.client.login(TEST_EMAIL_ADMIN, TEST_PASSWORD)
    resp = opts.client.post('/api/assistant', {
        "message": "Hello"
    })
    data = resp.json
    assert_true(data.error, f"Expected error when disabled, got: {data}")


@th.django_unit_test()
@th.requires_extra("slow")
def test_rest_no_permission(opts):
    """POST /api/assistant denied without view_admin permission."""
    if opts.skip_live:
        return

    with th.server_settings(**_get_assistant_settings()):
        opts.client.login(TEST_EMAIL_NOPERM, TEST_PASSWORD)
        resp = opts.client.post('/api/assistant', {
            "message": "Hello"
        })
        assert_true(resp.status_code in [401, 403],
                    f"Expected 401/403 for user without view_admin, got {resp.status_code}")


# ---------------------------------------------------------------------------
# WebSocket tests
# ---------------------------------------------------------------------------

@th.django_unit_test()
@th.requires_extra("slow")
def test_ws_assistant_message_flow(opts):
    """WebSocket assistant_message -> thinking -> response flow."""
    if opts.skip_live:
        return

    with th.server_settings(**_get_assistant_settings()):
        ws, uid = _ws_connect_and_auth(opts, TEST_EMAIL_ADMIN, TEST_PASSWORD)
        try:
            # Send assistant message
            ws.send_json({
                "type": "assistant_message",
                "message": "What tools do you have available? Just list the tool names briefly.",
            })

            # Should get immediate thinking ack (direct WS response, not via topic)
            thinking = ws.wait_for_type("assistant_thinking", timeout=10.0)
            assert_true(thinking.data.get("conversation_id"),
                        "Expected conversation_id in thinking event")
            conv_id = thinking.data["conversation_id"]

            # Wait for final response (arrives via send_to_user -> Redis pub/sub)
            _, final = _ws_collect_assistant_events(ws, timeout=60.0)
            assert_true(final, "Expected a final response or error from assistant")
            assert_eq(final.get("type"), "assistant_response",
                      f"Expected assistant_response, got: {final}")
            assert_true(final.get("response"),
                        "Expected non-empty response text")
            assert_eq(final.get("conversation_id"), conv_id,
                      "conversation_id should match thinking event")
        finally:
            ws.close()


@th.django_unit_test()
@th.requires_extra("slow")
def test_ws_assistant_tool_call_events(opts):
    """WebSocket publishes tool_call events when the LLM calls tools."""
    if opts.skip_live:
        return

    with th.server_settings(**_get_assistant_settings()):
        ws, uid = _ws_connect_and_auth(opts, TEST_EMAIL_ADMIN, TEST_PASSWORD)
        try:
            # Ask something that should trigger tool use
            ws.send_json({
                "type": "assistant_message",
                "message": "Use query_incidents to check for any open incidents right now.",
            })

            # Wait for thinking
            ws.wait_for_type("assistant_thinking", timeout=10.0)

            # Collect tool calls and final response
            tool_calls, final = _ws_collect_assistant_events(ws, timeout=60.0)

            assert_true(final, "Expected a final response or error")
            assert_eq(final.get("type"), "assistant_response",
                      f"Expected assistant_response, got: {final}")
            assert_true(len(tool_calls) > 0,
                        "Expected at least one tool_call event")
            first_tc = tool_calls[0]
            assert_true(first_tc.get("tool"),
                        f"Expected 'tool' field in tool_call event, got: {first_tc}")
        finally:
            ws.close()


@th.django_unit_test()
@th.requires_extra("slow")
def test_ws_assistant_multi_turn(opts):
    """WebSocket multi-turn: second message references context from first."""
    if opts.skip_live:
        return

    with th.server_settings(**_get_assistant_settings()):
        ws, uid = _ws_connect_and_auth(opts, TEST_EMAIL_ADMIN, TEST_PASSWORD)
        try:
            # First message
            ws.send_json({
                "type": "assistant_message",
                "message": "Remember this code: BLUE-777",
            })
            thinking1 = ws.wait_for_type("assistant_thinking", timeout=10.0)
            conv_id = thinking1.data["conversation_id"]
            _ws_collect_assistant_events(ws, timeout=60.0)

            # Second message in same conversation
            ws.send_json({
                "type": "assistant_message",
                "message": "What code did I just ask you to remember?",
                "conversation_id": conv_id,
            })
            ws.wait_for_type("assistant_thinking", timeout=10.0)
            _, final = _ws_collect_assistant_events(ws, timeout=60.0)
            assert_true(final, "Expected response to second message")
            assert_eq(final.get("type"), "assistant_response",
                      f"Expected response, got: {final}")
            assert_true("BLUE-777" in final.get("response", ""),
                        f"Expected 'BLUE-777' in response: {final.get('response', '')[:300]}")
        finally:
            ws.close()


# ---------------------------------------------------------------------------
# Structured data block tests (table, stat, chart)
# ---------------------------------------------------------------------------

@th.django_unit_test()
@th.requires_extra("slow")
def test_rest_stat_blocks(opts):
    """Asking for system health should return stat blocks."""
    if opts.skip_live:
        return

    with th.server_settings(**_get_assistant_settings()):
        opts.client.login(TEST_EMAIL_ADMIN, TEST_PASSWORD)
        resp = opts.client.post('/api/assistant', {
            "message": (
                "Give me a system health overview using the get_system_health tool. "
                "Present the results as a stat block with key metrics."
            ),
        })
        assert_eq(resp.status_code, 200,
                  f"Expected 200, got {resp.status_code}: {resp.json}")
        data = resp.json.data
        assert_true(data.response, "Expected non-empty response")
        assert_true(data.blocks is not None and len(data.blocks) > 0,
                    f"Expected at least one structured block, got: {data.blocks}")
        block_types = [b.type for b in data.blocks]
        assert_true("stat" in block_types,
                    f"Expected a 'stat' block in response, got types: {block_types}")
        # Verify stat block has items
        stat_block = [b for b in data.blocks if b.type == "stat"][0]
        stat_items = stat_block["items"]
        assert_true(stat_items and len(stat_items) > 0,
                    f"Expected stat block to have items, got: {stat_block}")


@th.django_unit_test()
@th.requires_extra("slow")
def test_rest_table_blocks(opts):
    """Querying for a list of items should return table blocks."""
    if opts.skip_live:
        return

    with th.server_settings(**_get_assistant_settings()):
        opts.client.login(TEST_EMAIL_ADMIN, TEST_PASSWORD)
        resp = opts.client.post('/api/assistant', {
            "message": (
                "Use the query_jobs tool to list recent jobs. "
                "Present the results as a table with columns for ID, status, function, and created date."
            ),
        })
        assert_eq(resp.status_code, 200,
                  f"Expected 200, got {resp.status_code}: {resp.json}")
        data = resp.json.data
        assert_true(data.response, "Expected non-empty response")
        # If there are jobs, expect a table block. If no jobs, the LLM should say so.
        if data.blocks and len(data.blocks) > 0:
            block_types = [b.type for b in data.blocks]
            assert_true("table" in block_types,
                        f"Expected a 'table' block, got types: {block_types}")
            table_block = [b for b in data.blocks if b.type == "table"][0]
            assert_true(table_block.columns and len(table_block.columns) > 0,
                        f"Expected table block to have columns, got: {table_block}")


@th.django_unit_test()
@th.requires_extra("slow")
def test_rest_chart_blocks(opts):
    """Asking for trends should return chart blocks with series data."""
    if opts.skip_live:
        return

    with th.server_settings(**_get_assistant_settings()):
        opts.client.login(TEST_EMAIL_ADMIN, TEST_PASSWORD)
        resp = opts.client.post('/api/assistant', {
            "message": (
                "Use the get_incident_trends tool to get incident and event trends. "
                "Present the results as a chart block showing the trend over the time periods "
                "(1h, 6h, 24h, 7d) with series for both incidents and events."
            ),
        })
        assert_eq(resp.status_code, 200,
                  f"Expected 200, got {resp.status_code}: {resp.json}")
        data = resp.json.data
        assert_true(data.response, "Expected non-empty response")
        assert_true(data.blocks is not None and len(data.blocks) > 0,
                    f"Expected at least one structured block, got: {data.blocks}")
        block_types = [b.type for b in data.blocks]
        assert_true("chart" in block_types,
                    f"Expected a 'chart' block in response, got types: {block_types}")
        # Verify chart has series with values
        chart_block = [b for b in data.blocks if b.type == "chart"][0]
        assert_true(chart_block.series and len(chart_block.series) > 0,
                    f"Expected chart block to have series, got: {chart_block}")
        assert_true(chart_block.labels and len(chart_block.labels) > 0,
                    f"Expected chart block to have labels, got: {chart_block}")
        # Verify each series has values
        for s in chart_block.series:
            series_values = s["values"]
            assert_true(series_values is not None and len(series_values) > 0,
                        f"Expected series '{s.get('name', '?')}' to have values, got: {s}")


@th.django_unit_test()
@th.requires_extra("slow")
def test_ws_chart_blocks_via_websocket(opts):
    """WebSocket response includes chart blocks with series data."""
    if opts.skip_live:
        return

    with th.server_settings(**_get_assistant_settings()):
        ws, uid = _ws_connect_and_auth(opts, TEST_EMAIL_ADMIN, TEST_PASSWORD)
        try:
            ws.send_json({
                "type": "assistant_message",
                "message": (
                    "Use get_incident_trends to show me incident and event trends. "
                    "Present as a chart with series for incidents and events."
                ),
            })
            ws.wait_for_type("assistant_thinking", timeout=10.0)
            _, final = _ws_collect_assistant_events(ws, timeout=60.0)
            assert_true(final, "Expected a final response")
            assert_eq(final.get("type"), "assistant_response",
                      f"Expected assistant_response, got: {final}")
            blocks = final.get("blocks", [])
            assert_true(len(blocks) > 0,
                        f"Expected structured blocks in WS response, got none")
            chart_blocks = [b for b in blocks if b.get("type") == "chart"]
            assert_true(len(chart_blocks) > 0,
                        f"Expected chart block, got block types: {[b.get('type') for b in blocks]}")
            chart = chart_blocks[0]
            assert_true(chart.get("series") and len(chart["series"]) > 0,
                        f"Expected chart series, got: {chart}")
            assert_true(chart.get("labels") and len(chart["labels"]) > 0,
                        f"Expected chart labels, got: {chart}")
        finally:
            ws.close()


# ---------------------------------------------------------------------------
# WebSocket error path tests
# ---------------------------------------------------------------------------

@th.django_unit_test()
@th.requires_extra("slow")
def test_ws_assistant_disabled(opts):
    """WebSocket returns error when assistant is disabled."""
    if opts.skip_live:
        return

    # Default: LLM_ADMIN_ENABLED is False
    ws, uid = _ws_connect_and_auth(opts, TEST_EMAIL_ADMIN, TEST_PASSWORD)
    try:
        ws.send_json({
            "type": "assistant_message",
            "message": "Hello",
        })
        # Error comes as direct WS response (not via topic)
        err = ws.wait_for_type("assistant_error", timeout=10.0)
        assert_true("not enabled" in err.data.get("error", "").lower(),
                    f"Expected 'not enabled' in error: {err.data.get('error')}")
    finally:
        ws.close()


@th.django_unit_test()
@th.requires_extra("slow")
def test_ws_assistant_no_permission(opts):
    """WebSocket returns error for user without view_admin."""
    if opts.skip_live:
        return

    with th.server_settings(**_get_assistant_settings()):
        ws, uid = _ws_connect_and_auth(opts, TEST_EMAIL_NOPERM, TEST_PASSWORD)
        try:
            ws.send_json({
                "type": "assistant_message",
                "message": "Hello",
            })
            # Permission error comes as direct WS response
            err = ws.wait_for_type("assistant_error", timeout=10.0)
            assert_true("permission" in err.data.get("error", "").lower(),
                        f"Expected 'permission' in error: {err.data.get('error')}")
        finally:
            ws.close()


@th.django_unit_test()
@th.requires_extra("slow")
def test_ws_assistant_empty_message(opts):
    """WebSocket returns error for empty message."""
    if opts.skip_live:
        return

    with th.server_settings(**_get_assistant_settings()):
        ws, uid = _ws_connect_and_auth(opts, TEST_EMAIL_ADMIN, TEST_PASSWORD)
        try:
            ws.send_json({
                "type": "assistant_message",
                "message": "",
            })
            err = ws.wait_for_type("assistant_error", timeout=10.0)
            assert_true(err.data.get("error"),
                        f"Expected error message for empty message, got: {err.data}")
        finally:
            ws.close()
