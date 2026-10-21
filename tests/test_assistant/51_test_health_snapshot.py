"""Cached snapshot discovery and input/permission contract."""
TESTIT_TIER = "admin"
from testit import helpers as th


@th.django_unit_test()
def test_snapshot_is_read_only_and_rejects_collector_parameters(opts):
    from mojo.apps.assistant import get_registry, user_can_use_tool
    from mojo.apps.assistant.mcp.server import _mcp_tool, projected_registry
    from mojo.apps.assistant.services.tools.cloud.reads import _tool_get_health_snapshot
    class Denied:
        is_active = True
        is_authenticated = True
        is_request_user = True
        def has_permission(self, perms):
            return False
    entry = get_registry()["get_health_snapshot"]
    assert not entry["mutates"] and not entry["core"], "cached snapshot tool became mutating/core agent collection"
    assert _mcp_tool(entry)["annotations"]["readOnlyHint"], "snapshot MCP annotation does not say read-only"
    assert "get_health_snapshot" in projected_registry(hide_infrastructure=True), "external mode hid a read-only cached report"
    assert not user_can_use_tool(Denied(), entry), "snapshot tool discovery bypassed global platform grants"
    for parameter in ("refresh", "host", "origin", "function", "provider", "channel"):
        result = _tool_get_health_snapshot({parameter: "forbidden"}, Denied())
        assert result["error_code"] == "invalid_request", f"snapshot accepted caller collector input {parameter}"
    assert _tool_get_health_snapshot({}, Denied())["error_code"] == "permission_denied", "direct snapshot tool call bypassed its service entry grant"
