"""Built-in Admin WebApp reveal-once key lifecycle smoke coverage."""

import uuid
from pathlib import Path
from unittest import mock

from testit import helpers as th


ADMIN_EMAIL = "admin_portal_webapps@test.com"
ADMIN_PASSWORD = "Admin_portal_webapps_pw_99"
MEMBERSHIP_FREE_GROUP = "Membership-free WebApp owner"
SCOPED_EMAIL = "admin_portal_webapps_scoped@test.com"
SCOPED_PASSWORD = "Admin_portal_webapps_scoped_pw_99"
VIEWER_EMAIL = "admin_portal_webapps_viewer@test.com"
VIEWER_PASSWORD = "Admin_portal_webapps_viewer_pw_99"
SUMMARIES_DOMAIN = "admin-portal-summaries.example"
SUMMARIES_BUCKET = "edge-test-releases"


def _reset_scoped_permissions():
    """Pin the scoped fixture user's global grants to view_admin only.

    Another test in this module deliberately walks the user through global
    permission cases; calling this first makes summary-scope tests
    order-independent.
    """
    from mojo.apps.account.models import User

    user = User.objects.get(email=SCOPED_EMAIL)
    user.permissions = {"view_admin": True}
    user.save(update_fields=["permissions", "modified"])


@th.django_unit_setup()
def setup_admin_portal_webapps(opts):
    from datetime import timedelta

    from django.utils import timezone
    from mojo.apps.account.models import Group, User
    from mojo.apps.dnsman.models import Certificate, Domain
    from mojo.apps.edge.models import Vhost, WebApp

    User.objects.filter(email__in=[ADMIN_EMAIL, SCOPED_EMAIL, VIEWER_EMAIL]).delete()
    WebApp.objects.filter(slug__startswith="summaries-").delete()
    Vhost.objects.filter(domain__name=SUMMARIES_DOMAIN).delete()
    Certificate.objects.filter(domain__name=SUMMARIES_DOMAIN).delete()
    Domain.objects.filter(name=SUMMARIES_DOMAIN).delete()
    Group.objects.filter(name__startswith="WebApp authority ").delete()
    Group.objects.filter(name=MEMBERSHIP_FREE_GROUP).delete()
    user = User.objects.create_user(username=ADMIN_EMAIL, email=ADMIN_EMAIL,
                                    password=ADMIN_PASSWORD)
    user.is_active = True
    user.is_email_verified = True
    user.requires_mfa = False
    user.is_superuser = True
    user.save()
    group = Group.objects.create(name=MEMBERSHIP_FREE_GROUP, kind="organization")
    opts.membership_free_group_id = group.pk
    scoped = User.objects.create_user(
        username=SCOPED_EMAIL, email=SCOPED_EMAIL, password=SCOPED_PASSWORD)
    scoped.is_active = True
    scoped.is_email_verified = True
    scoped.requires_mfa = False
    scoped.permissions = {"view_admin": True}
    scoped.save()
    parent = Group.objects.create(
        name="WebApp authority Parent", kind="organization")
    child = Group.objects.create(
        name="WebApp authority Child", kind="organization", parent=parent)
    partial = Group.objects.create(
        name="WebApp authority Partial", kind="organization")
    dark_parent = Group.objects.create(
        name="WebApp authority Dark parent", kind="organization", is_active=False)
    dark_child = Group.objects.create(
        name="WebApp authority Dark child", kind="organization", parent=dark_parent)
    member = parent.add_member(scoped)
    member.permissions = {"manage_webapp": True, "manage_dns": True}
    member.save(update_fields=["permissions", "modified"])
    member = partial.add_member(scoped)
    member.permissions = {"manage_webapp": True}
    member.save(update_fields=["permissions", "modified"])
    member = dark_parent.add_member(scoped)
    member.permissions = {"manage_webapp": True, "manage_dns": True}
    member.save(update_fields=["permissions", "modified"])
    opts.scoped_group_ids = [child.pk, parent.pk]
    opts.partial_group_id = partial.pk
    opts.dark_group_ids = [dark_parent.pk, dark_child.pk]

    viewer = User.objects.create_user(
        username=VIEWER_EMAIL, email=VIEWER_EMAIL, password=VIEWER_PASSWORD)
    viewer.is_active = True
    viewer.is_email_verified = True
    viewer.requires_mfa = False
    viewer.permissions = {"view_admin": True}
    viewer.save()

    # Summaries fixtures: one vhost-backed app and one bare app inside the
    # scoped member's authority, one app in a group they cannot see.
    # No EDGE_RELEASE_BUCKETS write: the bucket allowlist is only read by
    # validate_web_app (mocked below), and replacing that global Setting
    # mid-run yanks other modules' declared buckets out from under edge's
    # pool convergence when test_edge runs concurrently.
    domain = Domain.objects.create(
        name=SUMMARIES_DOMAIN, group=parent, provider="godaddy",
        status="active", verified=True)
    certificate = Certificate.objects.create(
        domain=domain, common_name=f"portal.{SUMMARIES_DOMAIN}",
        sans=[SUMMARIES_DOMAIN, f"*.{SUMMARIES_DOMAIN}"], status="active",
        not_after=timezone.now() + timedelta(days=60))
    # is_enabled=False keeps this fixture out of edge's desired-state and
    # hosted-auth convergence, which iterate every ENABLED vhost fleet-wide;
    # the summaries endpoint and drill-in read the vhost/certificate FKs
    # regardless of the serving flag.
    vhost = Vhost.objects.create(
        domain=domain, certificate=certificate, label="portal", kind="site",
        is_enabled=False)
    with mock.patch("mojo.apps.edge.validators.validate_web_app"):
        fixtures = {}
        for slug, owner, linked in (
                ("summaries-green", parent, vhost),
                ("summaries-bare", child, None),
                ("summaries-foreign", group, None)):
            site = WebApp(group=owner, slug=slug, vhost=linked,
                          bucket=SUMMARIES_BUCKET, prefix="pending")
            site.save()
            site.prefix = site.storage_prefix()
            site.save()
            fixtures[slug] = site.pk
    opts.summaries_green = fixtures["summaries-green"]
    opts.summaries_bare = fixtures["summaries-bare"]
    opts.summaries_foreign = fixtures["summaries-foreign"]
    opts.summaries_hostname = vhost.server_name


