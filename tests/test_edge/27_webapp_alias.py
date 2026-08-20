"""Custom-domain ALIAS addresses for a WebApp (item 2226).

One app, one primary address (`WebApp.vhost`) and any number of extra
addresses (`Vhost.alias_of`) serving the identical release. These tests cover
the attach state machine, its refusals, the fleet fan-out, and the model
invariants that hold even when a service is bypassed.

No `cleanup()` here on purpose: every fixture is uuid-named and lives in a
group created by this setup, so there is nothing from a previous run to
collide with — and the shared `cleanup()` sweeps rows belonging to the other
edge modules running in parallel.
"""

import uuid
from unittest import mock

from testit import helpers as th

from tests.test_edge._helpers import (
    declare_pools, declare_release_buckets, login, make_certificate,
    make_domain, make_group, make_group_member, make_release, make_upstream,
    make_user, make_vhost, make_webapp, raises, with_setting,
)


TARGET = "edge-alias-target.example.net"


@th.django_unit_setup()
def setup_webapp_alias(opts):
    from mojo.apps.account.models.setting import Setting

    declare_pools()
    declare_release_buckets()
    opts.group = make_group("edge-alias")
    opts.actor, opts.actor_email, opts.actor_password, _ = make_group_member(
        ["manage_webapp", "manage_dns"], group=opts.group)
    opts.admin, opts.admin_email, opts.admin_password = make_user(
        ["manage_dns", "manage_webapp"], is_superuser=True)
    opts.auth_upstream = make_upstream()
    Setting.set("EDGE_WEBAPP_AUTH_UPSTREAM", str(opts.auth_upstream.pk),
                group=None)


def _app_with_primary(opts, group=None, domain=None, pool="default",
                      label="primary"):
    """A WebApp already live on its own address — attach()'s precondition."""
    group = group or opts.group
    domain = domain if domain is not None else make_domain(
        group=group, provider="route53")
    certificate = make_certificate(domain)
    vhost = make_vhost(domain, certificate, label=label, kind="site_api",
                       pool=pool)
    web_app = make_webapp(group, slug=f"al{uuid.uuid4().hex[:8]}", vhost=vhost)
    return web_app, domain, certificate, vhost


def _attach(opts, web_app, hostname, actor=None, zone=None, cname_targets=None,
            retry_certificate=False, delegation_row=None):
    """Run attach with every provider seam mocked; return (result, mocks)."""
    from mojo.apps.edge.services import webapp_alias

    def run():
        with mock.patch("mojo.apps.dnsman.services.dns.list_records",
                        return_value=list(zone or [])) as listed, \
                mock.patch("mojo.apps.dnsman.services.dns.upsert_record") as upsert, \
                mock.patch("mojo.helpers.dns.probe.query_cname",
                           return_value=mock.Mock(
                               targets=list(cname_targets or []))), \
                mock.patch("mojo.apps.dnsman.services.delegation.for_domain",
                           return_value=delegation_row), \
                mock.patch("mojo.apps.dnsman.services.delegation.initiate") as initiate, \
                mock.patch(
                    "mojo.apps.dnsman.services.certs.request_certificate") as request:
            try:
                result = webapp_alias.attach(
                    web_app, hostname, actor or opts.actor,
                    retry_certificate=retry_certificate)
                error = None
            except Exception as exc:
                result, error = None, exc
            return result, error, listed, upsert, request, initiate

    return with_setting("EDGE_WEBAPP_CNAME_TARGET", TARGET, run)


# ---------------------------------------------------------------------------
# attach — happy paths
# ---------------------------------------------------------------------------
@th.django_unit_test("a managed alias writes one CNAME, reuses the cert, and serves")
def test_managed_attach_happy_path(opts):
    from mojo.apps.edge.services import webapp_auth_routes

    web_app, domain, certificate, primary = _app_with_primary(
        opts, pool="staging")
    hostname = f"shop.{domain.name}"

    result, error, listed, upsert, request, _ = _attach(opts, web_app, hostname)

    assert error is None, f"a clear managed alias was refused: {error}"
    assert result.status == "attached", f"managed alias did not attach: {result}"
    upsert.assert_called_once_with(domain, "CNAME", hostname, [TARGET], ttl=300)
    request.assert_not_called()  # the app's apex+wildcard cert already covers it

    from mojo.apps.edge.models import Vhost
    alias = Vhost.objects.get(pk=result.vhost)
    assert alias.alias_of_id == web_app.pk, \
        f"the new vhost was not marked as this app's alias: {alias.alias_of_id}"
    assert alias.server_name == hostname, \
        f"the alias serves the wrong name: {alias.server_name}"
    assert alias.pool == primary.pool, \
        f"the alias landed in pool {alias.pool}, not the app's {primary.pool}"
    assert alias.kind == "site_api" and alias.spa is True, \
        f"the alias is not a hosted SPA vhost: {alias.kind}/{alias.spa}"
    assert alias.certificate_id == certificate.pk, \
        "the alias did not reuse the domain's covering certificate"
    routes = set(alias.routes.values_list("path_prefix", flat=True))
    assert routes == set(webapp_auth_routes.auth_route_prefixes()), \
        f"the alias did not get the hosted-auth route contract: {routes}"


