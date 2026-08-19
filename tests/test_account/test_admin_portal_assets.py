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
    webapps = (ASSETS / "features/webapps/feature.js").read_text()
    assert "feature.render({ctx: context, route, navigate, signal: renderController.signal})" in app
    assert "controller?.abort()" in app and "page.dispose?.()" in app
    assert "page instanceof Node" in app and "closeAllOverlays()" in app
    assert "features/platform/page.js" not in app and "features/people/page.js" not in app
    for name in ("dashboard", "people", "webapps", "activity", "platform", "advanced", "settings", "sms"):
        assert f"./{name}/feature.js" in registry
    assert "[dashboard, webapps, advanced, people, activity, platform, settings, sms]" in registry, \
        "primary navigation does not follow the approved operator journey"
    assert "routes: ['setup', 'metrics', 'maintenance']" in platform, \
        "Platform grew or lost a route — health dissolved into the Dashboard, " \
        "deployments belongs to the merged lane"
    assert "routes: ['deployments', 'webapps']" in webapps, \
        "the merged Deployments lane does not own both routes"
    assert "label: 'Deployments'" in webapps, \
        "the lane is not labeled Deployments in primary navigation"
    assert "ctx.features?.platform?.capabilities?.view === true" in webapps, \
        "platform viewers lost their route to deploy history"
    assert "setupPage(ctx, signal)" in platform and "platformPage" not in platform, \
        "Setup lost its render call, or the dissolved Platform page came back"
    assert "const ROUTES = ['domains', 'credentials', 'dns', 'certificates'" in advanced, \
        "Advanced kept its raw-evidence route or lost a hosting route"
    assert "route: 'domains'" in advanced and "label: 'Domains & DNS'" in advanced, \
        "the permanent Domains & DNS control is missing from the sidebar"
    assert "ctx.capabilities.network || ctx.capabilities.manage_network" in advanced, \
        "Domains & DNS sidebar visibility is not permission-gated"
    assert "networkPage(ctx, route)" in advanced and "advancedControlPage" not in advanced, \
        "Advanced no longer renders hosting only — the raw-evidence page is back"
    assert "src: 'assets/mojo-logo.png'" in app and "brand-mark', text: 'M'" not in app, \
        "Admin shell did not replace the placeholder badge with the Mojo logo"
    assert '<link rel="icon" type="image/png" href="assets/mojo-logo.png">' in index, \
        "Admin shell does not use the Mojo logo as its browser favicon"
    assert ".brand-mark{display:block;width:32px;height:32px;object-fit:contain" in styles, \
        "Admin logo has no stable sidebar sizing contract"


@th.django_unit_test("the shared row layout is packaged, linked, and used by Dashboard")
def test_row_component_contract(opts):
    from mojo.apps.account.services import admin_assets

    index = (ROOT / "mojo/apps/account/admin_portal/index.html").read_text()
    rows = (ASSETS / "components/rows.js").read_text()
    row_styles = (ASSETS / "components/rows.css").read_text()
    dashboard = (ASSETS / "features/dashboard/page.js").read_text()
    preview = (ROOT / "bin/admin_preview_support/server.py").read_text()
    assets = admin_assets.load_manifest()

    for asset in ("assets/components/rows.js", "assets/components/rows.css"):
        assert asset in assets, f"{asset} is not a declared package asset"
    assert '<link rel="stylesheet" href="assets/components/rows.css">' in index, \
        "the shared row stylesheet is never loaded by the Admin shell"

    for builder in ("export function rowSection", "export function statusRow",
                    "export function statusHeadline", "export function rowLink"):
        assert builder in rows, f"rows.js does not expose {builder!r}"
    assert "valueNode" in rows and "detailNode" in rows and "action" in rows, \
        "statusRow lost the extension points sibling pages build on"
    for rule in (".row-page", ".row-section-label", ".status-row", ".row-value",
                 ".row-detail", ".row-link", ".status-headline", ".status-sub",
                 ".row-asof"):
        assert rule in row_styles, f"the shared row stylesheet is missing {rule}"
    assert "7px 130px minmax(0, 1fr) auto" in row_styles, \
        "the four-column row grid is not the locked layout"
    assert "@media (max-width: 600px)" in row_styles, \
        "the row layout does not collapse on a narrow viewport"

    assert "'../../components/rows.js'" in dashboard, \
        "Dashboard does not build on the shared row components"
    assert "statusHeadline(" in dashboard and "rowSection(" in dashboard, \
        "Dashboard did not adopt the locked status-page structure"
    assert "featureDescriptors" in dashboard and "'maintenance'" in dashboard, \
        "the maintenance link is not gated on the route actually existing"
    assert "refresh=1" in dashboard, \
        "the refresh control does not bypass the server-side caches"
    for jargon in ("Current evidence is healthy", "independently permissioned",
                   "Four answers", "dashboard-source", "dashboard-grid"):
        assert jargon not in dashboard, \
            f"the rebuilt Dashboard still carries {jargon!r}"
    assert '"down"' in preview or "'down'" in preview, \
        "the visual preview cannot render a proven outage"


