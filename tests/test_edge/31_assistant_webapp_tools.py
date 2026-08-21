"""Assistant `webapp` domain: registration, authority, and read projections.

Lives in `tests/test_edge` rather than `tests/test_assistant` because it needs
the edge ORM fixtures, and `declare_pools` / `declare_release_buckets` write
`EDGE_*` settings — a protected prefix the isolation scanner refuses in a
parallel default-tier package. This package is already `serial: True` and
already owns those fixtures.
"""

import uuid

from testit import helpers as th

from tests.test_edge._helpers import (
    cleanup, declare_pools, declare_release_buckets, make_certificate,
    make_domain, make_group, make_group_member, make_release, make_upstream,
    make_user, make_vhost, make_webapp,
)


WEBAPP_TOOLS = 26
MUTATING_TOOLS = 14


def _registry():
    from mojo.apps.assistant import get_registry

    return {name: entry for name, entry in get_registry().items()
            if entry["domain"] == "webapp"}


def _call(name, params, user):
    from mojo.apps.assistant import get_registry

    return get_registry()[name]["handler"](params, user)


def _preview(name, params, user):
    from mojo.apps.assistant import get_registry

    return get_registry()[name]["preview"](params, user)


def _grant(user, perms):
    user.add_permission(list(perms))
    user.save()
    return user


@th.django_unit_setup()
def setup_assistant_webapp(opts):
    cleanup()
    declare_pools()
    declare_release_buckets()

    opts.group = make_group("edge-astools")
    opts.domain = make_domain(group=opts.group)
    opts.certificate = make_certificate(opts.domain)
    opts.vhost = make_vhost(opts.domain, opts.certificate, label="www",
                            kind="site_api")
    opts.webapp = make_webapp(opts.group, slug=f"as{uuid.uuid4().hex[:6]}",
                              vhost=opts.vhost)
    opts.release = make_release(opts.webapp, "v1", status="uploaded")
    opts.upstream = make_upstream(group=opts.group)

    # A sibling tenant the fixtures above must never reach.
    opts.other_group = make_group("edge-asother")
    opts.other_domain = make_domain(group=opts.other_group)
    opts.other_webapp = make_webapp(
        opts.other_group, slug=f"ot{uuid.uuid4().hex[:6]}")

    # The group-scoped manager: no global WebApp grant at all.
    opts.manager, _, _, _ = make_group_member(
        ["manage_webapp", "manage_dns"], group=opts.group)
    _grant(opts.manager, ["view_admin"])

    # A viewer OF THIS APP who still manages apps somewhere else, which is what
    # makes them reachable by the domain at all.
    opts.viewer, _, _, _ = make_group_member(["view_dns"], group=opts.group)
    opts.viewer_home = make_group("edge-asviewerhome")
    from mojo.apps.account.models import GroupMember
    member, _ = GroupMember.objects.get_or_create(
        user=opts.viewer, group=opts.viewer_home)
    member.permissions = {"manage_webapp": True, "manage_dns": True}
    member.save()
    _grant(opts.viewer, ["view_admin"])

    # Group `security` alone: onboarding says yes, every day-2 write says no.
    opts.security_member, _, _, _ = make_group_member(
        ["security"], group=opts.group)
    _grant(opts.security_member, ["view_admin"])

    # Global view_admin and nothing else — the domain must not exist for them.
    opts.stranger, _, _ = make_user(["view_admin"])


@th.django_unit_test("the webapp domain registers on demand with every gate declared")
def test_domain_registration(opts):
    from mojo.apps.assistant import DOMAIN_DESCRIPTIONS

    entries = _registry()
    assert "webapp" in DOMAIN_DESCRIPTIONS, \
        "the webapp domain is not listed in DOMAIN_DESCRIPTIONS"
    assert len(entries) == WEBAPP_TOOLS, \
        f"expected {WEBAPP_TOOLS} webapp tools, found {len(entries)}"
    mutating = [name for name, entry in entries.items() if entry["mutates"]]
    assert len(mutating) == MUTATING_TOOLS, \
        f"expected {MUTATING_TOOLS} mutating webapp tools, found {mutating}"
    for name, entry in entries.items():
        assert entry["core"] is False, f"{name} is core; the domain is on demand"
        assert entry["permission"] == "view_admin", \
            f"{name} declares {entry['permission']}, not the registry gate"
        assert entry["authorize"] is not None, \
            f"{name} has no authorize predicate, so it would list for everyone"
        assert "confirm with the user" not in entry["definition"]["description"].lower(), \
            f"{name} tells the model to confirm; the approval card IS the confirmation"
        if entry["mutates"]:
            assert entry["summarize"] is not None and entry["preview"] is not None, \
                f"{name} mutates without a summarize/preview pair"


