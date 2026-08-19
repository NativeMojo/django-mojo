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
    assert "ctx.capabilities.manage_webapps ? h('button'" in page and \
        "routeHref('domains')" not in page, \
        "the WebApps header exposes globally gated Domains to a scoped-only admin"


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