@th.django_unit_test("membership-free superuser can choose every active WebApp group")
def test_membership_free_superuser_webapp_groups(opts):
    assert opts.client.login(ADMIN_EMAIL, ADMIN_PASSWORD), \
        "membership-free superuser could not authenticate"

    response = opts.client.get("/api/account/admin/bootstrap")
    data = response.json.get("data") or {}
    choices = data.get("webapp_groups") or []

    assert response.status_code == 200, \
        f"Admin bootstrap returned {response.status_code}: {response.json}"
    assert opts.membership_free_group_id in [row.get("id") for row in choices], \
        f"membership-free superuser did not receive the active group choice: {choices}"


@th.django_unit_test("scoped inherited authority enables WebApps without global grants")
def test_scoped_webapp_authority_bootstrap(opts):
    assert opts.client.login(SCOPED_EMAIL, SCOPED_PASSWORD), \
        "scoped WebApp administrator could not authenticate"
    response = opts.client.get("/api/account/admin/bootstrap")
    data = response.json.get("data") or {}
    choice_ids = [row.get("id") for row in data.get("webapp_groups") or []]

    assert response.status_code == 200, \
        f"scoped Admin bootstrap returned {response.status_code}: {response.json}"
    assert choice_ids == opts.scoped_group_ids, \
        f"scoped and inherited WebApp choices drifted: {choice_ids}"
    assert all(row.get("can_manage_dns") is True
               for row in data.get("webapp_groups") or []), \
        "eligible scoped groups did not publish their DNS-management authority"
    assert opts.partial_group_id not in choice_ids, \
        "a partial WebApp-only member grant entered the eligible choices"
    assert not set(opts.dark_group_ids).intersection(choice_ids), \
        "an inactive group chain entered the eligible choices"
    assert data.get("can_create_webapp_group") is False, \
        "scoped authority incorrectly granted global group creation"
    assert data.get("capabilities", {}).get("manage_webapps") is True, \
        "eligible scoped groups did not enable WebApp onboarding"
    assert data.get("features", {}).get("webapps", {}).get("enabled") is True, \
        "eligible scoped groups did not enable the WebApps feature"


@th.django_unit_test("WebApp draft recovery freezes replay and uses selected-group DNS authority")
def test_webapp_onboarding_browser_contract(opts):
    root = Path(__file__).resolve().parents[2]
    assets = root / "mojo/apps/account/admin_portal/assets/features/webapps"
    wizard = (assets / "wizard.js").read_text()
    page = (assets / "page.js").read_text()

    # Reload reconciles the saved operation before creating anything.
    assert "resumeWizard" in wizard and \
        "/api/edge/webapp/onboarding/detail?operation=" in wizard, \
        "reload does not reconcile a saved operation UUID before create"
    # The submitted draft is frozen: one durable UUID + payload, replayed as-is.
    assert "draft?.submitted && draft.operation_id" in wizard and \
        "body: JSON.stringify(frozenPayload)" in wizard and \
        "submitted: true, payload: frozenPayload" in wizard, \
        "an ambiguous create can mutate or rebuild its frozen replay payload"
    assert "Start over" in wizard and "clearPendingDraft()" in wizard, \
        "the frozen draft has no explicit abandonment path"
    # Domain choices depend on the selected group's DNS authority, not a global.
    assert "groupDnsAuthority" in wizard and "group?.can_manage_dns" in wizard, \
        "address choices still depend on global rather than selected-group DNS authority"
    domain = wizard[wizard.index("function domainPhase"):wizard.index("function openConnectedPicker")]
    assert "options.external_available" in domain and "canDns" in domain, \
        "the keep-my-DNS path is not gated on availability and selected-group authority"
    # The list header only launches the wizard; it does not surface a globally
    # gated Domains destination to a scoped WebApp admin.
    assert "startWizard(ctx, render)" in page and "hasPendingWizard()" in page, \
        "the WebApps list cannot launch or resume onboarding"
    # Scoped to the LIST page: its header must not offer a globally gated
    # Domains destination. The app page's needs_domain steer is a different
    # thing — the admin has just named a domain that has to be connected first,
    # and the plain reason without the way to act on it would be a dead end.
    list_page = page[page.index("export async function deploymentsPage"):]
    assert "ctx.capabilities.manage_webapps ? h('button'" in page and \
        "routeHref('domains')" not in list_page, \
        "the WebApps header exposes globally gated Domains to a scoped-only admin"