@th.django_unit_test("authorize hides the domain from an operator with no WebApp authority")
def test_authorize_gates_the_domain(opts):
    from mojo.apps.assistant import get_available_domains
    from mojo.apps.assistant.services.tools.webapp import common

    assert common.authorized(opts.manager) is True, \
        "a group-scoped WebApp manager was refused the domain"
    assert common.authorized(opts.security_member) is True, \
        "a group security holder was refused the domain"
    assert common.authorized(opts.stranger) is False, \
        "a global view_admin user with no WebApp grant reached the domain"
    assert "webapp" not in get_available_domains(opts.stranger), \
        "the webapp domain was listed for an operator who holds no WebApp authority"
    assert "webapp" in get_available_domains(opts.manager), \
        "the webapp domain was hidden from a group-scoped manager"


@th.django_unit_test("a group-scoped manager with no global grant reaches the read tools")
def test_group_scoped_manager_reads(opts):
    summary = _call("get_webapp", {"webapp": opts.webapp.pk}, opts.manager)
    assert summary.get("webapp", {}).get("slug") == opts.webapp.slug, \
        f"a group-scoped manager could not read their own app: {summary}"
    assert summary["context_ref"] == {
        "app_name": "edge", "model_name": "WebApp", "pk": opts.webapp.pk,
        "label": opts.webapp.slug}, \
        f"the context hint was not the WebApp ref: {summary.get('context_ref')}"
    groups = _call("list_webapp_groups", {}, opts.manager)
    assert opts.group.pk in [row["id"] for row in groups["groups"]], \
        f"the manager's own workspace was missing from list_webapp_groups: {groups}"


@th.django_unit_test("list_webapps lists exactly what the caller may see")
def test_list_webapps_is_tenant_scoped(opts):
    listed = _call("list_webapps", {}, opts.manager)
    ids = [row["webapp"]["id"] for row in listed["items"]]
    assert opts.webapp.pk in ids, "the manager's own app was not listed"
    assert opts.other_webapp.pk not in ids, \
        f"a sibling tenant's app was listed: {ids}"
    assert "fleet" in listed, "the fleet summary was dropped from the projection"

    denied = _call("list_webapps", {"group": opts.other_group.pk}, opts.manager)
    assert "error" in denied, \
        f"a sibling workspace filter was accepted: {denied}"


@th.django_unit_test("a cross-tenant or missing app id gets one identical refusal")
def test_webapp_resolution_is_non_oracular(opts):
    foreign = _call("get_webapp", {"webapp": opts.other_webapp.pk}, opts.manager)
    missing = _call("get_webapp", {"webapp": 999_000_999}, opts.manager)
    junk = _call("get_webapp", {"webapp": "not-an-id"}, opts.manager)
    assert foreign.get("error") == missing.get("error") == junk.get("error"), (
        "a cross-tenant id is distinguishable from a missing one: "
        f"{foreign} vs {missing} vs {junk}")
    assert foreign.get("error"), "a cross-tenant app id was not refused at all"


@th.django_unit_test("an inactive workspace resolves exactly like a missing one")
def test_inactive_group_resolves_like_missing(opts):
    group = make_group("edge-asinactive")
    from mojo.apps.account.models import GroupMember
    member, _ = GroupMember.objects.get_or_create(
        user=opts.manager, group=group)
    member.permissions = {"manage_webapp": True, "manage_dns": True}
    member.save()

    live = _call("get_webapp_setup_options", {"group": group.pk}, opts.manager)
    assert "error" not in live, f"an active workspace was refused: {live}"

    group.is_active = False
    group.save(update_fields=["is_active", "modified"])
    dark = _call("get_webapp_setup_options", {"group": group.pk}, opts.manager)
    gone = _call("get_webapp_setup_options", {"group": 999_000_999}, opts.manager)
    assert dark.get("error") == gone.get("error"), (
        f"a deactivated workspace is distinguishable from a missing one: "
        f"{dark} vs {gone}")


