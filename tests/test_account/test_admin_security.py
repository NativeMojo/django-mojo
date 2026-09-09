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


@th.django_unit_test("Portal package preserves pinned identity and private metadata")
def test_security_packaging_and_routes(opts):
    from mojo.apps.account.services import admin_artifact, admin_assets
    result = admin_artifact.validate(admin_assets.ROOT_V2, admin_artifact.PINNED_MANIFEST_SHA256)
    assert result["metadata"]["source_session_contract"] == 1, "source protocol changed"
    assert admin_assets.asset_path("v2/admin-artifact.json") is None, "provenance became HTTP-deliverable"
    assert len(result["allowlist"]) > 1, "lazy runtime inventory is missing"


@th.django_unit_test("both Admin clients preserve return hashes and never replay mutations on 401")
def test_admin_auth_recovery_contract(opts):
    for root in (ROOT / "mojo/apps/account/admin_portal/assets",):
        core = (root / "core.js").read_text()
        app = (root / "app.js").read_text()
        assert "const replaySafe = method === 'GET' || method === 'HEAD'" in core
        assert "mojo-admin:session-expired" in core and "returnPath" in core
        assert "AdminApiError" in core and "safeScalar" in core
        assert "effectiveResponseStatus" in core and "payload?.error_status" in core
        assert "const status = effectiveResponseStatus(payload, response)" in core
        assert "status === 401 ? 'session_expired'" in core
        assert "if (status === 401 && retry && replaySafe" in core
        assert "if (status === 440)" in core
        assert "if (!freshRetry) throw error" in core
        assert "return requestPayload(path, options, retry, false)" in core
        assert "mojo-admin:session-expired" in app
        assert "location.pathname}${location.search}${location.hash}" in app
        assert "context = null" in app and "closeAllOverlays()" in app


@th.django_unit_test("preview owns every Security evidence and recovery state")
def test_security_preview_contract(opts):
    preview = (ROOT / "bin/admin_preview_support/features/security.py").read_text()
    server = (ROOT / "bin/admin_preview_support/server.py").read_text()
    for state in ("full", "empty", "unavailable", "view-only", "no-access",
                  "partial", "failed", "stale", "expired-session", "440",
                  "conflict", "recovery", "malformed"):
        assert f'"{state}"' in server
    assert "expected_host_ids" in preview and "missing_host_ids" in preview
    assert "SCHEMA_VERSION = 3" in preview and "CAPABILITIES" in preview
    for required in ("203.0.113.7", "203.0.113.0/24", "/var/log/auth.log"):
        assert required in preview, f"preview omits operator evidence: {required}"
    for forbidden in ("source_key", "refresh_token", "access_token"):
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
    for proof in ("artifact identity", "protected lazy", "post-logout denial",
                  "narrow viewport", "dark theme", "two-tab", "renewal"):
        assert proof in harness
    assert "process.terminate()" in harness and "process.kill()" in harness