@th.django_unit_test("the onboarding wizard derives its destination and never fakes a manual record")
def test_webapp_onboarding_destination_contract(opts):
    root = Path(__file__).resolve().parents[2]
    wizard = (root / "mojo/apps/account/admin_portal/assets/features/webapps/wizard.js").read_text()

    # No internal setting name leaks into the wizard copy or logic.
    assert "EDGE_WEBAPP_CNAME_TARGET" not in wizard, \
        "the wizard references an internal setting name"
    # The silent choice-swallowing catches are gone; refusals are classified.
    assert "catch (_) { /* the run panel keeps an explicit continuation available */ }" not in wizard and \
        "catch (_) { /* surfaced by the run panel from the operation state */ }" not in wizard, \
        "the wizard still swallows choice-submission errors silently"
    assert "submitChoiceRecovering" in wizard and "isWaitStateError" in wizard, \
        "the wizard has no wait-state-aware choice submission"
    # The address auto-submit is a run-panel state-machine transition keyed on
    # the fetched cursor, not on the create-response cursor (the dead gate).
    assert "function pendingAutoAddress" in wizard and "maybeAutoAdvance" in wizard, \
        "the resolved-domain address choice is not auto-submitted from the run loop"
    create = wizard[wizard.index("async function createOperation"):wizard.index("async function submitChoice")]
    assert "operation.cursor === 'address'" not in create, \
        "the address choice is still (dead-)gated on the create-response cursor"
    # Records render from server evidence; the precheck fallback is external-only
    # and complete-only. A managed domain never renders a manual records panel.
    assert "state.resolved?.provider === 'mojo'" in wizard and "r.type && r.name && r.value" in wizard, \
        "the records fallback is not restricted to complete external-domain records"
    # A configuration-required verdict steers to System Setup.
    assert "configuration_required" in wizard and "routeHref('setup')" in wizard, \
        "the wizard does not route an unserveable installation to System Setup"
    # The identity step is gated on the resolved destination, covering the
    # connected-domain and purchase paths that never see a precheck verdict.
    assert "data.destination" in wizard and "destination_error" in wizard, \
        "the identity step is not gated on the installation's serving destination"
    # Managed-domain copy tells the operator what happens automatically.
    assert "automatically" in wizard and "HTTPS" in wizard and "deploy" in wizard, \
        "the managed-domain copy does not explain the automatic add / HTTPS / deploy"


@th.django_unit_test("an addressless WebApp row reaches management (delete), not a delete-less wizard jump")
def test_addressless_webapp_row_reaches_management(opts):
    root = Path(__file__).resolve().parents[2]
    page = (root / "mojo/apps/account/admin_portal/assets/features/webapps/page.js").read_text()

    # Every list row opens the app's own page — its Danger tab still carries
    # Delete. An addressless app (a half-finished onboarding) used to route
    # straight to the address wizard (onSetAddress), trapping it with no way to
    # delete it; now the row also says plainly what happened and keeps both
    # ways out inline: finish setup, or delete.
    assert "onSetAddress" not in page, \
        "an addressless WebApp row still routes to the wizard instead of management"
    window = page[page.index("function webappRow"):page.index("export async function deploymentsPage")]
    assert "Setup never finished — not reachable" in window \
        and "routeHref('deployments', {webapp: app.id})" in window, \
        "the addressless WebApp row does not say so plainly or open the app page"
    assert "'Finish setup'" in window and "changeAddressFor(ctx, app, reload)" in window \
        and "deleteWebApp(app, reload)" in window, \
        "the addressless row lost its inline finish-setup and delete actions"
    # Setup stays one click away: the Overview tab offers Set address for an
    # addressless app, so reaching the app page doesn't hide the common action.
    overview = page[page.index("section === 'overview'"):page.index("section === 'deploys'")]
    assert "!address.hostname && manage" in overview and "changeAddressFor" in overview \
        and "Set address" in overview, \
        "the Overview tab does not offer Set address for an addressless app"


@th.django_unit_test("a new WebApp is created from one name on the workspace apps domain")
def test_name_only_creation_contract(opts):
    root = Path(__file__).resolve().parents[2]
    wizard = (root / "mojo/apps/account/admin_portal/assets/features/webapps/wizard.js").read_text()

    # The new-app flow enters at the one-input name phase; the address-first
    # machinery survives only for adopt (change address / repair) and for the
    # no-eligible-workspace case, never as the creation entry.
    assert "function namePhase" in wizard and \
        "phase: adopt ? 'address' : newAppEntry" in wizard, \
        "the new-app flow does not enter at the name-only phase"
    assert "function addressPhase" in wizard and "startChangeAddress" in wizard \
        and "resumeWizard" in wizard, \
        "the repair and change-address flows lost their address-phase machinery"
    name_phase = wizard[wizard.index("function namePhase"):wizard.index("function addressPhase")]
    # No web-address input in the creation flow: the address is composed from
    # the workspace's apps domain, previewed live from the slugified name.
    assert "myapp.example.com" not in name_phase, \
        "the name-only creation phase still asks for a web address"
    assert "${slug}.${appsDomain().name}" in name_phase \
        and "${currentSlug() || 'your-app'}.${active.name}" in name_phase, \
        "the address preview is not composed from the apps domain"
    # Availability is a debounced precheck of the composed hostname, reusing
    # the precheck verdicts (ready / taken / invalid...), and Create is gated
    # on a passing check.
    assert "setTimeout(checkAvailability, 350)" in name_phase \
        and "/api/edge/webapp/onboarding/precheck?group=" in name_phase, \
        "the composed hostname is not prechecked as the name is typed"
    assert "kind === 'ready' || kind === 'records_needed'" in name_phase \
        and "kind === 'taken'" in name_phase and "available = true" in name_phase, \
        "the availability line does not reuse the precheck verdicts"
    assert "available && (state.options?.buckets || []).length" in name_phase, \
        "Create is not gated on a passing availability check"
    # Workspace and environment live under Advanced; the workspace select is
    # hidden outright when there is only one choice, and defaults to the
    # last-used workspace (options are per group, so changing it re-fetches).
    assert "wizard-advanced" in name_phase and "field('Workspace', group" in name_phase \
        and "field('Environment', environment" in name_phase \
        and "groups.length > 1 ?" in name_phase, \
        "workspace and environment did not move under the Advanced disclosure"
    assert "function defaultGroup" in wizard and "LAST_GROUP_KEY" in wizard, \
        "the workspace default does not remember the last-used group"
    assert "state.groupId = group.value; rememberGroup(group.value);" in name_phase \
        and "loadOptions();" in name_phase, \
        "changing the workspace does not re-fetch its options and address suffix"
    # No apps domain -> the plain-language reason plus links to the two places
    # that fix it; Create stays disabled (the ready panel is never painted).
    assert "apps_domain_error" in name_phase and "routeHref('setup')" in name_phase \
        and "routeHref('domains')" in name_phase, \
        "a workspace without an apps domain is not steered to Setup and Domains"
    # The create payload keeps its shape: group + slug + display_name from the
    # name + environment, through the same frozen createOperation path.
    assert "display_name: name.value.trim(), slug," in name_phase \
        and "environment: environment.value, bucket: bucket.value" in name_phase \
        and "await createOperation(state, identity, render)" in name_phase, \
        "name-only creation does not reuse the frozen createOperation payload"