@th.django_unit_test("every Dashboard row's evidence is one deliberate click away")
def test_dashboard_drilldown_contract(opts):
    import json
    from mojo.apps.account.services import admin_assets

    manifest = json.loads(
        (ASSETS / "features/dashboard/manifest.json").read_text())
    inspectors = (ASSETS / "features/dashboard/inspectors.js").read_text()
    dashboard = (ASSETS / "features/dashboard/page.js").read_text()
    assets = admin_assets.load_manifest()

    assert "inspectors.js" in manifest["assets"], \
        "the drill-in module is not owned by the dashboard feature manifest"
    assert "assets/features/dashboard/inspectors.js" in assets, \
        "inspectors.js is not a declared package asset"

    assert "openInspector" in inspectors and "'../../components/overlays.js'" in inspectors, \
        "the drill-ins do not use the shared overlay layer"
    assert "JSON.stringify(" in inspectors and "Technical details" in inspectors, \
        "the exact collector payload is not reachable behind a disclosure"

    # Both extra reads are narrowed and paid only when the drill-in is opened.
    assert "sections=fleet" in inspectors and "sections=security" in inspectors, \
        "the drill-ins pay the full platform overview instead of a sections slice"
    assert "capabilities?.view !== true" in inspectors, \
        "the edge runner roster is not gated on platform view"
    assert "capabilities?.security !== true" in inspectors, \
        "security posture is not gated on the platform-security tier"

    for name in ("feature.js", "page.js", "inspectors.js", "styles.css"):
        text = (ASSETS / f"features/dashboard/{name}").read_text()
        assert "innerHTML" not in text, \
            f"the Dashboard writes markup instead of text nodes in {name}"
        assert "checks passing" not in text, \
            f"{name} counts checks at the operator instead of naming the failure"

    assert "jobsRow" in dashboard and "SANITY_COPY" in dashboard, \
        "the Dashboard lost the jobs row or the plain-words sanity copy"
    assert "failed_recent" in dashboard, \
        "the jobs row colours itself from the all-time ledger, not the last hour"


@th.django_unit_test("System Setup is a badged sidebar destination, ordered below daily work")
def test_system_setup_nav_contract(opts):
    platform = (ASSETS / "features/platform/feature.js").read_text()
    registry = (ASSETS / "features/registry.js").read_text()
    app = (ASSETS / "app.js").read_text()
    styles = (ASSETS / "admin.css").read_text()

    assert "route: 'setup'" in platform and "label: 'System Setup'" in platform, \
        "System Setup is not a primary navigation destination"
    assert "section: 'System'" in platform and "order: 100" in platform, \
        "System Setup is not pinned below the Control plane entries"
    assert "badge: capabilities.setup_attention === true" in platform, \
        "the Setup entry's badge is not bound to the bootstrap attention flag"
    assert "capabilities.setup" in platform, \
        "the Setup entry is not gated on the superuser-only capability"

    assert "(left.order || 0) - (right.order || 0)" in registry, \
        "the sidebar no longer honours an entry's declared order"
    assert "'nav-badge'" in app and "'aria-label': 'Needs attention'" in app, \
        "the shell does not render an accessible attention badge"
    assert ".nav-badge{" in styles, \
        "the attention badge has no styling contract"