@th.django_unit_test("a covering wildcard CNAME means the alias writes no DNS")
def test_managed_attach_wildcard_covered_skips_write(opts):
    from objict import objict

    web_app, domain, _, _ = _app_with_primary(opts)
    zone = [objict(type="CNAME", name=f"*.{domain.name}",
                   record_values=[TARGET], ttl=300)]

    result, error, _, upsert, _, _ = _attach(
        opts, web_app, f"store.{domain.name}", zone=zone)

    assert error is None and result.status == "attached", \
        f"a wildcard-covered alias did not attach: {error or result}"
    upsert.assert_not_called()  # the wildcard already routes this name


@th.django_unit_test("an external alias asks for complete records before attaching")
def test_external_attach_records_needed(opts):
    web_app, _, _, _ = _app_with_primary(opts)
    external = make_domain(group=opts.group, provider="mojo")
    make_certificate(external)
    hostname = f"www.{external.name}"

    result, error, _, upsert, _, _ = _attach(opts, web_app, hostname)

    assert error is None, f"an unpublished external alias raised: {error}"
    assert result.status == "records_needed", \
        f"an unpublished external CNAME did not ask for records: {result}"
    assert result.reason, "records_needed carried no plain-language reason"
    assert result.records, "records_needed carried no records to publish"
    for record in result.records:
        for field in ("type", "name", "value", "ttl"):
            assert record.get(field), \
                f"the record to publish has a blank {field}: {record}"
    assert result.records[0]["name"] == hostname and \
        result.records[0]["value"] == TARGET, \
        f"the traffic CNAME was not the record shown: {result.records}"
    upsert.assert_not_called()  # the platform holds no credential for this zone

    from mojo.apps.edge.models import Vhost
    assert not Vhost.objects.filter(alias_of=web_app).exists(), \
        "a waiting alias created a serving vhost before its DNS existed"


@th.django_unit_test("a published external alias attaches with the delegated cert")
def test_external_attach_published(opts):
    web_app, _, _, _ = _app_with_primary(opts)
    external = make_domain(group=opts.group, provider="mojo")
    certificate = make_certificate(external)
    delegated = mock.Mock(state="verified", source_name="_acme-challenge.x",
                          target_name="hub.example.net")
    hostname = f"www.{external.name}"

    result, error, _, upsert, request, _ = _attach(
        opts, web_app, hostname, cname_targets=[TARGET],
        delegation_row=delegated)

    assert error is None, f"a published external alias was refused: {error}"
    assert result.status == "attached", \
        f"a published external CNAME did not attach: {result}"
    assert result.certificate == certificate.pk, \
        "the external alias did not reuse the delegated wildcard certificate"
    upsert.assert_not_called()
    request.assert_not_called()


# ---------------------------------------------------------------------------
# attach — refusals
# ---------------------------------------------------------------------------
@th.django_unit_test("an unconnected domain is needs_domain and never delegates")
def test_needs_domain_never_delegates(opts):
    web_app, _, _, _ = _app_with_primary(opts)
    stranger = make_group("edge-alias-stranger")
    foreign = make_domain(group=stranger, provider="route53")

    result, error, listed, upsert, request, initiate = _attach(
        opts, web_app, f"www.{foreign.name}")

    assert error is None, f"an unconnected domain raised instead of steering: {error}"
    assert result.status == "needs_domain", \
        f"an unconnected domain was not needs_domain: {result}"
    assert foreign.name in result.reason, \
        f"needs_domain did not name the domain to connect: {result.reason}"
    # There is no public-suffix logic here, so a guessed registrable parent
    # would delegate ACME control of the WRONG zone.
    initiate.assert_not_called()
    listed.assert_not_called()
    upsert.assert_not_called()
    request.assert_not_called()


@th.django_unit_test("the bare domain is refused with a www suggestion")
def test_apex_refused_with_www_suggestion(opts):
    web_app, domain, _, _ = _app_with_primary(opts)

    result, error, _, upsert, _, _ = _attach(opts, web_app, domain.name)

    assert error is not None, "the bare domain was accepted as an alias"
    assert f"www.{domain.name}" in str(error), \
        f"the apex refusal did not suggest the www address: {error}"
    upsert.assert_not_called()


