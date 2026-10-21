"""Read-only on-demand coverage discovery and exact inputs."""
TESTIT_TIER = "admin"
from testit import helpers as th


@th.django_unit_test()
def test_coverage_registry_and_input_refusals(opts):
    from mojo.apps.assistant import get_registry
    from mojo.apps.assistant.mcp.server import _mcp_tool
    from mojo.apps.assistant.services.tools.cloud.reads import _tool_get_monitoring_coverage
    class Denied:
        is_active = True
        is_authenticated = True
        is_request_user = True
        def has_permission(self, permissions):
            return False
    entry = get_registry()["get_monitoring_coverage"]
    assert not entry["mutates"] and _mcp_tool(entry)["annotations"]["readOnlyHint"], "monitoring coverage became mutating or misannotated"
    assert _tool_get_monitoring_coverage({"host": "untrusted"}, Denied())["error_code"] == "invalid_request", "coverage accepted arbitrary host targeting"
    assert _tool_get_monitoring_coverage({"node_id": "a", "cursor": "x"}, Denied())["error_code"] == "invalid_request", "coverage accepted conflicting node/cursor inputs"
    assert _tool_get_monitoring_coverage({}, Denied())["error_code"] == "permission_denied", "direct coverage invocation bypassed global source grants"