@th.django_unit_test("the Metrics lane is capability-gated, degradation-aware, and markup-free")
def test_metrics_asset_contract(opts):
    from mojo.apps.account.services import admin_assets

    metrics = (ASSETS / "features/platform/metrics.js").read_text()
    chart = (ASSETS / "features/platform/chart.js").read_text()
    feature = (ASSETS / "features/platform/feature.js").read_text()
    styles = (ASSETS / "features/platform/styles.css").read_text()
    core = (ASSETS / "core.js").read_text()
    assets = admin_assets.load_manifest()

    for asset in ("assets/features/platform/metrics.js",
                  "assets/features/platform/chart.js"):
        assert asset in assets, f"{asset} is not a declared package asset"

    for endpoint in ("/api/aws/cloudwatch/resources", "/api/aws/cloudwatch/fetch"):
        assert endpoint in metrics, f"the Metrics page never calls {endpoint}"
    assert "ctx.features?.platform?.capabilities?.metrics" in metrics, \
        "the Metrics page reads a raw capability instead of its feature lane"
    assert "dt_start" in metrics and "toISOString()" in metrics, \
        "the Metrics page does not send an explicit ISO time range"
    for reason in ("credentials_unavailable", "denied", "network_unavailable",
                   "service_error"):
        assert reason in metrics, f"the Metrics page cannot explain {reason}"
    assert "signal: local.signal" in metrics or "{signal: local.signal}" in metrics, \
        "the metric fetch is not abortable"

    assert "document.createElementNS" in chart, \
        "the chart does not build real SVG nodes"
    assert "innerHTML" not in chart, \
        "the chart writes markup — series names come from attacker-influenceable Name tags"
    assert "innerHTML" not in metrics, \
        "the Metrics page writes markup instead of textContent"
    assert "polyline" in chart and "fill: 'none'" in chart, \
        "the chart does not draw an unfilled line per series"
    assert "pointermove" in chart and "removeEventListener" in chart, \
        "the chart guide line leaks its listeners"
    assert "No non-zero datapoints in this range" in chart, \
        "an all-zero range is not called out"

    assert "--chart-1" in styles, "the chart palette is never declared"
    dark = styles.split('html[data-theme="dark"]', 1)
    assert len(dark) == 2 and "--chart-1" in dark[1].split("}", 1)[0], \
        "the chart palette is not redefined for the dark theme"
    assert "prefers-color-scheme:dark" in styles, \
        "the chart palette ignores an operator following the system theme"

    assert "capabilities.metrics" in feature and "route: 'metrics'" in feature, \
        "the Metrics sidebar entry is not gated on its capability"
    assert "chart:" in core, "the chart icon is missing from the shared catalog"


