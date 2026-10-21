"""Thin health handlers/MCP authorization and scalar-contract projection."""
TESTIT_TIER = "admin"
from testit import helpers as th


@th.django_unit_test()
def test_health_registry_projection_and_refusal(opts):
    from mojo.apps.assistant import get_registry, user_can_use_tool
    from mojo.apps.assistant.mcp.server import _mcp_tool, projected_registry
    from mojo.apps.assistant.services.tools.cloud.reads import _tool_get_fleet_health, _tool_get_node_health
    class Denied:
        is_active = True
        is_request_user = True
        def has_permission(self, permissions):
            return False
    denied = Denied()
    for name in ("get_fleet_health", "get_node_health"):
        entry = get_registry()[name]
        assert not entry["mutates"] and not entry["core"], "health reads must stay on demand and declare no mutation"
        assert _mcp_tool(entry)["annotations"]["readOnlyHint"] is True, "MCP must advertise truthful read-only annotations"
        assert name in projected_registry(hide_infrastructure=True), "external infrastructure mode must retain diagnostic reads"
        assert not user_can_use_tool(denied, entry), "MCP registry must refuse callers lacking all global source grants"
    assert _tool_get_fleet_health({}, denied)["error_code"] == "permission_denied", "direct handler invocation cannot bypass service grants"
    assert _tool_get_fleet_health({"limit": True}, denied)["error_code"] == "invalid_limit", "bool must not pass the integer limit contract"
    assert _tool_get_fleet_health({"cursor": "forged"}, denied)["error_code"] == "invalid_cursor", "cursor signatures must be checked explicitly"
    assert _tool_get_node_health({"node_id": "x", "hours": True}, denied)["error_code"] == "invalid_hours", "bool must not pass the integer hours contract"
    assert _tool_get_node_health({"node_id": "x", "channel": "arbitrary"}, denied)["error_code"] == "invalid_request", "tools must reject caller-supplied collection channel/path inputs"


@th.django_unit_test()
def test_health_projection_preserves_cursor_and_metric_metadata(opts):
    from mojo.apps.assistant.services.tools.cloud.common import bounded
    value = {"coverage": {"next_cursor": "opaque-signed-" + "x" * 240},
             "node": {"trends": {"memory": {"namespace": "mojo/custom", "unit": "Percent",
                        "aggregation": "Average", "start": "2026-10-20T00:00:00Z",
                        "sample_count": 1, "last_sample_at": "2026-10-20T01:00:00Z",
                        "buckets": [{"value": None}, {"value": 0}]}}}}
    projected = bounded(value, depth=14, max_items=128, max_nodes=18000, max_string=1024)
    assert projected == value, "health projection must preserve signed continuation, null/zero values, and trend metadata"


@th.django_unit_test()
def test_final_continuation_remains_incomplete_through_mcp_dispatch(opts):
    import json
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from mojo.apps.assistant import get_registry
    from mojo.apps.assistant.services.agent import _execute_tool
    from mojo.apps.assistant.mcp.server import _call_result
    from mojo.apps.aws.services.fleet_health import report
    class Actor:
        is_active = True
        is_request_user = True
        email = "fixture@example.com"
        pk = 1
        def has_permission(self, permissions):
            return True
    actor = Actor()
    now = datetime.now(timezone.utc).isoformat()
    readers = {
        "expected": lambda grants, deadline: {"complete": True, "configured": True, "sources": {}, "spec": None,
            "declarations": [{"node_id": name, "hostname": name, "source": "configuration"} for name in ("a", "b")]},
        "inventory": lambda spec, deadline: {"complete": True, "instances": {}},
        "heartbeats": lambda deadline: [{"hostname": name, "runner_id": name, "last_heartbeat": now, "alive": True}
                                        for name in ("a", "b")],
        "scheduler": lambda deadline: {"status": "ok", "scope": "installation"},
        "identity_cache": SimpleNamespace(read=lambda *args, **kwargs: None, write=lambda *args, **kwargs: True),
    }
    entry = dict(get_registry()["get_fleet_health"])
    entry["handler"] = lambda params, user: report(user, readers=readers, **params)
    registry = {"get_fleet_health": entry}
    def call(params):
        outcome = _execute_tool({"id": "fixture", "name": "get_fleet_health", "input": params},
                                registry, actor, SimpleNamespace(pk=1), [], None, [])
        return _call_result(outcome["content"])["structuredContent"]
    first = call({"limit": 1})
    last = call({"limit": 1, "cursor": first["coverage"]["next_cursor"]})
    assert last["coverage"]["has_more"] is False, "final page must terminate continuation"
    assert last["coverage"]["complete"] is False and last["collection_status"] == "partial", "a final continuation page is still a partial fleet response"
    assert last["health"] != "healthy", "MCP pagination cannot manufacture a healthy full fleet"