@th.django_unit_test("a multi-label address is refused with the one-label rule")
def test_deep_label_refused(opts):
    web_app, domain, _, _ = _app_with_primary(opts)

    result, error, _, upsert, _, _ = _attach(
        opts, web_app, f"a.b.{domain.name}")

    assert error is not None, "a two-label alias was accepted"
    assert domain.name in str(error), \
        f"the deep-label refusal did not name the domain: {error}"
    upsert.assert_not_called()


@th.django_unit_test("an occupied address is refused plainly, never as an IntegrityError")
def test_conflict_refused_for_primary_and_admin_vhost(opts):
    from django.db import IntegrityError

    web_app, domain, certificate, _ = _app_with_primary(opts)

    # (a) another app's PRIMARY address
    other_vhost = make_vhost(domain, certificate, label="taken", kind="site_api")
    make_webapp(opts.group, slug=f"occ{uuid.uuid4().hex[:6]}", vhost=other_vhost)
    _, error, _, _, _, _ = _attach(opts, web_app, f"taken.{domain.name}")
    assert error is not None and not isinstance(error, IntegrityError), \
        f"another app's address was adopted or raised at the DB: {error!r}"
    assert "already serving" in str(error), \
        f"the conflict refusal was not the plain message: {error}"

    # (b) an admin-created vhost of an unrelated kind
    make_vhost(domain, certificate, label="go", kind="redirect",
               redirect_to="example.com")
    _, error, _, _, _, _ = _attach(opts, web_app, f"go.{domain.name}")
    assert error is not None and not isinstance(error, IntegrityError), \
        f"an admin-created redirect vhost did not refuse plainly: {error!r}"


@th.django_unit_test("re-attaching the same address is a free, write-free success")
def test_idempotent_reattach(opts):
    web_app, domain, _, _ = _app_with_primary(opts)
    hostname = f"again.{domain.name}"

    first, error, _, upsert, _, _ = _attach(opts, web_app, hostname)
    assert error is None and first.status == "attached", \
        f"the first attach did not succeed: {error or first}"
    upsert.assert_called_once()

    second, error, listed, upsert, request, _ = _attach(
        opts, web_app, hostname)
    assert error is None and second.status == "attached", \
        f"re-attaching an owned address was not idempotent: {error or second}"
    assert second.vhost == first.vhost, \
        "re-attaching minted a second vhost for the same address"
    assert second.created is False, \
        "an idempotent re-attach reported itself as a fresh create"
    upsert.assert_not_called()  # the Check button must be free to press
    listed.assert_not_called()
    request.assert_not_called()


@th.django_unit_test("a failed certificate is re-requested only on an explicit retry")
def test_certificate_failed_requires_explicit_retry(opts):
    from mojo.apps.dnsman.models import Certificate

    web_app, domain, certificate, _ = _app_with_primary(opts)
    # The domain's only certificate for the apex+wildcard profile FAILED.
    Certificate.objects.filter(pk=certificate.pk).update(status="failed")
    hostname = f"fail.{domain.name}"

    result, error, _, _, request, _ = _attach(opts, web_app, hostname)
    assert error is None, f"a failed certificate raised instead of reporting: {error}"
    assert result.status == "certificate_failed", \
        f"a failed certificate was not reported: {result}"
    assert result.records, "certificate_failed carried no repair records"
    # request_certificate's reuse scan does not match a failed row, so an
    # auto-retry would mint a brand-new ACME order on every click.
    request.assert_not_called()

    result, error, _, _, request, _ = _attach(
        opts, web_app, hostname, retry_certificate=True)
    assert error is None, f"an explicit retry raised: {error}"
    assert result.status == "certificate_pending", \
        f"an explicit retry did not re-request the certificate: {result}"
    request.assert_called_once()


@th.django_unit_test("an app with no address of its own cannot take a custom one")
def test_app_without_primary_refused(opts):
    web_app = make_webapp(opts.group, slug=f"nop{uuid.uuid4().hex[:6]}")
    domain = make_domain(group=opts.group, provider="route53")
    make_certificate(domain)

    _, error, listed, upsert, _, _ = _attach(
        opts, web_app, f"www.{domain.name}")

    assert error is not None, "an addressless app accepted a custom domain"
    assert "address first" in str(error), \
        f"the refusal was not the plain platform-address-first message: {error}"
    listed.assert_not_called()
    upsert.assert_not_called()


