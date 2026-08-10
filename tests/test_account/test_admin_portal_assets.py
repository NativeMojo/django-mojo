"""Static browser-foundation contracts kept independent of feature behavior."""

from pathlib import Path

from testit import helpers as th


ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "mojo/apps/account/admin_portal/assets"


@th.django_unit_test("Admin shell owns lifecycle while features own pages")
def test_modular_shell_contract(opts):
    app = (ASSETS / "app.js").read_text()
    registry = (ASSETS / "features/registry.js").read_text()
    platform = (ASSETS / "features/platform/feature.js").read_text()
    advanced = (ASSETS / "features/advanced/feature.js").read_text()
    assert "feature.render({ctx: context, route, navigate, signal: renderController.signal})" in app
    assert "controller?.abort()" in app and "page.dispose?.()" in app
    assert "page instanceof Node" in app and "closeAllOverlays()" in app
    assert "features/platform/page.js" not in app and "features/people/page.js" not in app
    for name in ("dashboard", "people", "webapps", "activity", "platform", "advanced"):
        assert f"./{name}/feature.js" in registry
    assert "routes: ['platform', 'deployments', 'setup']" in platform
    assert "setupPage(ctx)" in platform and "platformPage(ctx, route)" in platform
    assert "const ROUTES = ['advanced', 'domains', 'credentials', 'dns', 'certificates'" in advanced
    assert "networkPage(ctx, route)" in advanced and "advancedControlPage(ctx)" in advanced


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
    platform = (ASSETS / "features/platform/page.js").read_text()
    advanced = (ASSETS / "features/advanced/page.js").read_text()
    webapps = (ASSETS / "features/webapps/page.js").read_text()
    core = (ASSETS / "core.js").read_text()
    app = (ASSETS / "app.js").read_text()
    preview = (ROOT / "bin/admin_preview_support/server.py").read_text()

    assert "result.token" not in platform and "MOJO_DEPLOY_KEY" not in platform
    assert "apiOnce" in advanced and "refresh-required" in advanced
    assert "'login.methods'" in advanced and "'registration.methods'" in advanced
    assert "name === 'password'" in advanced and "Save access methods" in advanced
    assert "class: 'table-wrap', tabindex: '0', role: 'region'" in core
    assert "['ArrowLeft', 'ArrowRight'].includes(event.key)" in core
    assert "canonicalRecordName" in advanced and "sameRecordSet" in advanced
    assert "ensureRoute" in advanced and "ROUTE_REPAIR_KEY" in advanced
    assert "routeState(await loadRoutes(), desired)" in advanced
    assert "rememberRoute(desired" in advanced and "forgetRoute(desired)" in advanced
    assert "result.token = null" in webapps and "quote.token = null" in advanced
    assert "secretPayload.api_secret = ''" in advanced
    assert "data-webapp-key" in webapps
    assert "oneTimeSecret(webapp, result, returnFocus)" in webapps
    assert "--setup-state" in preview and "[redacted]" in preview
    assert "setup_choice_operation" in preview
    assert "Deterministic partial route failure" in preview
    assert "mojo-admin-theme" in app and "focus?.({preventScroll: true})" in app
    for shape in ("api", "site", "site_api", "redirect"):
        assert f"'{shape}'" in advanced
    for endpoint in (
            "/api/dnsman/registrar/purchase", "/api/dnsman/credential/link",
            "/api/dnsman/dns", "/api/dnsman/certificate/request",
            "/api/edge/upstream/declare", "/api/edge/vhost",
            "/api/edge/route"):
        assert endpoint in advanced, f"Advanced is missing {endpoint}"


@th.django_unit_test("WebApps owns resumable onboarding and lost-response UX")
def test_webapp_onboarding_asset_contract(opts):
    webapps = (ASSETS / "features/webapps/page.js").read_text()
    webapp_styles = (ASSETS / "features/webapps/styles.css").read_text()
    platform = (ASSETS / "features/platform/page.js").read_text()
    preview = (ROOT / "bin/admin_preview_support/server.py").read_text()

    for step in ("WebApp", "Domain & DNS", "GitHub", "Go live"):
        assert step in webapps, f"WebApp onboarding omitted {step}"
    for endpoint in (
            "/api/edge/webapp/onboarding/options",
            "/api/edge/webapp/onboarding/create",
            "/api/edge/webapp/onboarding/detail",
            "/api/edge/webapp/onboarding/choose",
            "/api/edge/webapp/onboarding/workflow"):
        assert endpoint in webapps, f"WebApp onboarding omitted {endpoint}"
    assert "apiOnce('/api/edge/webapp/onboarding/choose'" in webapps, \
        "provider-bearing onboarding choice gained a transport retry"
    assert "do not replay it blindly" in webapps, \
        "lost provider response has no reconciliation guidance"
    assert "Apex onboarding is intentionally refused" in webapps, \
        "the UI implies apex onboarding is supported"
    assert "aria-current" in webapps and "aria-label': 'WebApp onboarding progress" in webapps, \
        "wizard progress is not exposed to assistive technology"
    start = webapps[webapps.index("function startOnboarding"):webapps.index("export async function webappsPage")]
    github = webapps[webapps.index("function githubChoice"):webapps.index("function wizardChoice")]
    assert "owner/repository" not in start and "owner/repository" in github, \
        "GitHub fields leaked into the WebApp identity step"
    assert "Continue to Domain & DNS" in start and "A guided setup with DNS included." in start, \
        "the first wizard step does not explain its single purpose"
    assert "We create the DNS record automatically" in webapps and "HTTPS is handled automatically" in webapps, \
        "the domain step does not explain automatic DNS and HTTPS"
    assert "target: '_blank', rel: 'noopener'" in webapps and "Add or connect a domain" in webapps, \
        "WebApp onboarding cannot reach first-class domain management safely"
    assert "group=${encodeURIComponent(groupId || '')}" in webapps and "group: groupId" in webapps, \
        "domain discovery and purchase are not bound to the selected WebApp group"
    assert "ctx.groups?.[0]?.id" not in webapps, \
        "WebApp onboarding silently fell back to the first visible group"
    assert "Domains & DNS" in platform and "Add domains and manage the public records" in platform, \
        "Platform still buries domain management under Advanced"
    assert ".row-actions" in webapp_styles and "gap: .5rem" in webapp_styles, \
        "WebApp table actions have no stable spacing contract"
    assert "Technical details" in webapps and "].filter(Boolean)" in webapps, \
        "provider evidence or empty states can leak raw values into the wizard"
    assert "var(--border)" not in webapp_styles, \
        "WebApp progress uses an undefined border token"
    assert "--onboarding-state" in preview and "lost_key" in preview, \
        "preview cannot render onboarding recovery states"
    assert "cls._safe_payload(value)" in preview, \
        "preview redaction is not recursive"