@th.django_unit_test("serving withholds fleet inventory from a viewer")
def test_serving_partitions_fleet_inventory(opts):
    viewer = _call("get_webapp_serving", {"webapp": opts.webapp.pk}, opts.viewer)
    manager = _call("get_webapp_serving", {"webapp": opts.webapp.pk}, opts.manager)

    assert viewer.get("can_manage") is False, \
        "a view-only operator was reported as a writer"
    assert viewer["serving"]["pools"] is None and viewer["upstreams"] is None, (
        "a viewer was told the deployment's pool and upstream inventory: "
        f"{viewer['serving']['pools']} / {viewer['upstreams']}")
    assert manager["serving"]["pools"], \
        "a manager was denied the pool list they are meant to choose from"
    assert isinstance(viewer.get("addresses"), list), \
        "the merged address list is missing from the serving read"


@th.django_unit_test("deployment evidence is counts for a viewer and never a runner id")
def test_deployment_evidence_is_partitioned(opts):
    from mojo.apps.edge.models import WebAppDeployment

    deployment = WebAppDeployment.objects.create(
        webapp=opts.webapp, release=opts.release, status="failed",
        detail="fleet deployment failed",
        targets=[{"runner": "edge-secret-runner-01", "job": 987_000_001},
                 {"runner": "edge-secret-runner-02", "job": 987_000_002}])

    viewer = _call("get_webapp_deployment", {"deployment": deployment.pk}, opts.viewer)
    manager = _call("get_webapp_deployment", {"deployment": deployment.pk}, opts.manager)

    assert viewer["nodes"]["expected"] == 2, \
        f"the node count was wrong for a viewer: {viewer['nodes']}"
    assert "detail" not in viewer["nodes"] and "errors" not in viewer["nodes"], (
        "a viewer was given per-node outcomes and error text: "
        f"{viewer['nodes']}")
    assert "detail" in manager["nodes"] and "errors" in manager["nodes"], \
        f"a writer was denied the per-node evidence: {manager['nodes']}"
    for payload in (viewer, manager):
        assert "edge-secret-runner" not in str(payload), \
            f"a runner id reached the model: {payload}"


@th.django_unit_test("a cross-tenant deployment id is refused like a missing one")
def test_deployment_resolution_is_non_oracular(opts):
    from mojo.apps.edge.models import WebAppDeployment

    other_release = make_release(opts.other_webapp, "otherv1", status="uploaded")
    foreign_deployment = WebAppDeployment.objects.create(
        webapp=opts.other_webapp, release=other_release, status="live")

    foreign = _call("get_webapp_deployment",
                    {"deployment": foreign_deployment.pk}, opts.manager)
    missing = _call("get_webapp_deployment",
                    {"deployment": 999_000_999}, opts.manager)
    assert foreign.get("error") == missing.get("error"), (
        "webapp_deploy.payload performs no authority check, so a cross-tenant "
        f"deployment must resolve like a missing one: {foreign} vs {missing}")


@th.django_unit_test("deploy setup returns the workflow and a safe key status, never a token")
def test_deploy_setup_never_mints_or_reveals(opts):
    from pathlib import Path

    from mojo.apps.edge.services import webapp_keys

    webapp_keys.link(opts.webapp)
    opts.webapp.refresh_from_db()
    result = _call("get_webapp_deploy_setup", {"webapp": opts.webapp.pk}, opts.manager)

    assert result["key"] == {
        "linked": True, "active": True,
        "created": result["key"]["created"],
        "last_used": result["key"]["last_used"],
        "last_action": result["key"]["last_action"]}, \
        f"the key projection changed shape: {result['key']}"
    assert set(result["key"]) == {"linked", "active", "created", "last_used",
                                  "last_action"}, \
        f"the key projection grew a field: {sorted(result['key'])}"
    assert "MOJO_DEPLOY_KEY: ${{ secrets.MOJO_DEPLOY_KEY }}" in result["workflow"]["yaml"], \
        "the workflow file no longer references the repository secret"
    assert "token" not in str(result).lower().replace("mojo_deploy_key", ""), \
        f"a token-shaped value appeared in the deploy setup result: {result}"

    root = Path(__file__).resolve().parents[2]
    package = root / "mojo/apps/assistant/services/tools/webapp"
    for source in package.glob("*.py"):
        assert "link_once(" not in source.read_text(), (
            f"{source.name} calls webapp_keys.link_once; a minted deploy key "
            f"would land in the approval record the model reads next turn")


@th.django_unit_test("precheck never offers to buy a domain")
def test_precheck_strips_purchase(opts):
    result = _call("precheck_new_webapp_address",
                   {"group": opts.group.pk, "url": "https://brand-new-name.example"},
                   opts.manager)
    options = result.get("options") or {}
    assert "purchase_available" not in options and "godaddy_available" not in options, \
        f"chat was offered a domain purchase path: {options}"