@th.django_unit_test("the Maintenance lane is capability-gated, confirmed, and honest about success")
def test_maintenance_asset_contract(opts):
    from mojo.apps.account.services import admin_assets

    maintenance = (ASSETS / "features/platform/maintenance.js").read_text()
    feature = (ASSETS / "features/platform/feature.js").read_text()
    core = (ASSETS / "core.js").read_text()
    assets = admin_assets.load_manifest()

    assert "assets/features/platform/maintenance.js" in assets, \
        "maintenance.js is not a declared package asset"
    for endpoint in ("/api/aws/maintenance/versions", "/api/aws/maintenance/status",
                     "/api/aws/maintenance/apply",
                     "/api/account/admin/platform/framework"):
        assert endpoint in maintenance, f"the Maintenance page never calls {endpoint}"

    assert "ctx.features?.platform?.capabilities?.maintenance" in maintenance, \
        "the Maintenance page reads a raw capability instead of its feature lane"
    assert "capabilities.maintenance" in feature and "route: 'maintenance'" in feature, \
        "the Maintenance sidebar entry is not gated on its capability"
    assert "refresh:" in core, "the refresh icon is missing from the shared catalog"

    # A browser confirm() cannot carry the apply window or the typed echo, and
    # cannot be styled as the destructive action it is.
    assert "openModal" in maintenance, \
        "the Maintenance page does not build its confirmation on the portal modal"
    assert "window.confirm" not in maintenance, \
        "the Maintenance page uses a browser confirm instead of the portal modal"
    assert "confirm_resource" in maintenance and "confirm_version" in maintenance, \
        "the typed confirmation is never sent to the server"
    assert "apply_immediately: choice.apply_immediately" in maintenance, \
        "the apply window is not sent as an explicit operator choice"

    # The poll must stop when the page goes away, and must never call an
    # unchanged engine version a success.
    assert "signal?.aborted" in maintenance and "signal?.addEventListener('abort'" in maintenance, \
        "the status poll is not abort-aware and would outlive the page"
    assert "POLL_INTERVAL = 10000" in maintenance and "POLL_LIMIT = 180" in maintenance, \
        "the poll cadence or its 30-minute ceiling changed silently"
    assert "live.upgraded" in maintenance, \
        "the poll reports on status instead of the engine version"
    assert "the engine version is unchanged — the upgrade did not take effect" in maintenance, \
        "a settled-but-unchanged resource is not called out"
    assert "innerHTML" not in maintenance, \
        "the Maintenance page writes markup — identifiers come from AWS"

    for reason in ("no_converged_deployment", "requires_superuser", "update_unavailable"):
        assert reason in maintenance, \
            f"the Maintenance page cannot explain the {reason} block"
    assert "routeHref('deployments')" in maintenance, \
        "the framework update never hands the operator to Deployments"


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
    settings_panels = (ASSETS / "features/settings/panels.js").read_text()
    webapps = (ASSETS / "features/webapps/page.js").read_text()
    core = (ASSETS / "core.js").read_text()
    app = (ASSETS / "app.js").read_text()
    preview = (ROOT / "bin/admin_preview_support/server.py").read_text()

    assert "result.token" not in platform and "MOJO_DEPLOY_KEY" not in platform
    assert "apiOnce" in advanced and "refresh-required" in advanced
    # The sign-in editor moved into its own drill-in panel; the list never
    # opens a modal any more.
    assert "'login.methods'" in settings_panels and "'registration.methods'" in settings_panels
    assert "name === 'password'" in settings_panels and "Save authentication settings" in settings_panels
    assert "openModal" not in settings, "the Settings list still opens a modal"
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
            "/api/dnsman/certificate/remove-failed",
            "/api/edge/upstream/declare", "/api/edge/vhost",
            "/api/edge/route"):
        assert endpoint in advanced, f"Advanced is missing {endpoint}"