@th.django_unit_test("an ancestor's domain needs authority in the owning workspace")
def test_authority_is_checked_before_any_provider_call(opts):
    from mojo.apps.account.models import Group
    from mojo import errors as me

    parent = make_group("edge-alias-parent")
    child = Group.objects.create(
        name=f"edge-alias-child_{uuid.uuid4().hex[:8]}",
        kind="organization", parent=parent)
    domain = make_domain(group=parent, provider="route53")
    web_app, _, _, _ = _app_with_primary(
        opts, group=child, domain=domain, label="own")
    hostname = f"shop.{domain.name}"

    child_actor, _, _, _ = make_group_member(
        ["manage_webapp", "manage_dns"], group=child)
    _, error, listed, upsert, request, _ = _attach(
        opts, web_app, hostname, actor=child_actor)
    assert isinstance(error, me.PermissionDeniedException), \
        f"a child-only grant reached an ancestor's zone: {error!r}"
    assert "workspace that owns it" in str(error), \
        f"the refusal was not the plain owning-workspace message: {error}"
    listed.assert_not_called()  # refused before ANY provider read or write
    upsert.assert_not_called()
    request.assert_not_called()

    parent_actor, _, _, _ = make_group_member(
        ["manage_webapp", "manage_dns"], group=parent)
    result, error, _, upsert, _, _ = _attach(
        opts, web_app, hostname, actor=parent_actor)
    assert error is None and result.status == "attached", \
        f"a parent-level grant did not attach the alias: {error or result}"
    upsert.assert_called_once()


# ---------------------------------------------------------------------------
# detach
# ---------------------------------------------------------------------------
@th.django_unit_test("detach removes the alias only, never the app's own address")
def test_detach_removes_only_the_alias(opts):
    from mojo import errors as me
    from mojo.apps.edge.models import Vhost
    from mojo.apps.edge.services import webapp_alias

    web_app, domain, certificate, primary = _app_with_primary(opts)
    result, error, _, _, _, _ = _attach(
        opts, web_app, f"drop.{domain.name}")
    assert error is None, f"setup attach failed: {error}"
    alias = Vhost.objects.get(pk=result.vhost)

    assert raises(webapp_alias.detach, web_app, primary) is not None, \
        "the app's own address was detachable through the alias path"

    webapp_alias.detach(web_app, alias)
    assert not Vhost.objects.filter(pk=alias.pk).exists(), \
        "detach left the alias vhost serving"
    assert Vhost.objects.filter(pk=primary.pk).exists(), \
        "detaching an alias removed the app's own address"
    from mojo.apps.dnsman.models import Certificate
    assert Certificate.objects.filter(pk=certificate.pk).exists(), \
        "detaching an alias destroyed the shared certificate"

    other_app = make_webapp(opts.group, slug=f"oth{uuid.uuid4().hex[:6]}")
    stray = make_vhost(domain, certificate, label="stray", kind="site_api")
    error = raises(webapp_alias.detach, other_app, stray)
    assert isinstance(error, me.MojoException), \
        f"a vhost that is not this app's alias was detachable: {error!r}"


# ---------------------------------------------------------------------------
# fleet fan-out
# ---------------------------------------------------------------------------
@th.django_unit_test("desired state carries alias rows only while a primary exists")
def test_desired_webapps_emits_alias_rows(opts):
    from mojo.apps.edge.models import Vhost
    from mojo.apps.edge.services import releases

    web_app, domain, _, primary = _app_with_primary(opts)
    result, error, _, _, _, _ = _attach(opts, web_app, f"fan.{domain.name}")
    assert error is None, f"setup attach failed: {error}"
    alias = Vhost.objects.get(pk=result.vhost)
    release = make_release(web_app, f"v{uuid.uuid4().hex[:6]}", status="live")
    web_app.current_release = release
    web_app.save(update_fields=["current_release", "modified"])

    rows = releases.desired_webapps([primary, alias])
    by_vhost = {row["vhost"]: row for row in rows}
    assert set(by_vhost) == {primary.pk, alias.pk}, \
        f"desired state did not carry both addresses: {sorted(by_vhost)}"
    assert by_vhost[alias.pk]["release"]["id"] == release.pk, \
        "the alias serves a different release than the app's own address"
    assert by_vhost[alias.pk]["slug"] == web_app.slug and \
        by_vhost[alias.pk]["bucket"] == web_app.bucket, \
        "the alias row lost the owning app's identity"

    # Take the site offline: with no primary, the custom domain must stop
    # being desired too, or it would keep serving what was just taken down.
    web_app.vhost = None
    web_app.save(update_fields=["vhost", "modified"])
    rows = releases.desired_webapps([primary, alias])
    assert [row["vhost"] for row in rows] == [], \
        f"an addressless app still published its alias to the fleet: {rows}"


