"""Static browser-foundation contracts kept independent of feature behavior."""

from pathlib import Path

from testit import helpers as th


ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "mojo/apps/account/admin_portal/assets"


@th.django_unit_test("Admin shell owns lifecycle while features own pages")
def test_modular_shell_contract(opts):
    app = (ASSETS / "app.js").read_text()
    registry = (ASSETS / "features/registry.js").read_text()
    assert "feature.render({ctx: context, route, navigate, signal: renderController.signal})" in app
    assert "controller?.abort()" in app and "page.dispose?.()" in app
    assert "page instanceof Node" in app and "closeAllOverlays()" in app
    assert "features/platform/page.js" not in app and "features/people/page.js" not in app
    for name in ("dashboard", "people", "webapps", "activity", "platform", "advanced"):
        assert f"./{name}/feature.js" in registry


@th.django_unit_test("shared relationship controls preserve paged REST envelopes")
def test_relationship_component_contract(opts):
    core = (ASSETS / "core.js").read_text()
    relationship = (ASSETS / "components/relationship.js").read_text()
    assert "export async function apiEnvelope" in core
    for key in ("data", "results", "items", "count", "start", "size", "raw"):
        assert f"{key}" in core
    assert "URLSearchParams" in relationship and "encodeURIComponent(detail)" in relationship
    assert "AbortController" in relationship and "generation !== this.generation" in relationship
    assert "role: 'combobox'" in relationship and "role: 'listbox'" in relationship
    assert "ArrowDown" in relationship and "Load more" in relationship
    assert "setCustomValidity" in relationship and "type: 'hidden'" in relationship


@th.django_unit_test("nested overlays scrub route state and restore focus")
def test_overlay_contract(opts):
    overlays = (ASSETS / "components/overlays.js").read_text()
    assert "const STACK = []" in overlays and "aria-hidden" in overlays
    assert "entry.previous" not in overlays  # closure-held opener is never serialized.
    assert "previous?.isConnected" in overlays and "closeAllOverlays" in overlays
    assert "requireReason" in overlays and "A reason is required." in overlays


@th.django_unit_test("feature lanes retain provider-safe hosting workflows")
def test_feature_asset_contracts(opts):
    advanced = (ASSETS / "features/advanced/page.js").read_text()
    platform = (ASSETS / "features/platform/page.js").read_text()
    webapps = (ASSETS / "features/webapps/page.js").read_text()
    core = (ASSETS / "core.js").read_text()
    app = (ASSETS / "app.js").read_text()
    preview = (ROOT / "bin/admin_preview_support/server.py").read_text()

    assert "result.token" not in advanced and "MOJO_DEPLOY_KEY" not in advanced
    assert "apiOnce" in platform and "refresh-required" in platform
    assert "class: 'table-wrap', tabindex: '0', role: 'region'" in core
    assert "['ArrowLeft', 'ArrowRight'].includes(event.key)" in core
    assert "canonicalRecordName" in platform and "sameRecordSet" in platform
    assert "ensureRoute" in platform and "ROUTE_REPAIR_KEY" in platform
    assert "routeState(await loadRoutes(), desired)" in platform
    assert "rememberRoute(desired" in platform and "forgetRoute(desired)" in platform
    assert "result.token = null" in webapps and "quote.token = null" in platform
    assert "secretPayload.api_secret = ''" in platform
    assert "data-webapp-key" in webapps
    assert "oneTimeSecret(webapp, result, returnFocus)" in webapps
    assert "--setup-state" in preview and "[redacted]" in preview
    assert "setup_choice_operation" in preview
    assert "Deterministic partial route failure" in preview
    assert "mojo-admin-theme" in app and "focus?.({preventScroll: true})" in app
    for shape in ("api", "site", "site_api", "redirect"):
        assert f"'{shape}'" in platform
    for endpoint in (
            "/api/dnsman/registrar/purchase", "/api/dnsman/credential/link",
            "/api/dnsman/dns", "/api/dnsman/certificate/request",
            "/api/edge/upstream/declare", "/api/edge/vhost",
            "/api/edge/route"):
        assert endpoint in platform, f"Platform is missing {endpoint}"