@th.django_unit_test("the run panel drives github-skip and verify itself, keyed on the fetched step")
def test_wizard_auto_run_contract(opts):
    root = Path(__file__).resolve().parents[2]
    wizard = (root / "mojo/apps/account/admin_portal/assets/features/webapps/wizard.js").read_text()

    # Both auto-submits are run-loop transitions keyed on the FETCHED cursor
    # with a recorded-choice check and a once-guard; the backend only retries a
    # step whose choice is recorded, so without them the setup parks forever.
    github = wizard[wizard.index("function pendingAutoGithubSkip"):wizard.index("function pendingAutoVerify")]
    assert "op.cursor === 'github'" in github and "!(op.choices || {}).github" in github \
        and "githubSkipSubmitted" in github and "github_repository" in github, \
        "the github skip is not keyed on the fetched cursor with a once-guard"
    verify = wizard[wizard.index("function pendingAutoVerify"):wizard.index("async function maybeAutoAdvance")]
    assert "op.cursor === 'verify'" in verify and "!(op.choices || {}).verify" in verify \
        and "verifySubmitted" in verify, \
        "the verify submit is not keyed on the fetched cursor with a once-guard"
    advance = wizard[wizard.index("async function maybeAutoAdvance"):wizard.index("function isAdvancing")]
    assert "submitChoiceRecovering(state, 'github', {skip: true})" in advance \
        and "submitChoiceRecovering(state, 'verify', {})" in advance, \
        "the auto choices do not classify wait-state refusals like the address one"
    assert "|| pendingAutoGithubSkip(state) || pendingAutoVerify(state)" in wizard, \
        "polling stops before the pending auto choices have been recorded"
    # Success hands off to the app page's Set up deploys tab, not an inspector.
    done = wizard[wizard.index("function donePanel"):wizard.index("function failedPanel")]
    assert "routeHref('deployments', {webapp: app, tab: 'setup'})" in done, \
        "the done panel does not open the app page's Set up deploys tab"
    assert "inspector" not in done, \
        "the done panel still routes to the retired inspector drawer"


@th.django_unit_test("each WebApp has its own page with a promoted Set up deploys tab")
def test_webapp_detail_page_contract(opts):
    root = Path(__file__).resolve().parents[2]
    page = (root / "mojo/apps/account/admin_portal/assets/features/webapps/page.js").read_text()

    # ?webapp=<id> renders a full page, not a drawer over the list; legacy
    # numeric ?inspector= deep links redirect to it.
    assert "async function webappDetailPage" in page \
        and "return webappDetailPage(ctx, linkedWebapp" in page, \
        "?webapp= does not open a full app page"
    assert r"/^\d+$/.test(String(linkedState.inspector" in page \
        and "routeHref('deployments', {webapp: linkedWebapp" in page, \
        "legacy numeric ?inspector= deep links do not redirect to the app page"
    # The five tabs re-home the existing sections; Setup is promoted to
    # "Set up deploys" with three honest sub-tabs.
    assert "['setup', 'Set up deploys']" in page and "manageSection(ctx, app, summary, id, body, reload)" in page, \
        "the app page does not re-home the existing management sections"
    assert "'GitHub Actions'" in page and "'Upload a build'" in page \
        and "'Any other CI / API'" in page, \
        "the Set up deploys tab lost one of its three ways to deploy"
    assert "Create key — shown once" in page and "keyDialog(app, reload)" in page, \
        "the GitHub Actions steps lost the inline reveal-once key creation"
    assert "workflowPanel(app, keyButton())" in page, \
        "the GitHub Actions steps do not reuse the generated workflow panel"
    # The upload way is a real uploader now, not the Phase-2 placeholder.
    assert "landing here shortly" not in page, \
        "the upload tab still carries its placeholder copy"
    assert "uploadPanel(ctx, app, summary, reload)" in page, \
        "the upload sub-tab does not render the real uploader"
    # The list page stays, and every row opens the app page by link.
    assert "startWizard(ctx, render)" in page \
        and "action: {label: 'Open', href: openHref}" in page, \
        "the deployments list no longer links each app to its own page"