@th.django_unit_test("WebApps owns URL-first onboarding, external domains, and day-2")
def test_webapp_onboarding_asset_contract(opts):
    wizard = (ASSETS / "features/webapps/wizard.js").read_text()
    page = (ASSETS / "features/webapps/page.js").read_text()
    api_side = (ASSETS / "features/webapps/api.js").read_text()
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

    # --- merged Deployments list (page.js) ---
    assert "/api/edge/webapp/summaries" in page, \
        "the merged list does not read the bounded summaries endpoint"
    assert "statusRow(" in page and "rowSection(" in page and "statusHeadline(" in page, \
        "the merged list does not build on the shared row grammar"
    assert "history.replaceState" in page and "routeHref('deployments'" in page, \
        "#/webapps does not canonicalize to #/deployments"
    assert "No address yet — not reachable" in page and "'Set address'" in page, \
        "a missing address is not the row's health story"
    assert "label: 'Created'" not in page, \
        "the redesign removed the Created column; last deploy is the date that matters"
    assert "badge(r.current_release" not in page, \
        "release identifiers are back inside green pills"
    assert ".slice(0, 10)" in page and ".slice(0, 10)" in api_side, \
        "full-length identifiers leaked onto the row surface"
    assert "innerHTML" not in page and "innerHTML" not in api_side, \
        "the Deployments lane writes markup instead of text nodes"

    # --- API section + drill-ins (api.js) ---
    assert "'/api/account/admin/platform?sections=deployments,api'" in api_side, \
        "the API section pays the full platform overview instead of the sections allowlist"
    assert "'/api/account/admin/platform/deploy/'" in api_side and "Retry same SHA" in api_side \
        and "'verify', 'Verify'" in api_side and "'converge', 'Converge'" in api_side, \
        "the API drill-in lost its same-SHA recovery controls"
    assert "'/api/account/admin/platform/framework'" in api_side \
        and "'/api/account/admin/platform/framework/update'" in api_side, \
        "the django-mojo row does not use the shared framework endpoints"
    assert "confirm_version" in api_side, \
        "the framework update lost its typed version echo"
    assert "framework_pin" in api_side and "settings_owner_edit" in api_side, \
        "the framework hold is not surfaced through the owner-tier writer"
    assert "apiOnce" in api_side, \
        "provider-bearing mutations gained a transport retry"
    for reason in ("no_converged_deployment", "requires_superuser", "update_unavailable"):
        assert reason in api_side, \
            f"the framework drill-in cannot explain the {reason} block"
    assert "capabilities?.manage === true" in api_side, \
        "API recovery controls are not gated on manage_platform"
    assert "Technical details" in api_side, \
        "full shas and attempt UUIDs are not confined to a disclosure"

    # --- unchanged platform contracts still hold ---
    assert "Add domains and manage the public records" not in platform, \
        "Platform duplicates the first-class Domains & DNS destination"
    # The evidence cards are gone entirely: raw payloads now live in the
    # Dashboard drill-in disclosures, one row at a time.
    assert "evidenceSummary" not in platform and "View raw evidence" not in platform, \
        "the dissolved Platform evidence grid came back"
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


@th.django_unit_test("every portal surface that could mutate infrastructure reads the mode")
def test_infrastructure_mode_asset_contract(opts):
    maintenance = (ASSETS / "features/platform/maintenance.js").read_text()
    api = (ASSETS / "features/webapps/api.js").read_text()
    setup = (ASSETS / "features/platform/page.js").read_text()

    # A MISSING capability is an older server, and an older server is a managed
    # install — the controls must not disappear on a payload that predates the
    # switch. Only an explicit false takes them away.
    assert "capabilities?.infrastructure_managed !== false" in maintenance, \
        "Maintenance does not treat a missing infrastructure capability as managed"
    assert "if (busy || !managed) return;" in maintenance, \
        "the Maintenance apply control has no belt-and-braces mode guard"
    assert "if (busy || !managed || !framework?.can_update) return;" in maintenance, \
        "the framework update control has no belt-and-braces mode guard"
    assert "infrastructure_external" in maintenance, \
        "Maintenance cannot explain an infrastructure_external block"

    # The Deployments-lane framework row and its drill-in both route through
    # BLOCKED_COPY, so the copy entry is the whole contract there.
    assert "infrastructure_external:" in api, \
        "the Deployments framework drill-in cannot explain external mode"
    assert "BLOCKED_COPY[framework?.blocked_reason]" in api, \
        "the framework drill-in no longer reads blocked_reason for its copy"

    # System Setup names the mode in words, from the top-level bootstrap fact.
    assert "ctx?.infrastructure?.managed !== false" in setup, \
        "System Setup does not read the published infrastructure fact"
    assert "Infrastructure: managed by this portal" in setup \
        and "Infrastructure: external" in setup, \
        "System Setup does not state which kind of installation this is"
    assert "infrastructureNote(ctx)" in setup, \
        "the System Setup mode line is never rendered"

    for source in (maintenance, api, setup):
        assert "innerHTML" not in source, \
            "an infrastructure-mode surface writes markup instead of building nodes"