@th.django_unit_test("a lagging alias warns the node instead of failing the deploy")
def test_install_node_proves_primary_strictly_and_alias_softly(opts):
    from mojo.apps.edge.models import Vhost, WebAppDeployment
    from mojo.apps.edge.services import webapp_deploy

    web_app, domain, _, primary = _app_with_primary(opts)
    result, error, _, _, _, _ = _attach(opts, web_app, f"soft.{domain.name}")
    assert error is None, f"setup attach failed: {error}"
    alias = Vhost.objects.get(pk=result.vhost)
    release = make_release(web_app, f"v{uuid.uuid4().hex[:6]}", status="live")
    web_app.current_release = release
    web_app.save(update_fields=["current_release", "modified"])
    deployment = WebAppDeployment.objects.create(webapp=web_app, release=release)

    def run_node(installed):
        job = mock.Mock()
        job.payload = {"deployment": deployment.pk,
                       "expected_release": release.pk, "rollback": False}
        job.metadata = {}
        with mock.patch("mojo.apps.edge.services.installer.install",
                        return_value=installed):
            outcome = webapp_deploy.install_node(job)
        return outcome, job

    # The ALIAS lags: a warning, and the node still succeeds. A hard fail here
    # would roll the release back — and the same check runs during rollback.
    outcome, job = run_node({
        "generation": "gen1", "changed": True, "excluded": [],
        "www_pending": {str(alias.pk): release.version}})
    warnings = job.metadata["webapp_deployment"]["alias_warnings"]
    assert outcome.startswith("installed"), \
        f"a lagging alias failed the whole node: {outcome}"
    assert any(alias.server_name in warning for warning in warnings), \
        f"the lagging alias was not named in the node warnings: {warnings}"

    outcome, job = run_node({
        "generation": "gen2", "changed": True, "excluded": [alias.pk],
        "www_pending": {}})
    warnings = job.metadata["webapp_deployment"]["alias_warnings"]
    assert outcome.startswith("installed") and warnings, \
        f"an excluded alias failed the node instead of warning: {outcome}"

    # The PRIMARY keeps its hard proof.
    error = raises(lambda: run_node({
        "generation": "gen3", "changed": True, "excluded": [],
        "www_pending": {str(primary.pk): release.version}}))
    assert error is not None, \
        "the primary address lost its strict pending-bytes proof"


@th.django_unit_test("deleting an app and taking it offline both remove its aliases")
def test_delete_and_detach_address_remove_aliases(opts):
    from mojo.apps.edge.models import Vhost

    # take-a-site-offline
    web_app, domain, _, _ = _app_with_primary(opts)
    result, error, _, _, _, _ = _attach(opts, web_app, f"off.{domain.name}")
    assert error is None, f"setup attach failed: {error}"
    offline_alias = result.vhost
    login(opts, opts.admin_email, opts.admin_password)
    response = opts.client.post("/api/edge/webapp/detach_address",
                                {"webapp": web_app.pk})
    assert response.status_code == 200, \
        f"detach_address failed: {response.status_code} {response.body}"
    assert not Vhost.objects.filter(pk=offline_alias).exists(), \
        "taking a site offline left its custom domain serving"

    # delete the app
    other, other_domain, _, _ = _app_with_primary(opts)
    result, error, _, _, _, _ = _attach(
        opts, other, f"gone.{other_domain.name}")
    assert error is None, f"setup attach failed: {error}"
    deleted_alias = result.vhost
    response = opts.client.delete(f"/api/edge/webapp/{other.pk}")
    assert response.status_code == 200, \
        f"webapp delete failed: {response.status_code} {response.body}"
    assert not Vhost.objects.filter(pk=deleted_alias).exists(), \
        "deleting an app left its custom domain serving"


@th.django_unit_test("an alias renders and heals exactly like a primary address")
def test_alias_gets_the_same_auth_contract(opts):
    from mojo.apps.edge.models import Vhost, VhostRoute
    from mojo.apps.edge.services import webapp_auth_routes

    web_app, domain, _, _ = _app_with_primary(opts)
    result, error, _, _, _, _ = _attach(opts, web_app, f"auth.{domain.name}")
    assert error is None, f"setup attach failed: {error}"
    alias = Vhost.objects.select_related("domain").get(pk=result.vhost)

    assert webapp_auth_routes.owning_app(alias).pk == web_app.pk, \
        "an alias did not resolve back to its owning app"
    contract = webapp_auth_routes.rendered_contract(alias)
    assert contract is not None, \
        "an alias rendered WITHOUT the auth contract and honeypots"
    assert contract["honeypots"] == webapp_auth_routes.HONEYPOT_PATHS, \
        f"the alias honeypot set differs from the primary's: {contract}"

    # Startup healing reaches aliases too.
    VhostRoute.objects.filter(vhost=alias).delete()
    healed = webapp_auth_routes.reconcile_all()
    routes = set(alias.routes.values_list("path_prefix", flat=True))
    assert routes == set(webapp_auth_routes.auth_route_prefixes()), \
        f"startup healing skipped the alias address: {routes}"
    assert web_app.pk not in healed["failed"], \
        f"startup healing failed on this app's addresses: {healed}"