@th.django_unit_test("the upload tab really uploads: picker, drop zone, hashing, honest limits and failure copy")
def test_webapp_upload_tab_contract(opts):
    root = Path(__file__).resolve().parents[2]
    page = (root / "mojo/apps/account/admin_portal/assets/features/webapps/page.js").read_text()

    upload = page[page.index("function uploadPanel"):page.index("const SETUP_WAYS")]
    # A folder picker (with a plain multi-file fallback) AND a drag-drop zone.
    assert "webkitdirectory: true" in upload and "type: 'file', multiple: true" in upload, \
        "the uploader lost its folder picker or its plain-files fallback"
    assert "upload-drop" in upload and "ondrop" in upload and "ondragover" in upload, \
        "the uploader lost its drag-drop zone"
    # The manifest is built client-side: relative paths, sha256 via
    # crypto.subtle, size — and each PUT sends the server's exact headers.
    assert "crypto.subtle.digest('SHA-256'" in page and "webkitRelativePath" in page, \
        "the manifest is not hashed and path-relativized in the browser"
    assert "manifest.push({path: row.path, sha256: await sha256Hex(row.file), size: row.file.size})" in upload, \
        "the register manifest lost its path/sha256/size shape"
    assert "headers: upload.headers || {}" in upload, \
        "PUTs do not send the exact headers the signed URL was bound to"
    assert "x-amz-checksum-sha256" in page, \
        "the uploader no longer names the checksum header binding"
    # The three calls, in order, then the deployment poll.
    assert "api('/api/edge/release', {" in upload and "api('/api/edge/release/complete', {" in upload \
        and "/api/edge/release/deployment/" in upload, \
        "the uploader does not make the register/complete/status calls"
    assert "panel.isConnected" in upload and "setTimeout(step, DEPLOY_POLL_MS)" in upload, \
        "deployment polling does not stop when the panel is closed"
    # A blocked PUT (network/CORS-shaped) says what to run, and links Setup.
    blocked = ("The browser was blocked from uploading directly to storage — "
               "run the storage checkup in System Setup (bucket sharing rules), "
               "then try again.")
    assert blocked in page, \
        "the storage-blocked failure copy drifted from its contract"
    assert "storageBlocked" in upload and "error?.name === 'TypeError'" in upload \
        and "routeHref('setup')" in upload, \
        "a blocked upload does not route the admin to System Setup"
    # Cap honesty: the server's own numbers, refused before registering.
    assert "{files: 5000, bytes: 1073741824}" in page, \
        "the client caps drifted from the server's EDGE_RELEASE_* defaults"
    assert "Too big to deploy: the server accepts at most" in upload \
        and "per release." in upload, \
        "the caps refusal does not cite the server's limits plainly"


@th.django_unit_test("the reveal-once deploy-key modal scrolls its body instead of clipping the command")
def test_one_time_secret_modal_is_not_clipped(opts):
    root = Path(__file__).resolve().parents[2]
    assets = root / "mojo/apps/account/admin_portal/assets"
    page = (assets / "features/webapps/page.js").read_text()
    styles = (assets / "admin.css").read_text()

    secret = page[page.index("function oneTimeSecret"):page.index("function keyDialog")]
    # One opaque token does not need four rows of vertical space; the .secret
    # word-break keeps it readable in two.
    assert "rows: '4'" not in secret, \
        "the one-time secret still reserves four rows for a single opaque token"
    assert "class: 'secret', readonly: true, rows: '2'" in secret, \
        "the secret field lost its compact two-row shape"
    # Reveal-once behavior and copy are untouched.
    assert "secretField.value = ''" in secret and "result.token = null" in secret \
        and "secret = ''" in secret, \
        "the reveal-once scrub changed"
    assert "Copy this value now" in secret \
        and "gh secret set MOJO_DEPLOY_KEY --repo YOUR_ORG/YOUR_REPO" in secret, \
        "the modal copy or its gh command changed"

    # The modal is a column that never scrolls itself: the header is pinned and
    # the body owns the scrolling, so a tall body is reachable rather than clipped.
    modal_rule = styles.split(".modal{", 1)[1].split("}", 1)[0]
    for declaration in ("display:flex", "flex-direction:column", "overflow:hidden",
                        "max-height:min(780px,calc(100vh - 36px))"):
        assert declaration in modal_rule, \
            f"the modal shell is missing {declaration!r} — it is still its own scroll container"
    assert "overflow:auto" not in modal_rule, \
        "the whole modal, header included, still scrolls"
    assert ".modal>header{flex:0 0 auto;" in styles, \
        "the modal header is not pinned against a scrolling body"
    body_rule = styles.split(".modal-body{", 1)[1].split("}", 1)[0]
    for declaration in ("flex:1 1 auto", "min-height:0", "overflow:auto"):
        assert declaration in body_rule, \
            f"the modal body is missing {declaration!r} and cannot shrink and scroll"

    # The gh command wraps instead of hiding itself behind a sideways scroll.
    command_rule = styles.split(".command{", 1)[1].split("}", 1)[0]
    assert "white-space:pre-wrap" in command_rule and "word-break:break-all" in command_rule, \
        "the gh command block does not wrap"
    assert "overflow:auto" not in command_rule, \
        "the gh command block still scrolls sideways"

    # The narrow-viewport bottom sheet keeps its own sizing.
    assert ".modal,.modal.wide{width:100%;max-height:92vh;border-radius:13px 13px 0 0}" in styles, \
        "the narrow-viewport bottom-sheet modal lost its sizing"
    assert ".modal.wide{width:min(760px,100%)}" in styles, \
        "the wide modal lost its width"


