"""Static browser-foundation contracts kept independent of feature behavior."""

from pathlib import Path

from testit import helpers as th


ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "mojo/apps/account/admin_portal/assets"


@th.django_unit_test("Admin shell owns lifecycle while features own pages")
def test_modular_shell_contract(opts):
    index = (ROOT / "mojo/apps/account/admin_portal/index.html").read_text()
    app = (ASSETS / "app.js").read_text()
    styles = (ASSETS / "admin.css").read_text()
    registry = (ASSETS / "features/registry.js").read_text()
    platform = (ASSETS / "features/platform/feature.js").read_text()
    advanced = (ASSETS / "features/advanced/feature.js").read_text()
    assert "feature.render({ctx: context, route, navigate, signal: renderController.signal})" in app
    assert "controller?.abort()" in app and "page.dispose?.()" in app
    assert "page instanceof Node" in app and "closeAllOverlays()" in app
    assert "features/platform/page.js" not in app and "features/people/page.js" not in app
    for name in ("dashboard", "people", "webapps", "activity", "platform", "advanced", "settings"):
        assert f"./{name}/feature.js" in registry
    assert "[dashboard, webapps, advanced, people, activity, platform, settings]" in registry, \
        "primary navigation does not follow the approved operator journey"
    assert "routes: ['platform', 'deployments', 'setup']" in platform
    assert "setupPage(ctx, signal)" in platform and "platformPage(ctx, route)" in platform
    assert "const ROUTES = ['advanced', 'domains', 'credentials', 'dns', 'certificates'" in advanced
    assert "route: 'domains'" in advanced and "label: 'Domains & DNS'" in advanced, \
        "the permanent Domains & DNS control is missing from the sidebar"
    assert "ctx.capabilities.network || ctx.capabilities.manage_network" in advanced, \
        "Domains & DNS sidebar visibility is not permission-gated"
    assert "networkPage(ctx, route)" in advanced and "advancedControlPage(ctx)" in advanced
    assert "src: 'assets/mojo-logo.png'" in app and "brand-mark', text: 'M'" not in app, \
        "Admin shell did not replace the placeholder badge with the Mojo logo"
    assert '<link rel="icon" type="image/png" href="assets/mojo-logo.png">' in index, \
        "Admin shell does not use the Mojo logo as its browser favicon"
    assert ".brand-mark{display:block;width:32px;height:32px;object-fit:contain" in styles, \
        "Admin logo has no stable sidebar sizing contract"


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


@th.django_unit_test("Admin dates accept epoch seconds and ISO timestamps")
def test_datetime_contract(opts):
    core = (ASSETS / "core.js").read_text()
    platform = (ASSETS / "features/platform/page.js").read_text()
    preview = (ROOT / "bin/admin_preview_support/features/platform.py").read_text()
    assert "Math.abs(numeric) < 100000000000" in core and "numeric * 1000" in core, \
        "the shared date formatter does not normalize Unix epoch seconds"
    assert "new Date(entry.at)" not in platform, \
        "the Setup operation log bypasses the shared epoch/ISO date formatter"
    assert "new Date(section.observed_at)" not in platform, \
        "Platform evidence bypasses the shared epoch/ISO date formatter"
    assert "observed_at=1786384800" in preview, \
        "the visual preview does not exercise a production-shaped epoch timestamp"


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
    settings = (ASSETS / "features/settings/page.js").read_text()
    webapps = (ASSETS / "features/webapps/page.js").read_text()
    core = (ASSETS / "core.js").read_text()
    app = (ASSETS / "app.js").read_text()
    preview = (ROOT / "bin/admin_preview_support/server.py").read_text()

    assert "result.token" not in platform and "MOJO_DEPLOY_KEY" not in platform
    assert "apiOnce" in advanced and "refresh-required" in advanced
    assert "'login.methods'" in settings and "'registration.methods'" in settings
    assert "name === 'password'" in settings and "Save authentication settings" in settings
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
    assert "ctx.webapp_groups || []" in webapps and "ctx.can_create_webapp_group" in webapps, \
        "WebApp onboarding does not consume its purpose-specific group choices"
    assert "Create New Group" in webapps and "NEW_GROUP_VALUE = 'new'" in webapps, \
        "the conditional nonnumeric new-group sentinel is missing"
    assert "Number.parseInt(group.value, 10)" in webapps and \
        "Number(group.value)" not in webapps, \
        "the group selector can still coerce an empty or new intent to zero"
    assert "sessionStorage.getItem(ONBOARDING_DRAFT_KEY)" in webapps and \
        "sessionStorage.setItem(ONBOARDING_DRAFT_KEY" in webapps, \
        "pending onboarding UUID/profile does not survive navigation and reload"
    assert "operation_id: crypto.randomUUID()" in webapps and \
        "apiOnce('/api/edge/webapp/onboarding/create'" in webapps, \
        "create cannot reconcile a lost response with one durable UUID"
    assert "operation.group.id" in webapps and "operation.cursor === 'app'" in webapps, \
        "serialized owning group or legacy app-cursor compatibility was lost"
    assert "clearPendingDraft();" in webapps and "Start over" in webapps, \
        "the browser lacks authoritative draft clearing or deliberate abandonment"
    assert "Add domains and manage the public records" not in platform, \
        "Platform duplicates the first-class Domains & DNS destination"
    assert "evidenceSummary" in platform and "View raw evidence" in platform, \
        "Platform still exposes raw evidence instead of a professional summary-first card"
    assert "Configure BASE_URL" in platform and "check.code === 'django.base_url'" in platform, \
        "Setup does not attach BASE_URL repair to the exact failing check"
    assert "operatorChecks" in platform and "django.static_directories" in platform \
        and "configured_static" in platform, \
        "Setup does not hide legacy inferred listener/static noise from live targets"
    assert "/api/aws/s3/bucket" in platform and "Existing S3 bucket" in platform \
        and "Use this bucket" in platform and "preserves its objects" in platform, \
        "Setup does not turn legacy S3 choices into a clear existing-bucket journey"
    assert "current.kind === 'base_url'" in platform, \
        "the detected BASE_URL explanation can leak into unrelated Setup choices"
    assert "Changes applied and verified" in platform and "Applying changes" in platform \
        and "Verifying changes" in platform, \
        "Setup exposes internal mutation states instead of a clear Fix outcome"
    assert "networkChecklist" not in platform and "Advanced resources" not in platform, \
        "Platform or Setup still renders a duplicate resource directory"
    assert "Technical details" in platform and "Return to Dashboard" in platform, \
        "Setup does not keep technical evidence quiet or preserve the operator journey"
    assert ".row-actions" in webapp_styles and "gap: .5rem" in webapp_styles, \
        "WebApp table actions have no stable spacing contract"
    assert "Technical details" in webapps and "].filter(Boolean)" in webapps, \
        "provider evidence or empty states can leak raw values into the wizard"
    assert "var(--border)" not in webapp_styles, \
        "WebApp progress uses an undefined border token"
    assert "--onboarding-state" in preview and "lost_key" in preview and "new_group" in preview, \
        "preview cannot render onboarding recovery states"
    assert "cls._safe_payload(value)" in preview, \
        "preview redaction is not recursive"
