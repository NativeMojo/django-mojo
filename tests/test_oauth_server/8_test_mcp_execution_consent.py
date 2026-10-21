"""Narrow, explicit MCP execution consent with no widening of old grants."""
from objict import objict
from testit import helpers as th


TESTIT_TIER = "core"
ORIGIN = "https://consent.testit.example"
PATH = "/api/assistant/mcp"


def _registry():
    from mojo.apps.account.services.oauth_server.resources import ResourceRegistry
    from mojo.apps.assistant.apps import register_oauth_resources
    registry = ResourceRegistry()
    register_oauth_resources(PATH, "/api", lambda: True, registry=registry)
    return registry


@th.django_unit_test("execution consent is narrow, explicit and discoverable")
def test_narrow_execution_consent(opts):
    from mojo.apps.account.services.oauth_server import consent, resources
    registry = _registry()
    result = consent.validate_scope_resource(
        {"scope": "mcp mcp:execute", "resource": ORIGIN + PATH}, ORIGIN, registry)
    th.assert_eq(result["granted"], ["mcp", "mcp:execute"], "consent preserves exact requested scopes")
    th.assert_true(not result["entry"].prefix, "execution consent grants tools, not full REST reach")
    th.assert_true("mcp:execute" in resources.offered_scopes(registry),
                   "clients must discover the execution scope")
    lines = consent.consent_context(
        objict(client_id="testit", client_name="Test client", kind="dcr"),
        result["granted"], result["resource"])["access_lines"]
    th.assert_eq(len(lines), 1, "execution consent must not contradict itself with proposal-only copy")
    th.assert_true("without approving each change" in lines[0] and "permissions" in lines[0],
                   "the person must understand the delegated authority they are granting")


@th.django_unit_test("execution scope cannot silently replace tools consent or widen to REST")
def test_execution_binding_and_legacy_copy(opts):
    from mojo.apps.account.services.oauth_server import consent
    registry = _registry()
    for scope, resource in [("mcp:execute", ORIGIN + PATH),
                            ("mcp mcp:execute api", ORIGIN + "/api"),
                            ("mcp mcp:execute", ORIGIN + "/api")]:
        refused = False
        try:
            consent.validate_scope_resource({"scope": scope, "resource": resource}, ORIGIN, registry)
        except consent.RedirectableError:
            refused = True
        th.assert_true(refused, f"unconsented scope/resource combination must refuse: {scope}")
    legacy = consent.validate_scope_resource({}, ORIGIN, registry)
    th.assert_eq(legacy["granted"], ["mcp"], "omitted scope never buys execution authority")
    th.assert_true("changes still need your approval" in consent._access_lines(["mcp"])[0],
                   "existing tools-only consent retains its promise")


@th.django_unit_test("the Admin distinguishes delegated tools from full REST and proposals")
def test_connected_agent_access_kind(opts):
    from mojo.apps.account.services.assistant_setup import _access_kind
    th.assert_eq(_access_kind(["mcp", "mcp:execute"]), "execute", "label delegated tool access")
    th.assert_eq(_access_kind(["mcp"]), "tools", "old grants remain proposal-only tools")
    th.assert_eq(_access_kind(["mcp", "api"]), "both", "old full API semantics remain unchanged")