@th.django_unit_test("the app page lists every address and adds a custom one from server evidence")
def test_webapp_addresses_card_and_add_domain_dialog(opts):
    root = Path(__file__).resolve().parents[2]
    page = (root / "mojo/apps/account/admin_portal/assets/features/webapps/page.js").read_text()

    # The Overview tab carries an Addresses card, fed by the address list.
    overview = page[page.index("section === 'overview'"):page.index("section === 'deploys'")]
    assert "addressesCard(ctx, app, reload)" in overview, \
        "the Overview tab does not render the Addresses card"
    card = page[page.index("function addressesCard"):page.index("function addAddressDialog")]
    assert "/api/edge/webapp/aliases?webapp=" in card, \
        "the Addresses card does not read the app's addresses from the server"
    assert "'Addresses'" in card and "'Add a custom domain'" in card \
        and "addAddressDialog(app, reload)" in card, \
        "the Addresses card lost its heading or its add-an-address action"
    assert "row.role === 'primary'" in card and "row.role === 'alias'" in card, \
        "the card does not distinguish the app's own address from an added one"
    assert "certBadge(row.certificate)" in card and "removeAddress(app, row, reload)" in card, \
        "an address row lost its live/certificate state or its Remove action"
    remove = page[page.index("function removeAddress"):page.index("function addressesCard")]
    assert "confirmAction({title: `Remove ${row.hostname}?`" in remove \
        and "'/api/edge/webapp/detach_domain'" in remove and "await reload();" in remove, \
        "Remove does not confirm, detach and reload"

    dialog = page[page.index("function addAddressDialog"):page.index("async function manageSection")]
    # Every branch is keyed on the SERVER's status; nothing is detected here.
    for status in ("attached", "needs_domain", "records_needed",
                   "certificate_pending", "certificate_failed"):
        assert f"'{status}'" in dialog, \
            f"the add-an-address dialog cannot render a {status} result"
    assert "result.dns === 'managed'" in dialog and \
        "this domain’s DNS is managed here — records and HTTPS are handled for you" in dialog, \
        "the managed-domain confirmation is missing or is fabricated client-side"
    assert "done = true" in dialog and "onClose: () => { if (done) reload(); }" in dialog, \
        "a successful add does not close and reload the page"
    # A plain Check re-runs the same call; only Try again asks for a new
    # certificate order. bool('false') would have made every check a retry.
    assert "onclick: () => run(false)" in dialog and "onclick: () => run(true)" in dialog, \
        "the dialog lost its Check or its explicit certificate retry"
    assert "if (retryCertificate) payload.retry_certificate = true;" in dialog, \
        "retry_certificate is not sent only for the explicit repair"
    check = dialog[dialog.index("const checkButton"):dialog.index("function paint")]
    assert "retry_certificate" not in check, \
        "a plain Check would re-request the certificate"
    # needs_domain steers to Domains, with no records to publish.
    steer = dialog[dialog.index("'needs_domain'"):dialog.index("'records_needed'")]
    assert "routeHref('domains')" in steer and "recordsTable" not in steer, \
        "needs_domain does not steer to Domains, or invents records to publish"
    # The records themselves: Type / Name / Value, each copyable, verbatim.
    records = page[page.index("function recordsTable"):page.index("function certBadge")]
    for label in ("'Type'", "'Name'", "'Value'"):
        assert label in records, f"the records table lost its {label} column"
    assert records.count("cell(text(record.") == 3 and "copyButton(value)" in records, \
        "the records table does not render each value verbatim with a Copy button"
    # Copy rules: addresses, never plumbing.
    assert "vhost" not in dialog.lower(), \
        "the add-an-address dialog leaks the internal address record name"
    assert "Add address" in dialog and "an address you already own" in dialog, \
        "the dialog does not talk about addresses in plain words"


@th.django_unit_test("WebApp list rows state their health plainly and the copy carries no plumbing words")
def test_webapp_row_copy_and_jargon_gate(opts):
    import re

    root = Path(__file__).resolve().parents[2]
    assets = root / "mojo/apps/account/admin_portal/assets/features/webapps"
    wizard = (assets / "wizard.js").read_text()
    page = (assets / "page.js").read_text()

    row = page[page.index("function webappRow"):page.index("export async function deploymentsPage")]
    # Live on the placeholder page only: honest state plus a way forward that
    # lands on the app page's Set up deploys tab.
    assert "live with a welcome page — nothing deployed yet" in row \
        and "'Deploy something'" in row and "tab: 'setup'" in row, \
        "a placeholder-only app does not say so or offer Deploy something"
    # The stale push-to-branch promise is gone (deploys may not use GitHub).
    assert "push to" not in row, \
        "the placeholder row still promises a GitHub push flow"
    # No plumbing vocabulary in any user-facing literal of either file: scan
    # every quoted/template literal that reads as copy (contains a space) for
    # the words a person should never see.
    for name, src in (("wizard.js", wizard), ("page.js", page)):
        literals = [lit for lit in
                    re.findall(r"'([^'\n]*)'", src) + re.findall(r"`([^`\n]*)`", src)
                    if " " in lit and "/api/" not in lit]
        offenders = sorted({lit for lit in literals
                            for word in ("vhost", "cursor", "operation")
                            if word in lit.lower()})
        assert not offenders, \
            f"{name} user-facing copy leaks plumbing words: {offenders}"