@th.django_unit_test("onboarding refuses a label an alias already holds")
def test_advance_address_refuses_alias_occupied_label(opts):
    from mojo import errors as me
    from mojo.apps.edge.models import WebAppOnboardingOperation
    from mojo.apps.edge.services import webapp_onboarding

    web_app, domain, _, _ = _app_with_primary(opts)
    result, error, _, _, _, _ = _attach(opts, web_app, f"held.{domain.name}")
    assert error is None, f"setup attach failed: {error}"

    newcomer = make_webapp(opts.group, slug=f"new{uuid.uuid4().hex[:6]}")
    operation = WebAppOnboardingOperation.objects.create(
        group=opts.group, actor=opts.actor, web_app=newcomer,
        origin="https://example.com", replay_fingerprint=uuid.uuid4().hex,
        cursor="address",
        state={"profile": {}, "choices": {
            "address": {"label": "held", "domain": domain.pk}}})

    def run():
        with mock.patch("mojo.apps.dnsman.services.dns.list_records",
                        return_value=[]), \
                mock.patch("mojo.apps.dnsman.services.dns.upsert_record"):
            return raises(webapp_onboarding._advance_address, operation)

    error = with_setting("EDGE_WEBAPP_CNAME_TARGET", TARGET, run)
    assert isinstance(error, me.PermissionDeniedException), \
        f"onboarding adopted an address already held by an alias: {error!r}"
    newcomer.refresh_from_db()
    assert newcomer.vhost_id is None, \
        "the refused onboarding still linked the alias vhost"


@th.django_unit_test("the summary lists an app's alias addresses without changing v1")
def test_summary_lists_aliases(opts):
    from mojo.apps.edge.services import webapp_onboarding

    web_app, domain, _, _ = _app_with_primary(opts)
    result, error, _, _, _, _ = _attach(opts, web_app, f"sum.{domain.name}")
    assert error is None, f"setup attach failed: {error}"

    summary = webapp_onboarding.summary_for(web_app)
    assert summary["schema_version"] == 1, \
        "adding aliases changed the frozen summary schema version"
    hostnames = [row["hostname"] for row in summary["address"]["aliases"]]
    assert hostnames == [f"sum.{domain.name}"], \
        f"the summary did not list the app's alias address: {hostnames}"
    assert summary["address"]["aliases"][0]["certificate"]["status"] == "active", \
        f"the alias certificate status was not reported: {summary['address']}"

    from mojo.apps.edge.services import webapp_alias
    rows = webapp_alias.status_rows(web_app)
    assert [row["role"] for row in rows] == ["primary", "alias"], \
        f"status_rows did not list the primary first: {rows}"
    assert rows[1]["dns"] == "managed", \
        f"a route53 alias was not reported as platform-managed: {rows[1]}"


# ---------------------------------------------------------------------------
# the REST surface — same guard stack as link_key
# ---------------------------------------------------------------------------
@th.django_unit_test("a member with manage_webapp adds and removes an address through the API")
def test_attach_and_detach_endpoints(opts):
    from mojo.apps.edge.models import Vhost

    web_app, domain, _, primary = _app_with_primary(opts)
    hostname = f"api.{domain.name}"
    # Attach once in-process (every provider seam mocked); the endpoint call
    # below then re-enters attach on an address this app already owns, which is
    # exactly the free, write-free path the Check button relies on.
    seeded, error, _, _, _, _ = _attach(opts, web_app, hostname)
    assert error is None, f"setup attach failed: {error}"

    login(opts, opts.actor_email, opts.actor_password)
    try:
        response = opts.client.post("/api/edge/webapp/attach_domain", json=dict(
            webapp=web_app.pk, hostname=hostname, group=opts.group.pk))
        data = response.json.get("data") or {}
        assert response.status_code == 200, \
            f"a manage_webapp member could not add an address: {response.body}"
        assert data.get("status") == "attached" and data.get("created") is False, \
            f"the endpoint did not return the attach result verbatim: {data}"
        assert data.get("hostname") == hostname and data.get("vhost") == seeded.vhost, \
            f"the endpoint reported a different address than the service: {data}"
        assert data.get("dns") == "managed", \
            f"the endpoint dropped the server's own managed-DNS finding: {data}"

        # The app's OWN address is not removable through this path, and a
        # foreign id is refused the same way a missing one is.
        refused = opts.client.post("/api/edge/webapp/detach_domain", json=dict(
            webapp=web_app.pk, vhost=primary.pk, group=opts.group.pk))
        assert refused.status_code == 404, \
            f"the app's own address was removable as an alias: {refused.body}"
        assert Vhost.objects.filter(pk=primary.pk).exists(), \
            "the refused call still removed the app's own address"

        removed = opts.client.post("/api/edge/webapp/detach_domain", json=dict(
            webapp=web_app.pk, vhost=seeded.vhost, group=opts.group.pk))
        removed_data = removed.json.get("data") or {}
        assert removed.status_code == 200, \
            f"a manage_webapp member could not remove an address: {removed.body}"
        assert removed_data.get("status") == "detached" and \
            removed_data.get("hostname") == hostname, \
            f"detach did not report the address it removed: {removed_data}"
        assert not Vhost.objects.filter(pk=seeded.vhost).exists(), \
            "the removed address is still serving"

        repeat = opts.client.post("/api/edge/webapp/detach_domain", json=dict(
            webapp=web_app.pk, vhost=seeded.vhost, group=opts.group.pk))
        assert repeat.status_code == 404, \
            f"a removed address was not a plain 404 on the second call: {repeat.body}"
    finally:
        opts.client.logout()


