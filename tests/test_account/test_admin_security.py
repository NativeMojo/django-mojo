"""Packaged Admin v2 Security feature and fail-closed browser contracts."""

from pathlib import Path

from testit import helpers as th


TESTIT_TIER = "admin"
ROOT = Path(__file__).resolve().parents[2]
V2 = ROOT / "mojo/apps/account/admin_portal_v2/assets"
SECURITY = V2 / "features/security"


@th.django_unit_test("Security bootstrap is separate, exact and fail closed")
def test_security_feature_provider(opts):
    from mojo.apps.account.services.admin_features import security

    assert security.describe(None, {}) == {
        "id": "security", "enabled": False,
        "capabilities": {"view": False, "manage": False},
    }
    assert security.describe(None, {"admin": True}) == {
        "id": "security", "enabled": False,
        "capabilities": {"view": False, "manage": False},
    }, "literal portal admission must not disclose Security"
    assert security.describe(None, {"view_security": True}) == {
        "id": "security", "enabled": True,
        "capabilities": {"view": True, "manage": False},
    }
    assert security.describe(None, {"manage_security": True}) == {
        "id": "security", "enabled": False,
        "capabilities": {"view": False, "manage": True},
    }, "manage may never imply view inside a malformed bootstrap capability set"


@th.django_unit_test("Admin v2 packages exactly seven destinations with bounded Security")
def test_security_packaging_and_routes(opts):
    registry = (V2 / "features/registry.js").read_text()
    routes = (V2 / "components/routes.js").read_text()
    app = (V2 / "app.js").read_text()
    page = (SECURITY / "page.js").read_text()
    manifest = (SECURITY / "manifest.json").read_text()
    assert "[home, apps, infrastructure, domains, access, security, settings]" in registry
    assert "route: 'security-operations'" in (SECURITY / "manifest.js").read_text()
    for label in ("Overview", "Cases", "Incidents & events", "Rules",
                  "Firewall & IPSets", "Recommendations"):
        assert label in page
    for asset in ("manifest.js", "api.js", "page.js", "mojosec.js",
                  "activity.js", "rules.js", "firewall.js", "styles.css"):
        assert f'"{asset}"' in manifest
    assert "return routeHref('security-operations', state)" in routes
    assert "['incidents', 'events'].includes(requested.state.tab)" in app
    assert "capabilities?.view !== true" in app, "legacy Security hashes must fail closed"


@th.django_unit_test("v2 Security uses only the typed authority and governed schemas")
def test_security_browser_authority(opts):
    sources = "\n".join(path.read_text() for path in SECURITY.glob("*.js"))
    api = (SECURITY / "api.js").read_text()
    rules = (SECURITY / "rules.js").read_text()
    assert "/api/incident/admin/security" in api
    assert "/api/incident/incident" not in sources
    assert "/api/incident/event" not in sources
    assert "SECURITY_SCHEMA_VERSION = 2" in api
    assert "actionSchemas(report)" in rules and "policySchema(report)" in rules
    assert "confirm_catch_all = catchAllInput.value" in rules
    assert "await reread?.()" in api and "SecurityConflictError" in api
    assert "apiOnce" in api, "governed mutations may not receive transport replay"
    for forbidden in ("expected_roster", "row.runner_id", "row.incarnation",
                      "row.source_key", "row.broker", "row.title", ".cidrs",
                      "validation_reason"):
        assert forbidden not in sources, f"browser Security source names forbidden material: {forbidden}"


@th.django_unit_test("both Admin clients preserve return hashes and never replay mutations on 401")
def test_admin_auth_recovery_contract(opts):
    for root in (ROOT / "mojo/apps/account/admin_portal/assets", V2):
        core = (root / "core.js").read_text()
        app = (root / "app.js").read_text()
        assert "const replaySafe = method === 'GET' || method === 'HEAD'" in core
        assert "mojo-admin:session-expired" in core and "returnPath" in core
        assert "AdminApiError" in core and "safeScalar" in core
        assert "response.status === 401 ? 'session_expired'" in core
        assert "mojo-admin:session-expired" in app
        assert "location.pathname}${location.search}${location.hash}" in app
        assert "context = null" in app and "closeAllOverlays()" in app


@th.django_unit_test("preview owns every Security evidence and recovery state")
def test_security_preview_contract(opts):
    preview = (ROOT / "bin/admin_preview_support/features/security.py").read_text()
    server = (ROOT / "bin/admin_preview_support/server.py").read_text()
    for state in ("full", "empty", "unavailable", "view-only", "no-access",
                  "partial", "failed", "stale", "expired-session", "440",
                  "conflict", "recovery"):
        assert f'"{state}"' in server
    assert "expected_host_ids" in preview and "missing_host_ids" in preview
    for forbidden in ("source_key", "runner_id", "expected_roster", "cidrs"):
        assert forbidden not in preview
    assert "security.get(self, parsed)" in server
    assert "security" in (ROOT / "bin/admin_preview_support/gallery.py").read_text()


@th.django_unit_test("real browser proof is explicit, isolated and deadline bounded")
def test_security_browser_harness_contract(opts):
    harness = (ROOT / "tests/test_account/test_admin_security_browser.py").read_text()
    assert "MOJO_ADMIN_CHROME" in harness and 'requires_extra("slow")' in harness
    assert "--remote-debugging-port" in harness and "--user-data-dir" in harness
    assert "TemporaryDirectory" in harness and "DEADLINE_SECONDS" in harness
    assert "Runtime.exceptionThrown" in harness and "Log.entryAdded" in harness
    for proof in ("case paging", "case filtering", "bounded case detail",
                  "narrow viewport", "dark theme", "keyboard focus"):
        assert proof in harness
    assert "process.terminate()" in harness and "process.kill()" in harness