@th.django_unit_test("the Deployments API surface explains failures before offering controls")
def test_deployments_failure_surface_browser_contract(opts):
    root = Path(__file__).resolve().parents[2]
    assets = root / "mojo/apps/account/admin_portal/assets/features/webapps"
    api_side = (assets / "api.js").read_text()
    page = (assets / "page.js").read_text()

    # The manage label is History — 'Deploy' promised a new-commit control
    # that does not exist (Admin only ever retries the same SHA).
    assert "manage ? 'History' : 'Open'" in api_side, \
        "the API service row lost its History/Open label split"
    assert "? 'Deploy'" not in api_side and '"Deploy"' not in api_side, \
        "the misleading Deploy label is back on the API service row"

    # The failure explanation comes from the durable diagnosis journal (never
    # node_summary.failed), with the transition-detail fallback for rows
    # written before the journal existed — and it renders BEFORE the buttons.
    assert "latestDiagnosis" in api_side and "failureStory" in api_side, \
        "the failure story is not built from the diagnosis journal"
    view = api_side[api_side.index("function attemptView"):
                    api_side.index("export function openApiInspector")]
    assert view.index("failurePanel(row)") < view.index("attemptActions("), \
        "the attempt view offers controls before explaining the failure"
    story = api_side[api_side.index("function failureStory"):
                     api_side.index("function rollbackTargetLine")]
    assert "row.transitions" in story and "row.detail" in story, \
        "a pre-diagnosis row has no transition-detail fallback"
    for line in ("rollback_failed", "rollback_impossible", "rolled_back",
                 "Rollback unconfirmed"):
        assert line in api_side, f"the rollback outcome cannot say {line!r}"
    assert "remains on the same commit" in api_side, \
        "a SAME_RELEASE failure would render a self-rollback as a real one"

    # Controls are state-gated: no unconditional three-button map, nothing
    # offered on an in-flight attempt.
    assert "ACTIONS_BY_STATUS" in api_side, \
        "attempt controls are no longer state-gated"
    assert "DEPLOY_ACTIONS.map" not in api_side, \
        "the unconditional three-button map is back"
    gates = api_side[api_side.index("const ACTIONS_BY_STATUS"):
                     api_side.index("const ACTIVE_STATUSES")]
    for absent in ("requested", "canary", "fleet", "superseded"):
        assert f"{absent}:" not in gates, \
            f"an attempt in {absent!r} must offer no recovery controls"

    # The row and drill-in consume the additive payload facts.
    assert "currently_serving" in api_side and "node_summary" in api_side, \
        "the surface ignores currently_serving/node_summary"
    assert "provenCounts" not in api_side, \
        "the client still re-derives proof counts from raw evidence"
    assert "Show ${older.length} older attempt" in api_side, \
        "older attempts are not collapsed behind a disclosure"
    assert "coordination.sha" in api_side, \
        "the coordination line does not say which sha holds the lease"

    # The list polls while a deploy is in flight: bounded, hash-guarded, and
    # cleared on abort and on every reload.
    assert "pollTicks >= 36" in api_side + page and "10000" in page, \
        "the deploy poll lost its tick cap or cadence"
    assert "location.hash.startsWith('#/deployments')" in page, \
        "the poll would keep running after the operator navigates away"
    assert "signal?.addEventListener('abort', clearPoll)" in page, \
        "the poll is not cleared when the page is torn down"
    assert page.index("clearPoll();") < page.index("const reads = []"), \
        "a reload does not clear the pending poll timer first"


@th.django_unit_test("literal admin and partial globals do not grant backend WebApp authority")
def test_webapp_authority_rejects_frontend_admin_and_partial_grants(opts):
    from types import SimpleNamespace

    from mojo.apps.account.models import User
    from mojo.apps.account.services import webapp_authority

    user = User.objects.get(email=SCOPED_EMAIL)
    cases = (
        ({"admin": True}, False, False),
        ({"manage_webapp": True}, False, False),
        ({"manage_dns": True, "manage_groups": True}, False, False),
        ({"manage_webapp": True, "manage_dns": True}, True, False),
        ({"manage_webapp": True, "manage_dns": True,
          "manage_groups": True}, True, True),
        ({"security": True, "groups": True}, True, True),
    )
    for permissions, can_manage, can_create in cases:
        user.permissions = permissions
        user.save(update_fields=["permissions", "modified"])
        assert webapp_authority.has_global_webapp_authority(user) is can_manage, \
            f"global WebApp authority was wrong for {permissions}"
        assert webapp_authority.can_create_webapp_group(user) is can_create, \
            f"new-group entitlement was wrong for {permissions}"

    machine = SimpleNamespace(
        user=user, api_key=object(), group_token=None, is_authenticated=True)
    assert webapp_authority.is_interactive_request(machine) is False, \
        "an override-user API key session passed the interactive authority gate"


@th.django_unit_test("webapp summaries mirror the REST list scope and serve the slim row shape")
def test_webapp_summaries_scope_and_shape(opts):
    _reset_scoped_permissions()
    assert opts.client.login(SCOPED_EMAIL, SCOPED_PASSWORD), \
        "scoped WebApp administrator could not authenticate"
    try:
        response = opts.client.get("/api/edge/webapp/summaries")
        assert response.status_code == 200, \
            f"scoped summaries read failed: {response.json}"
        data = response.json.get("data") or {}
        assert data.get("schema_version") == 1, \
            f"summaries lost their schema version: {data.get('schema_version')}"
        assert data.get("limit") == 50 and data.get("truncated") is False, \
            f"summaries envelope bounds are wrong: {data}"
        items = data.get("items") or []
        assert data.get("count") == len(items), \
            "the summaries count does not match its items"
        ids = {row["webapp"]["id"] for row in items}
        listed = opts.client.get("/api/edge/webapp?size=50")
        assert listed.status_code == 200, \
            f"the scoped REST list read failed: {listed.json}"
        rows = listed.json.get("data") or listed.json.get("results") or []
        assert ids == {row["id"] for row in rows}, \
            "summaries and the REST list disagree about which apps this caller may see"
        assert opts.summaries_foreign not in ids, \
            "a scoped caller received another tenant's app"

        green = next(row for row in items
                     if row["webapp"]["id"] == opts.summaries_green)
        assert green["address"]["hostname"] == opts.summaries_hostname, \
            f"the vhost-backed app lost its address: {green['address']}"
        assert green["address"]["certificate"]["status"] == "active", \
            f"the vhost-backed app lost its certificate state: {green['address']}"
        assert green["address"]["certificate"]["not_after"], \
            "the certificate expiry did not serialize"
        assert green["webapp"]["slug"] == "summaries-green", \
            "the slim webapp identity block is wrong"
        bare = next(row for row in items
                    if row["webapp"]["id"] == opts.summaries_bare)
        assert bare["address"] is None, \
            "an addressless app must publish no address"
        assert bare["current_release"] is None and bare["latest_deployment"] is None, \
            "an app with no deploys invented release facts"
        for key in ("deployment_key", "onboarding"):
            assert key not in green, \
                f"the slim projection grew {key!r}, which belongs to the drill-in summary"
    finally:
        opts.client.logout()

    assert opts.client.login(ADMIN_EMAIL, ADMIN_PASSWORD), \
        "superuser could not authenticate for the cross-group read"
    try:
        for group_id, expected in (
                (opts.scoped_group_ids[1], opts.summaries_green),
                (opts.membership_free_group_id, opts.summaries_foreign)):
            scoped = opts.client.get(f"/api/edge/webapp/summaries?group={group_id}")
            assert scoped.status_code == 200, \
                f"superuser summaries read failed for group {group_id}: {scoped.json}"
            got = {row["webapp"]["id"]
                   for row in (scoped.json.get("data") or {}).get("items") or []}
            assert expected in got, \
                f"the superuser could not read group {group_id}'s rows: {got}"
    finally:
        opts.client.logout()