@th.django_unit_test("adding an address refuses machine sessions and stale logins")
def test_attach_endpoint_requires_fresh_interactive_auth(opts):
    import time

    from mojo.apps.account.utils.jwtoken import JWToken
    from mojo.apps.edge.services import webapp_keys

    web_app, domain, _, _ = _app_with_primary(opts)
    hostname = f"gate.{domain.name}"
    _, _, machine_token, _ = webapp_keys.link(web_app)

    opts.client.logout()
    opts.client.session.headers["Authorization"] = f"apikey {machine_token}"
    try:
        machine = opts.client.post("/api/edge/webapp/attach_domain", json=dict(
            webapp=web_app.pk, hostname=hostname))
        assert machine.status_code == 403, \
            f"a deployment key added an address ({machine.status_code})"
        machine = opts.client.post("/api/edge/webapp/detach_domain", json=dict(
            webapp=web_app.pk, vhost=987654321))
        assert machine.status_code == 403, \
            f"a deployment key removed an address ({machine.status_code})"
    finally:
        opts.client.session.headers.pop("Authorization", None)

    opts.client.is_authenticated = True
    opts.client.bearer = "bearer"
    opts.client.access_token = JWToken(
        opts.admin.get_auth_key()).create_access_token(
            uid=opts.admin.pk, auth_time=int(time.time()) - 601)
    stale = opts.client.post("/api/edge/webapp/attach_domain", json=dict(
        webapp=web_app.pk, hostname=hostname))
    assert stale.status_code == 440, \
        f"a stale login added an address without stepping up ({stale.status_code})"
    stale = opts.client.post("/api/edge/webapp/detach_domain", json=dict(
        webapp=web_app.pk, vhost=987654321))
    assert stale.status_code == 440, \
        f"a stale login removed an address without stepping up ({stale.status_code})"
    opts.client.logout()


@th.django_unit_test("the address list is a plain read, and never answers for another tenant's app")
def test_aliases_endpoint_scope(opts):
    from mojo.apps.edge.rest import web_app as views

    assert getattr(views.on_webapp_aliases,
                   "_mojo_denies_key_backed_session", False), \
        "the address list accepts key-backed sessions"
    assert not hasattr(views.on_webapp_aliases, "_mojo_requires_fresh_auth"), \
        "a read of the address list demands a step-up"

    web_app, domain, _, _ = _app_with_primary(opts)
    result, error, _, _, _, _ = _attach(opts, web_app, f"list.{domain.name}")
    assert error is None, f"setup attach failed: {error}"

    viewer, viewer_email, viewer_password, _ = make_group_member(
        ["view_dns"], group=opts.group)
    login(opts, viewer_email, viewer_password)
    try:
        response = opts.client.get(
            f"/api/edge/webapp/aliases?webapp={web_app.pk}&group={opts.group.pk}")
        data = response.json.get("data") or {}
        assert response.status_code == 200, \
            f"a view-level member could not read the addresses: {response.body}"
        rows = data.get("addresses") or []
        assert [row["role"] for row in rows] == ["primary", "alias"], \
            f"the address list did not lead with the app's own address: {rows}"
        assert rows[1]["hostname"] == f"list.{domain.name}" and \
            rows[1]["vhost"] == result.vhost, \
            f"the added address is missing from the list: {rows}"
    finally:
        opts.client.logout()

    stranger = make_group("edge-alias-reader")
    _, outsider_email, outsider_password, _ = make_group_member(
        ["view_dns", "manage_dns"], group=stranger)
    login(opts, outsider_email, outsider_password)
    try:
        refused = opts.client.get(
            f"/api/edge/webapp/aliases?webapp={web_app.pk}&group={stranger.pk}")
        assert refused.status_code in (401, 403, 404), \
            f"another tenant read this app's addresses: {refused.body}"
        assert domain.name not in str(refused.body), \
            f"the refusal disclosed the app's addresses: {refused.body}"
    finally:
        opts.client.logout()


