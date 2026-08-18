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
    styles = (ASSETS / "components/components.css").read_text()
    assert "const STACK = []" in overlays and "aria-hidden" in overlays
    assert "entry.previous" not in overlays  # closure-held opener is never serialized.
    assert "previous?.isConnected" in overlays and "closeAllOverlays" in overlays
    assert "requireReason" in overlays and "A reason is required." in overlays
    assert ".inspector-scrim{z-index:110}" in styles, \
        "the shared inspector layer lost its explicit stacking level"
    assert ".modal-scrim{z-index:120}" in styles, \
        "centered modals must stack above an already-open inspector drawer"


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
    assert "oneTimeSecret(webapp, result)" in webapps
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


@th.django_unit_test("WebApps owns URL-first onboarding, external domains, and day-2")
def test_webapp_onboarding_asset_contract(opts):
    wizard = (ASSETS / "features/webapps/wizard.js").read_text()
    page = (ASSETS / "features/webapps/page.js").read_text()
    webapp_styles = (ASSETS / "features/webapps/styles.css").read_text()
    platform = (ASSETS / "features/platform/page.js").read_text()
    preview = (ROOT / "bin/admin_preview_support/server.py").read_text()

    # --- URL-first wizard (wizard.js) ---
    for step in ("Address", "Set up", "Deploy", "Go live"):
        assert step in wizard, f"WebApp onboarding omitted step {step}"
    assert "What web address do you want?" in wizard, \
        "onboarding does not start from the desired address"
    for endpoint in (
            "/api/edge/webapp/onboarding/precheck",
            "/api/edge/webapp/onboarding/options",
            "/api/edge/webapp/onboarding/create",
            "/api/edge/webapp/onboarding/detail",
            "/api/edge/webapp/onboarding/choose",
            "/api/edge/webapp/onboarding/cancel"):
        assert endpoint in wizard, f"WebApp onboarding omitted {endpoint}"
    assert "apiOnce('/api/edge/webapp/onboarding/choose'" in wizard, \
        "provider-bearing onboarding choice gained a transport retry"
    assert "apiOnce('/api/edge/webapp/onboarding/create'" in wizard, \
        "the create call is not the single durable one-shot"
    # URL steering: un-serveable shapes are guided, not errored.
    assert "['path', 'apex', 'deep_label'].includes" in wizard and "'Use it'" in wizard, \
        "the wizard does not steer path/apex/deep-label addresses with a suggestion"
    # External domains ride the delegated-ACME lifecycle inline.
    assert "/api/dnsman/delegation/initiate" in wizard and "/api/dnsman/delegation/verify" in wizard, \
        "keeping DNS elsewhere does not drive the delegation lifecycle"
    for card in ("Keep my DNS where it is", "Buy a new domain", "Use a domain you"):
        assert card in wizard, f"domain choice omitted the {card!r} path"
    assert "I’ve added them — check now" in wizard and "I’ve added it — check now" in wizard, \
        "the records screen has no user-driven re-check"
    # DNS authority is per selected group, never a global grant.
    assert "groupDnsAuthority" in wizard and "group?.can_manage_dns" in wizard, \
        "domain choices still depend on global rather than selected-group DNS authority"
    assert "NEW_GROUP_VALUE = 'new'" in wizard, \
        "the nonnumeric new-group sentinel is missing"
    # Progress is exposed to assistive technology.
    assert "aria-current" in wizard and "'aria-label': 'Setup progress'" in wizard, \
        "wizard progress is not exposed to assistive technology"
    # Durable, crash-safe draft: one UUID survives reload; abandonment is explicit.
    assert "sessionStorage.getItem(ONBOARDING_DRAFT_KEY)" in wizard and \
        "sessionStorage.setItem(ONBOARDING_DRAFT_KEY" in wizard, \
        "pending onboarding UUID/profile does not survive navigation and reload"
    assert "submitted: true, payload: frozenPayload" in wizard and "crypto.randomUUID()" in wizard, \
        "create cannot reconcile a lost response with one durable UUID"
    assert "draft?.submitted && draft.operation_id" in wizard and \
        "/api/edge/webapp/onboarding/detail?operation=" in wizard, \
        "reload does not reconcile a saved operation before creating"
    assert "clearPendingDraft()" in wizard and "Start over" in wizard, \
        "the browser lacks authoritative draft clearing or deliberate abandonment"
    assert "operation.cursor === 'app'" in wizard and "result.operation || result" in wizard, \
        "legacy app-cursor auto-advance or serialized-operation shape was lost"
    # Vocabulary: raw framework words stay out of the primary onboarding copy.
    for banned in ("vhost", "bucket", "slug"):
        assert f"text: '{banned}'" not in wizard and f'text: "{banned}"' not in wizard, \
            f"the wizard shows the framework word {banned!r} in visible copy"

    # --- day-2 management (page.js) ---
    for endpoint in (
            "/api/edge/webapp/summary",
            "/api/edge/webapp/deployment",
            "/api/edge/webapp/release",
            "/api/edge/webapp/rollback",
            "/api/edge/webapp/detach_address",
            "/api/edge/webapp/health",
            "/api/edge/webapp/key_status",
            "/api/edge/webapp/link_key",
            "/api/edge/webapp/onboarding/workflow"):
        assert endpoint in page, f"management view omitted {endpoint}"
    for tab in ("'Overview'", "'Deploys'", "'Deploy key'", "'Setup'", "'Danger'"):
        assert tab in page, f"management view omitted the {tab} tab"
    for action in ("Roll back", "Change address", "Take offline", "Delete app"):
        assert action in page, f"management view omitted the {action!r} action"
    assert "startWizard" in page and "resumeWizard" in page and "startChangeAddress" in page, \
        "the list does not launch or resume the wizard"
    assert "statuses.set(row.id" not in page, \
        "the list still fans out a per-row key_status request (N+1)"
    assert "result.token = null" in page and "secretField.value = ''" in page, \
        "the deploy-key reveal does not scrub its one-time value"

    # --- unchanged platform contracts still hold ---
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

    # --- style + preview contracts ---
    assert ".row-actions" in webapp_styles and "gap: .5rem" in webapp_styles, \
        "WebApp table actions have no stable spacing contract"
    assert "Technical details" in wizard and "].filter(Boolean)" in page, \
        "provider evidence or empty states can leak raw values into the wizard"
    assert "var(--border)" not in webapp_styles, \
        "WebApp styles use an undefined border token"
    assert "--onboarding-state" in preview and "lost_key" in preview and "new_group" in preview, \
        "preview cannot render onboarding recovery states"
    assert "cls._safe_payload(value)" in preview, \
        "preview redaction is not recursive"