@th.django_unit_test("a caller-supplied group always intersects webapp summaries")
def test_webapp_summaries_group_param_scoped(opts):
    _reset_scoped_permissions()
    assert opts.client.login(SCOPED_EMAIL, SCOPED_PASSWORD), \
        "scoped WebApp administrator could not authenticate"
    try:
        parent_id = opts.scoped_group_ids[1]
        own = opts.client.get(f"/api/edge/webapp/summaries?group={parent_id}")
        assert own.status_code == 200, \
            f"a member-scoped group read failed: {own.json}"
        ids = {row["webapp"]["id"]
               for row in (own.json.get("data") or {}).get("items") or []}
        assert ids == {opts.summaries_green}, \
            f"the group intersection did not confine rows to the named tenant: {ids}"

        foreign = opts.client.get(
            f"/api/edge/webapp/summaries?group={opts.membership_free_group_id}")
        assert foreign.status_code == 200, \
            f"a foreign group id must not change the response shape: {foreign.json}"
        leaked = (foreign.json.get("data") or {}).get("items")
        assert leaked == [], \
            f"a member-scoped caller read another tenant's rows via ?group=: {leaked}"
    finally:
        opts.client.logout()


@th.django_unit_test("webapp summaries refuse callers without webapp visibility")
def test_webapp_summaries_requires_authority(opts):
    from mojo.apps.edge.rest import webapp_onboarding as views

    assert getattr(views.on_webapp_summaries,
                   "_mojo_denies_key_backed_session", False), \
        "the summaries endpoint accepts key-backed sessions"
    assert opts.client.login(VIEWER_EMAIL, VIEWER_PASSWORD), \
        "the view_admin-only fixture could not authenticate"
    try:
        refused = opts.client.get("/api/edge/webapp/summaries")
        assert refused.status_code in (401, 403), \
            f"view_admin alone read the fleet's webapps: {refused.json}"
    finally:
        opts.client.logout()


@th.django_unit_test("authenticated portal covers missing active rotated and revoked deploy keys")
def test_webapp_key_portal_smoke(opts):
    from mojo.apps.account.models import ApiKey, Group, Setting
    from mojo.apps.edge.models import WebApp

    group_name = "admin_portal_key_lifecycle"
    Group.objects.filter(name=group_name).delete()
    old_buckets, had_old_buckets = Setting.get_from_db("EDGE_RELEASE_BUCKETS")
    Setting.set("EDGE_RELEASE_BUCKETS", ["portal-test"], group=None)
    group = Group.objects.create(name=group_name, kind="organization")
    site = WebApp(group=group, slug="portal-key-smoke", bucket="portal-test", prefix="pending")
    try:
        with mock.patch("mojo.apps.edge.validators.validate_web_app"):
            site.save(); site.prefix = site.storage_prefix(); site.save()
        assert opts.client.login(ADMIN_EMAIL, ADMIN_PASSWORD)
        missing = opts.client.get(f"/api/edge/webapp/key_status?webapp={site.pk}")
        missing_data = (missing.json.get("data") or {}).get("status") or {}
        assert missing.status_code == 200 and missing_data.get("linked") is False
        assert "token" not in missing_data
        minted = opts.client.post("/api/edge/webapp/link_key", json={
            "webapp": site.pk, "action": "mint", "operation_id": str(uuid.uuid4())})
        assert minted.status_code == 200 and (minted.json.get("data") or {}).get("token")
        active = (opts.client.get(f"/api/edge/webapp/key_status?webapp={site.pk}").json.get("data") or {}).get("status") or {}
        assert active.get("linked") is True and active.get("active") is True and "token" not in active
        first_key = active.get("api_key")
        rotated = opts.client.post("/api/edge/webapp/link_key", json={
            "webapp": site.pk, "action": "rotate", "operation_id": str(uuid.uuid4())})
        assert rotated.status_code == 200
        rotated_status = (opts.client.get(f"/api/edge/webapp/key_status?webapp={site.pk}").json.get("data") or {}).get("status") or {}
        assert rotated_status.get("last_action") == "rotate" and rotated_status.get("api_key") != first_key
        assert "token" not in rotated_status
        revoked = opts.client.post("/api/edge/webapp/revoke_key", json={
            "webapp": site.pk, "operation_id": str(uuid.uuid4())})
        assert revoked.status_code == 200
        revoked_status = (opts.client.get(f"/api/edge/webapp/key_status?webapp={site.pk}").json.get("data") or {}).get("status") or {}
        assert revoked_status.get("linked") is False and revoked_status.get("last_action") == "revoke"
        assert "token" not in revoked_status
    finally:
        with mock.patch("mojo.apps.edge.validators.validate_web_app"):
            WebApp.objects.filter(pk=site.pk).delete()
        ApiKey.objects.filter(name="webapp:portal-key-smoke").delete()
        Group.objects.filter(pk=group.pk).delete()
        if had_old_buckets: Setting.set("EDGE_RELEASE_BUCKETS", old_buckets, group=None)
        else: Setting.remove("EDGE_RELEASE_BUCKETS", group=None)