@th.django_unit_test("the certificate retry flag is a strict boolean, never bool('false')")
def test_retry_certificate_flag_is_strict(opts):
    from mojo.apps.edge.rest.web_app import _flag

    for raw in (True, "true", "True", "1", "yes", "on"):
        assert _flag(raw) is True, \
            f"an explicit retry was dropped for {raw!r}"
    # bool("false") is True — the whole reason this helper exists. A retry
    # mints a fresh ACME order, so junk must never turn into one.
    for raw in (None, False, "", "false", "False", "0", "no", "off", "maybe", 0):
        assert _flag(raw) is False, \
            f"{raw!r} was read as an explicit certificate retry"


# ---------------------------------------------------------------------------
# model invariants — hold even when a service is bypassed
# ---------------------------------------------------------------------------
@th.django_unit_test("an alias vhost must be a site kind and cannot also be a primary")
def test_alias_kind_and_exclusivity_invariants(opts):
    from mojo.apps.edge.models import Vhost

    web_app, domain, certificate, primary = _app_with_primary(opts)

    error = raises(Vhost.objects.create, domain=domain, certificate=certificate,
                   label="red", kind="redirect", redirect_to="example.com",
                   alias_of=web_app)
    assert error is not None, "a redirect vhost was accepted as an app alias"

    primary.alias_of = web_app
    error = raises(primary.save)
    assert error is not None, \
        "one vhost was accepted as both an app's primary address and its alias"
    primary.refresh_from_db()
    assert primary.alias_of_id is None, "the refused write still persisted"


@th.django_unit_test("an alias must share the app's pool and use an owned domain")
def test_alias_pool_and_ownership_invariants(opts):
    from mojo.apps.account.models import Group
    from mojo.apps.edge.models import Vhost

    web_app, domain, certificate, _ = _app_with_primary(opts, pool="default")

    error = raises(Vhost.objects.create, domain=domain, certificate=certificate,
                   label="pool", kind="site_api", pool="staging",
                   alias_of=web_app)
    assert error is not None, \
        "an alias was accepted into a different pool than its app's"

    stranger = make_group("edge-alias-owner")
    foreign = make_domain(group=stranger, provider="route53")
    error = raises(Vhost.objects.create, domain=foreign,
                   certificate=make_certificate(foreign), label="www",
                   kind="site_api", alias_of=web_app)
    assert error is not None, \
        "an alias was accepted on an unrelated group's domain"

    house = make_domain(group=None, provider="route53")
    error = raises(Vhost.objects.create, domain=house,
                   certificate=make_certificate(house), label="www",
                   kind="site_api", alias_of=web_app)
    assert error is not None, \
        "an alias was accepted on a house (platform-owned) domain"

    # An ancestor's domain IS allowed — that is what lets one org domain carry
    # several teams' apps.
    parent = make_group("edge-alias-anc")
    child = Group.objects.create(
        name=f"edge-alias-anc-child_{uuid.uuid4().hex[:8]}",
        kind="organization", parent=parent)
    ancestor_domain = make_domain(group=parent, provider="route53")
    ancestor_cert = make_certificate(ancestor_domain)
    child_app, _, _, _ = _app_with_primary(
        opts, group=child, domain=ancestor_domain, label="teamapp")
    alias = Vhost.objects.create(
        domain=ancestor_domain, certificate=ancestor_cert, label="teamalias",
        kind="site_api", alias_of=child_app)
    assert alias.pk and alias.alias_of_id == child_app.pk, \
        "a child group's app could not take an alias on its parent's domain"


@th.django_unit_test("alias_of is not settable through the generic vhost endpoint")
def test_alias_of_is_not_rest_writable(opts):
    from mojo.apps.edge.models import Vhost

    assert "alias_of" in Vhost.RestMeta.NO_SAVE_FIELDS, \
        "alias_of left the vhost endpoint's no-save list"

    web_app, domain, certificate, _ = _app_with_primary(opts)
    plain = make_vhost(domain, certificate, label="restwrite", kind="site")

    login(opts, opts.admin_email, opts.admin_password)
    response = opts.client.post(f"/api/edge/vhost/{plain.pk}",
                                {"alias_of": web_app.pk})
    plain.refresh_from_db()
    assert plain.alias_of_id is None, (
        "a manage_dns holder grafted a vhost onto a WebApp through the generic "
        f"endpoint ({response.status_code}: {response.body})")