@th.django_unit_test("group security alone passes onboarding and fails every day-2 write")
def test_security_only_member_is_read_and_onboarding_only(opts):
    from mojo.apps.assistant.services.tools.webapp import common

    assert common.can_manage_onboarding(opts.security_member, opts.group) is True, \
        "a group security holder was refused the onboarding gate the endpoints allow"
    assert common.can_manage(opts.security_member, opts.group) is False, (
        "a group security holder passed the day-2 gate; every day-2 endpoint "
        "also carries requires_perms('manage_webapp')")

    day2 = [name for name, entry in _registry().items()
            if entry["mutates"] and name not in (
                "start_webapp_setup", "answer_webapp_setup_step",
                "cancel_webapp_setup")]
    assert len(day2) == 11, f"expected 11 day-2 tools, found {day2}"
    for name in day2:
        params = {"webapp": opts.webapp.pk, "reason": "audit exercise",
                  "hostname": "www.example.com", "vhost": 1, "release": 1,
                  "certificate": 1, "path_prefix": "/api", "upstream": "x",
                  "pool": "default"}
        error = None
        try:
            _preview(name, params, opts.security_member)
        except (ValueError, PermissionError) as exc:
            error = exc
        assert error is not None, \
            f"{name} let a group-security-only member propose a day-2 write"


@th.django_unit_test("health reports not_configured for an app with no address")
def test_health_reports_not_configured(opts):
    site = make_webapp(opts.group, slug=f"nh{uuid.uuid4().hex[:6]}")
    result = _call("check_webapp_health", {"webapp": site.pk}, opts.manager)
    assert result["status"] == "not_configured", (
        "an app with no address is not unhealthy; nothing is meant to be "
        f"serving yet: {result}")


@th.django_unit_test("deploy history caps its lists and keeps fleet targets out")
def test_deploy_history_projection(opts):
    from mojo.apps.edge.models import WebAppDeployment

    WebAppDeployment.objects.create(
        webapp=opts.webapp, release=opts.release, status="live",
        targets=[{"runner": "edge-secret-runner-09", "job": 987_000_003}])
    result = _call("get_webapp_deploy_history", {"webapp": opts.webapp.pk},
                   opts.manager)

    assert result["releases"], "no versions were returned for an app that has one"
    assert result["deployments"], "no deployments were returned"
    assert "targets" not in str(result) and "edge-secret-runner" not in str(result), (
        "the deploy history leaked fleet targets, which are outside both "
        f"RestMeta graphs: {result}")


@th.django_unit_test("setup status is readable across surfaces and names the origin")
def test_setup_status_reads_either_surface(opts):
    from mojo.apps.edge.models import WebAppOnboardingOperation
    from mojo.apps.edge.services import webapp_onboarding

    portal = WebAppOnboardingOperation.objects.create(
        group=opts.group, actor=opts.manager, web_app=opts.webapp,
        origin="https://admin.example.com",
        replay_fingerprint=uuid.uuid4().hex, cursor="address",
        state={"profile": {"slug": opts.webapp.slug}, "choices": {}})
    chat = WebAppOnboardingOperation.objects.create(
        group=opts.group, actor=opts.manager, web_app=opts.webapp,
        origin=webapp_onboarding.ASSISTANT_ORIGIN,
        replay_fingerprint=uuid.uuid4().hex, cursor="github",
        state={"profile": {"slug": opts.webapp.slug}, "choices": {}})

    from_portal = _call("get_webapp_setup_status",
                        {"operation_id": str(portal.operation_id)}, opts.manager)
    from_chat = _call("get_webapp_setup_status",
                      {"operation_id": str(chat.operation_id)}, opts.manager)
    assert from_portal["origin_surface"] == "admin_portal", \
        f"a portal setup did not name its surface: {from_portal.get('origin_surface')}"
    assert from_chat["origin_surface"] == "assistant", \
        f"a chat setup did not name its surface: {from_chat.get('origin_surface')}"

    foreign = _call("get_webapp_setup_status",
                    {"operation_id": str(chat.operation_id)}, opts.viewer)
    missing = _call("get_webapp_setup_status",
                    {"operation_id": str(uuid.uuid4())}, opts.viewer)
    assert foreign.get("error") == missing.get("error"), (
        "another administrator's setup is distinguishable from a missing one: "
        f"{foreign} vs {missing}")
