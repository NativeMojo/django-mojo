"""How a WebApp is served: address, certificate, shape, and paths (item 2229).

The app-scoped serving surface. Two invariants carry most of these tests:

- **Every serving change lands on every address.** An app answers on its
  primary address AND on its aliases; a pool move or a route that reached one
  but not the others is the split-brain the alias contract exists to prevent.
- **Managed routes are derived, not stored.** `managed` is computed from the
  resolved hosted-auth contract at read time, and the writes refuse those
  prefixes outright — there is no flag to get out of step with reality.

Mocked seams are exercised through the SERVICE, never through `opts.client`:
the client calls a separate server process where `mock.patch` has no effect.

No `cleanup()` here on purpose — every fixture is uuid-named inside a group
this setup creates, and the shared sweep would take rows out from under the
other edge modules running in parallel.
"""

import uuid
from unittest import mock

from django.db import transaction

from testit import helpers as th

from tests.test_edge._helpers import (
    declare_pools, declare_release_buckets, login, make_certificate,
    make_domain, make_group, make_group_member, make_route, make_upstream,
    make_user, make_vhost, make_webapp, raises,
)


@th.django_unit_setup()
def setup_webapp_serving(opts):
    declare_pools()
    declare_release_buckets()
    opts.group = make_group("edge-serving")
    opts.admin, opts.admin_email, opts.admin_pw = make_user(
        ["manage_dns", "manage_webapp"], is_superuser=True)
    # GLOBAL view_dns only: passes the read's verb gate and the object's
    # VIEW_PERMS, and fails SAVE_PERMS — the exact shape the read's
    # editables split is about.
    opts.viewer, opts.viewer_email, opts.viewer_pw = make_user(["view_dns"])
    opts.upstream = make_upstream()


def _app(opts, group=None, provider="route53", pool="default",
         kind="site_api", spa=True, with_alias=True, mojosec_policy=None,
         domain=None):
    """An app live on its own address, plus (by default) one alias address."""
    group = group or opts.group
    if domain is None:
        domain = make_domain(group=group, provider=provider)
    certificate = make_certificate(domain)
    extra = {}
    if mojosec_policy is not None:
        extra["mojosec_policy"] = mojosec_policy
    primary = make_vhost(domain, certificate, label="app", kind=kind,
                         pool=pool, spa=spa, **extra)
    web_app = make_webapp(group, slug=f"srv{uuid.uuid4().hex[:8]}",
                          vhost=primary)
    alias = None
    if with_alias:
        alias = make_vhost(domain, certificate, label="extra", kind="site_api",
                           pool=pool, spa=spa, alias_of=web_app)
    return web_app, domain, certificate, primary, alias


def _install_managed_routes(vhost, upstream):
    """The hosted-auth prefixes, installed directly.

    Deliberately not `webapp_auth_routes.reconcile()`: that resolves its
    upstream through a process-wide Setting, and writing one would race the
    other edge modules running in parallel.
    """
    from mojo.apps.edge.services import webapp_auth_routes

    for prefix in webapp_auth_routes.auth_route_prefixes():
        make_route(vhost, prefix, upstream)


def _use_apikey(opts, token):
    opts.client.logout()
    opts.client.session.headers["Authorization"] = f"apikey {token}"


def _clear_apikey(opts):
    opts.client.session.headers.pop("Authorization", None)


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------
@th.django_unit_test("the serving read reports the app's shape, pool and paths")
def test_serving_read_reports_shape_pool_and_routes(opts):
    from mojo.apps.edge.services import webapp_auth_routes

    web_app, domain, _, primary, alias = _app(opts, pool="staging")
    _install_managed_routes(primary, opts.upstream)
    _install_managed_routes(alias, opts.upstream)
    make_route(primary, "/reports", opts.upstream)

    login(opts, opts.admin_email, opts.admin_pw)
    resp = opts.client.get(f"/api/edge/webapp/serving?webapp={web_app.pk}")
    assert resp.status_code == 200, f"serving read failed: {resp.status_code} {resp.body}"
    data = resp.json.get("data") or {}

    serving = data.get("serving") or {}
    assert serving.get("kind") == "site_api", \
        f"the read did not report the app's serving shape: {serving}"
    assert serving.get("pool") == "staging", \
        f"the read did not report the app's fleet pool: {serving}"
    assert serving.get("spa") is True and serving.get("routes_supported") is True, \
        f"the read lost the SPA fallback or the routes capability: {serving}"

    address = data.get("address") or {}
    assert address.get("hostname") == primary.server_name, \
        f"the read reported the wrong address: {address}"
    assert address.get("https_origin") == f"https://{primary.server_name}", \
        f"the read did not build the app's own https origin: {address}"
    assert (address.get("domain") or {}).get("name") == domain.name, \
        f"the read lost the owning domain: {address}"

    managed = set(webapp_auth_routes.auth_route_prefixes())
    rows = {row["path_prefix"]: row for row in data.get("routes") or []}
    assert managed.issubset(set(rows)), \
        f"the read did not list the platform's own paths: {sorted(rows)}"
    flagged = {path for path, row in rows.items() if row.get("managed")}
    assert flagged == managed, (
        "the derived managed set does not match the resolved auth contract "
        f"(managed={sorted(flagged)} expected={sorted(managed)})")
    assert rows["/reports"]["managed"] is False, \
        "a path the customer added was reported as platform-managed"
    assert (rows["/reports"]["upstream"] or {}).get("name") == opts.upstream.name, \
        f"a route lost its destination name: {rows['/reports']}"

    assert [row["hostname"] for row in data.get("aliases") or []] == \
        [alias.server_name], \
        f"the read did not name the app's extra addresses: {data.get('aliases')}"


@th.django_unit_test("fleet pool and destination inventory is a writer's fact, not a viewer's")
def test_serving_read_hides_inventory_from_a_viewer(opts):
    web_app, _, _, _, _ = _app(opts)

    login(opts, opts.admin_email, opts.admin_pw)
    writer = opts.client.get(f"/api/edge/webapp/serving?webapp={web_app.pk}")
    assert writer.status_code == 200, f"writer read failed: {writer.body}"
    writer_data = writer.json.get("data") or {}
    assert isinstance((writer_data.get("serving") or {}).get("pools"), list), \
        "a caller who may save was not told which fleet pools exist"
    assert isinstance(writer_data.get("upstreams"), list), \
        "a caller who may save was not told which destinations exist"

    login(opts, opts.viewer_email, opts.viewer_pw)
    viewer = opts.client.get(f"/api/edge/webapp/serving?webapp={web_app.pk}")
    assert viewer.status_code == 200, \
        f"a view_dns holder could not read the serving tab: {viewer.status_code} {viewer.body}"
    viewer_data = viewer.json.get("data") or {}
    assert (viewer_data.get("serving") or {}).get("pools") is None, \
        "a read-only viewer was handed the deployment's fleet pool inventory"
    assert viewer_data.get("upstreams") is None, \
        "a read-only viewer was handed the org's upstream destination names"
    assert (viewer_data.get("serving") or {}).get("pool"), \
        "the viewer lost the app's own pool along with the inventory"


@th.django_unit_test("the certificate card reports renewal, expiry and wildcard coverage")
def test_serving_read_reports_certificate_health(opts):
    from datetime import timedelta

    from django.utils import timezone

    web_app, domain, certificate, primary, _ = _app(opts)
    certificate.not_after = timezone.now() + timedelta(days=30)
    certificate.renew_after = timezone.now() + timedelta(days=10)
    certificate.save()

    login(opts, opts.admin_email, opts.admin_pw)
    resp = opts.client.get(f"/api/edge/webapp/serving?webapp={web_app.pk}")
    assert resp.status_code == 200, f"serving read failed: {resp.body}"
    data = resp.json.get("data") or {}
    cert = data.get("certificate") or {}

    assert cert.get("id") == certificate.pk and cert.get("status") == "active", \
        f"the read did not name the certificate actually serving this app: {cert}"
    assert cert.get("renew_after") and cert.get("not_after"), \
        f"the certificate card carries no renewal or expiry date: {cert}"
    assert 28 <= (cert.get("days_remaining") or 0) <= 30, \
        f"days_remaining is not the real distance to expiry: {cert}"
    assert cert.get("wildcard") is True, \
        "a shared apex+wildcard certificate was not reported as shared"
    wildcard = (data.get("address") or {}).get("wildcard") or {}
    assert wildcard.get("covered") is True and \
        wildcard.get("name") == f"*.{domain.name}", \
        f"the address card cannot say the wildcard covers this name: {wildcard}"


@th.django_unit_test("a dedicated certificate is offered only where one could actually be issued")
def test_dedicated_support_is_server_evidence(opts):
    from mojo.apps.edge.services import webapp_serving

    web_app, _, _, _, _ = _app(opts, provider="route53")
    payload = webapp_serving.serving_for(web_app)
    assert payload["certificate"]["dedicated_supported"] is True, \
        "a normal managed domain refused a dedicated certificate"
    assert payload["certificate"]["dedicated_reason"] is None, \
        "a supported domain still explained itself as unsupported"

    external, _, _, _, _ = _app(opts, provider="mojo")
    payload = webapp_serving.serving_for(external)
    assert payload["certificate"]["dedicated_supported"] is False, \
        "an external-DNS domain offered a certificate the issuer would refuse"
    assert payload["certificate"]["dedicated_reason"], \
        "an unsupported domain offered no explanation in its place"

    with mock.patch("mojo.apps.dnsman.services.delegation.for_domain",
                    return_value=mock.Mock()):
        payload = webapp_serving.serving_for(web_app)
    assert payload["certificate"]["dedicated_supported"] is False, \
        "a delegated-ACME domain offered a certificate outside its v1 profile"


@th.django_unit_test("an app with no address reads as empty, not as an error")
def test_serving_read_of_an_addressless_app(opts):
    from mojo.apps.edge.services import webapp_serving

    web_app = make_webapp(opts.group, slug=f"srv{uuid.uuid4().hex[:8]}")
    payload = webapp_serving.serving_for(web_app, include_editables=True)
    assert payload["address"]["hostname"] is None, \
        f"an addressless app reported an address: {payload['address']}"
    assert payload["serving"]["kind"] is None and \
        payload["serving"]["routes_supported"] is False, \
        f"an addressless app claimed a serving shape: {payload['serving']}"
    assert payload["routes"] == [] and payload["aliases"] == [], \
        "an addressless app carried paths or extra addresses"
    assert payload["certificate"]["id"] is None, \
        "an addressless app named a certificate"


# ---------------------------------------------------------------------------
# how it's served
# ---------------------------------------------------------------------------
@th.django_unit_test("a pool move takes every address with it and publishes both pools")
def test_pool_change_moves_every_address(opts):
    from mojo.apps.edge.models import Vhost
    from mojo.apps.edge.services import convergence, webapp_serving

    web_app, _, _, primary, alias = _app(opts, pool="default")

    published = []
    callbacks = []
    with mock.patch.object(transaction, "on_commit",
                           side_effect=lambda callback: callbacks.append(callback)), \
            convergence.publisher_scope(lambda pool: published.append(pool)):
        webapp_serving.apply(web_app, {"pool": "staging"})
    for callback in callbacks:
        callback()

    assert Vhost.objects.get(pk=primary.pk).pool == "staging", \
        "the app's own address did not move to the new pool"
    assert Vhost.objects.get(pk=alias.pk).pool == "staging", \
        "an extra address was left behind on the old pool — it would serve " \
        "from a node fleet that never installs this app's release"
    assert {"default", "staging"}.issubset(set(published)), (
        "a pool move did not publish BOTH the pool it left and the one it "
        f"joined: {sorted(set(published))}")


@th.django_unit_test("the single-page fallback applies to every address at once")
def test_spa_toggle_applies_to_every_address(opts):
    from mojo.apps.edge.models import Vhost
    from mojo.apps.edge.services import webapp_serving

    web_app, _, _, primary, alias = _app(opts, spa=True)
    webapp_serving.apply(web_app, {"spa": False})

    assert Vhost.objects.get(pk=primary.pk).spa is False, \
        "the app's own address kept the single-page fallback"
    assert Vhost.objects.get(pk=alias.pk).spa is False, \
        "an extra address kept a fallback the app no longer uses"

    # "false" is a truthy string; a bare bool() here would silently re-enable it.
    webapp_serving.apply(web_app, {"spa": "true"})
    assert Vhost.objects.get(pk=primary.pk).spa is True, \
        "an explicit affirmative did not turn the fallback back on"
    webapp_serving.apply(web_app, {"spa": "false"})
    assert Vhost.objects.get(pk=primary.pk).spa is False, \
        "the string 'false' turned the single-page fallback ON"


@th.django_unit_test("a security policy pinned to the current mode refuses the toggle plainly")
def test_spa_toggle_refused_under_a_pinned_security_policy(opts):
    from mojo.apps.edge.models import Vhost
    from mojo.apps.edge.services import webapp_serving

    policy = {"version": 1, "impossible_path_families": ["wordpress"],
              "response_class": "spa_fallback"}
    web_app, _, _, primary, _ = _app(
        opts, kind="site", spa=True, with_alias=False, mojosec_policy=policy)

    error = raises(webapp_serving.apply, web_app, {"spa": False})
    assert error is not None, (
        "flipping the mode under a pinned security policy was accepted; the "
        "save would have failed deep inside renderer validation")
    assert "security policy tied to its current mode" in str(error), \
        f"the refusal was not the plain policy-first sentence: {error}"
    assert Vhost.objects.get(pk=primary.pk).spa is True, \
        "the refused toggle still changed the app"


@th.django_unit_test("a serving save reads pool, spa and certificate — and nothing else")
def test_serving_save_ignores_every_other_field(opts):
    from mojo.apps.edge.models import Vhost
    from mojo.apps.edge.services import webapp_serving

    web_app, _, _, primary, _ = _app(opts, pool="default")
    webapp_serving.apply(web_app, {
        "pool": "staging",
        "kind": "api", "label": "hijacked", "is_enabled": False,
        "body_size_mb": 4096, "serve_static": True, "redirect_to": "evil.test",
        "alias_of": web_app.pk, "domain": 1, "quiet_paths": ["/x"],
    })

    fresh = Vhost.objects.get(pk=primary.pk)
    assert fresh.pool == "staging", "the one settable field was not applied"
    assert fresh.kind == "site_api", \
        f"the serving shape was changed over the wire: {fresh.kind}"
    assert fresh.label == "app" and fresh.is_enabled is True, \
        "a serving save moved the address or took it offline"
    assert fresh.body_size_mb == 50 and fresh.serve_static is False, \
        "a serving save wrote renderer knobs it does not own"
    assert fresh.redirect_to is None and fresh.alias_of_id is None, \
        "a serving save reached fields that decide who owns this address"


@th.django_unit_test("a pool this deployment never declared is refused before any write")
def test_undeclared_pool_refused(opts):
    from mojo.apps.edge.models import Vhost
    from mojo.apps.edge.services import webapp_serving

    web_app, _, _, primary, alias = _app(opts, pool="default")
    error = raises(webapp_serving.apply, web_app, {"pool": f"p{uuid.uuid4().hex[:6]}"})
    assert error is not None, \
        "a tenant moved their app into a pool the deployment never declared"
    assert Vhost.objects.get(pk=primary.pk).pool == "default" and \
        Vhost.objects.get(pk=alias.pk).pool == "default", \
        "the refused pool move still touched an address"


# ---------------------------------------------------------------------------
# certificate
# ---------------------------------------------------------------------------
@th.django_unit_test("switching certificate takes only this domain's active, covering rows")
def test_certificate_switch_is_scoped_active_and_covering(opts):
    from mojo.apps.dnsman.models import Certificate
    from mojo.apps.edge.models import Vhost
    from mojo.apps.edge.services import webapp_serving

    web_app, domain, certificate, primary, _ = _app(opts, with_alias=False)
    hostname = primary.server_name

    foreign_domain = make_domain(group=opts.group, provider="route53")
    foreign = make_certificate(foreign_domain)
    error = raises(webapp_serving.apply, web_app, {"certificate": foreign.pk})
    assert error is not None, \
        "a certificate belonging to another domain was attached to this app"

    pending = Certificate.objects.create(
        domain=domain, common_name=hostname, sans=[hostname], status="pending")
    error = raises(webapp_serving.apply, web_app, {"certificate": pending.pk})
    assert error is not None, \
        "the app was switched onto a certificate that has not been issued yet"
    assert "isn’t ready yet" in str(error), \
        f"the refusal did not say the certificate is still being issued: {error}"

    elsewhere = Certificate.objects.create(
        domain=domain, common_name=f"other.{domain.name}",
        sans=[f"other.{domain.name}"], status="active")
    error = raises(webapp_serving.apply, web_app, {"certificate": elsewhere.pk})
    assert error is not None, (
        "a certificate that does not cover this address was accepted — the "
        "node would serve a name it was never issued for")

    dedicated = Certificate.objects.create(
        domain=domain, common_name=hostname, sans=[hostname], status="active")
    webapp_serving.apply(web_app, {"certificate": dedicated.pk})
    assert Vhost.objects.get(pk=primary.pk).certificate_id == dedicated.pk, \
        "an active, covering certificate of this domain was not accepted"


@th.django_unit_test("requesting a dedicated certificate is safe to press twice, in every state")
def test_dedicated_certificate_request_is_idempotent(opts):
    from mojo.apps.dnsman.models import Certificate
    from mojo.apps.edge.services import webapp_serving

    web_app, domain, _, primary, _ = _app(opts, with_alias=False)
    hostname = primary.server_name

    def _mint(target, names=None):
        return Certificate.objects.create(
            domain=target, common_name=names[0], sans=list(names),
            status="pending")

    with mock.patch("mojo.apps.dnsman.services.certs.request_certificate",
                    side_effect=_mint) as request:
        first = webapp_serving.request_dedicated_certificate(web_app, opts.admin)
        assert request.call_count == 1, \
            "the first request did not reach the certificate service"
        assert sorted(first.sans) == [hostname], \
            f"the request did not ask for this app's name alone: {first.sans}"

        for status in ("pending", "issuing", "active"):
            first.status = status
            first.save()
            again = webapp_serving.request_dedicated_certificate(web_app, opts.admin)
            assert again.pk == first.pk, \
                f"a second press in state {status} minted a second certificate row"
            assert request.call_count == 1, (
                f"a second press in state {status} started another ACME order — "
                "an active row makes request_certificate RAISE, so this is the "
                "press-twice failure the pre-scan exists to stop")


@th.django_unit_test("a dedicated certificate is refused where the issuer could not honour it")
def test_dedicated_certificate_refused_on_a_delegated_domain(opts):
    from mojo.apps.edge.services import webapp_serving

    web_app, _, _, _, _ = _app(opts, with_alias=False, provider="route53")
    with mock.patch("mojo.apps.dnsman.services.delegation.for_domain",
                    return_value=mock.Mock()), \
            mock.patch("mojo.apps.dnsman.services.certs.request_certificate") as request:
        error = raises(webapp_serving.request_dedicated_certificate,
                       web_app, opts.admin)
    assert error is not None, \
        "a delegated-ACME domain accepted an exact-name certificate request"
    assert "whole domain" in str(error), \
        f"the refusal was not the plain shared-certificate sentence: {error}"
    request.assert_not_called()


@th.django_unit_test("ordering a certificate on an ancestor's domain needs authority in that workspace")
def test_dedicated_certificate_needs_domain_owning_group_authority(opts):
    from mojo.apps.account.models import Group
    from mojo import errors as me
    from mojo.apps.edge.services import webapp_serving

    parent = make_group("edge-serving-parent")
    child = Group.objects.create(
        name=f"edge-serving-child_{uuid.uuid4().hex[:8]}",
        kind="organization", parent=parent)
    domain = make_domain(group=parent, provider="route53")
    web_app, _, _, _, _ = _app(
        opts, group=child, domain=domain, with_alias=False)

    child_actor, _, _, _ = make_group_member(
        ["manage_webapp", "manage_dns"], group=child)
    with mock.patch("mojo.apps.dnsman.services.certs.request_certificate") as request:
        error = raises(webapp_serving.request_dedicated_certificate,
                       web_app, child_actor)
        assert isinstance(error, me.PermissionDeniedException), \
            f"a child-only grant ordered a certificate in an ancestor's zone: {error!r}"
        assert "workspace that owns it" in str(error), \
            f"the refusal was not the plain owning-workspace message: {error}"
        request.assert_not_called()

    parent_actor, _, _, _ = make_group_member(
        ["manage_webapp", "manage_dns"], group=parent)
    with mock.patch("mojo.apps.dnsman.services.certs.request_certificate") as request:
        webapp_serving.request_dedicated_certificate(web_app, parent_actor)
        request.assert_called_once()


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------
@th.django_unit_test("adding and removing a path applies to every address at once")
def test_route_writes_apply_to_every_address(opts):
    from mojo.apps.edge.models import VhostRoute
    from mojo.apps.edge.services import webapp_serving

    web_app, _, _, primary, alias = _app(opts)
    webapp_serving.add_route(web_app, "/reports", opts.upstream.pk)

    for vhost, role in ((primary, "own"), (alias, "extra")):
        assert VhostRoute.objects.filter(
            vhost=vhost, path_prefix="/reports").exists(), \
            f"the new path never landed on the app's {role} address"

    webapp_serving.remove_route(web_app, "/reports")
    for vhost, role in ((primary, "own"), (alias, "extra")):
        assert not VhostRoute.objects.filter(
            vhost=vhost, path_prefix="/reports").exists(), \
            f"the removed path is still serving on the app's {role} address"

    error = raises(webapp_serving.remove_route, web_app, "/reports")
    assert error is not None, "removing a path that is not set up reported success"


@th.django_unit_test("the platform's own sign-in paths cannot be added, moved or removed")
def test_managed_prefixes_are_refused(opts):
    from mojo.apps.edge.models import VhostRoute
    from mojo.apps.edge.services import webapp_auth_routes, webapp_serving

    web_app, _, _, primary, alias = _app(opts)
    _install_managed_routes(primary, opts.upstream)
    _install_managed_routes(alias, opts.upstream)
    other = make_upstream(group=opts.group)

    for prefix in webapp_auth_routes.auth_route_prefixes():
        error = raises(webapp_serving.add_route, web_app, prefix, other.pk)
        assert error is not None, \
            f"{prefix} was repointed at another destination — sign-in would break"
        assert "handled for you" in str(error), \
            f"the refusal for {prefix} was not a plain sentence: {error}"
        error = raises(webapp_serving.remove_route, web_app, prefix)
        assert error is not None, f"{prefix} was deletable — sign-in would 404"
        assert VhostRoute.objects.filter(
            vhost=primary, path_prefix=prefix,
            upstream=opts.upstream).exists(), \
            f"the refused write still changed {prefix}"


@th.django_unit_test("a path may only go to a destination this app is allowed to reach")
def test_route_destination_must_be_declared_and_reachable(opts):
    from mojo.apps.edge.models import VhostRoute
    from mojo.apps.edge.services import webapp_serving

    web_app, _, _, primary, _ = _app(opts)

    disabled = make_upstream()
    disabled.is_enabled = False
    disabled.save()
    assert raises(webapp_serving.add_route, web_app, "/off", disabled.pk) is not None, \
        "a disabled destination was accepted"

    stranger = make_group("edge-serving-stranger")
    foreign = make_upstream(group=stranger)
    assert raises(webapp_serving.add_route, web_app, "/theirs", foreign.pk) is not None, \
        "another tenant's destination was accepted — this app would proxy into it"

    assert raises(webapp_serving.add_route, web_app, "/gone", 999_000_999) is not None, \
        "an unknown destination id was accepted"
    assert not VhostRoute.objects.filter(vhost=primary).exists(), \
        "a refused route still landed on the app"


@th.django_unit_test("an app on an ancestor's domain cannot route to the ancestor org's upstream")
def test_route_destination_is_scoped_to_the_apps_own_group(opts):
    # The app owner's write authority is in the CHILD group; the domain — and so
    # `_accessible_upstreams` — belongs to the PARENT. If the picklist were
    # keyed on the domain's group, a child-group operator could point a public
    # path at the parent org's private backend. Where app group and domain group
    # diverge, only house upstreams are offered and routable.
    from mojo.apps.account.models import Group
    from mojo.apps.edge.models import VhostRoute
    from mojo.apps.edge.services import webapp_serving

    parent = make_group("edge-serving-up-parent")
    child = Group.objects.create(
        name=f"edge-serving-up-child_{uuid.uuid4().hex[:8]}",
        kind="organization", parent=parent)
    domain = make_domain(group=parent, provider="route53")
    web_app, _, _, primary, _ = _app(
        opts, group=child, domain=domain, with_alias=False)

    ancestor_upstream = make_upstream(group=parent)
    house_upstream = make_upstream()  # no group — shared, routable anywhere

    listed = {row["id"] for row in webapp_serving.serving_for(
        web_app, include_editables=True)["upstreams"]}
    assert ancestor_upstream.pk not in listed, \
        "the ancestor org's upstream was offered in the app's own picklist"
    assert house_upstream.pk in listed, \
        "a shared (house) upstream was hidden — the scope over-narrowed"

    assert raises(webapp_serving.add_route, web_app, "/into-parent",
                  ancestor_upstream.pk) is not None, \
        "a child-group app routed into the ancestor org's private backend"
    assert not VhostRoute.objects.filter(
        vhost=primary, path_prefix="/into-parent").exists(), \
        "the refused ancestor route still landed on the app"

    webapp_serving.add_route(web_app, "/shared", house_upstream.pk)
    assert VhostRoute.objects.filter(
        vhost=primary, path_prefix="/shared", upstream=house_upstream).exists(), \
        "the app could not route to a shared house destination"


@th.django_unit_test("a path already pointing somewhere else is not silently repointed")
def test_add_route_refuses_a_conflicting_prefix(opts):
    from mojo.apps.edge.models import VhostRoute
    from mojo.apps.edge.services import webapp_serving

    web_app, _, _, primary, _ = _app(opts)
    second = make_upstream(group=opts.group)
    webapp_serving.add_route(web_app, "/reports", opts.upstream.pk)

    error = raises(webapp_serving.add_route, web_app, "/reports", second.pk)
    assert error is not None, \
        "an existing path was silently repointed at a different destination"
    assert "already goes somewhere else" in str(error), \
        f"the refusal was not the plain conflicting-path sentence: {error}"
    row = VhostRoute.objects.get(vhost=primary, path_prefix="/reports")
    assert row.upstream_id == opts.upstream.pk, \
        "the refused add still moved the existing path"

    # Re-adding the SAME destination is idempotent, not an error.
    webapp_serving.add_route(web_app, "/reports", opts.upstream.pk)
    assert VhostRoute.objects.filter(
        vhost=primary, path_prefix="/reports").count() == 1, \
        "re-adding an identical path duplicated it"


@th.django_unit_test("a static-only app has no paths to send elsewhere")
def test_routes_refused_on_a_static_only_app(opts):
    from mojo.apps.edge.services import webapp_serving

    web_app, _, _, _, _ = _app(opts, kind="site", with_alias=False)
    payload = webapp_serving.serving_for(web_app, include_editables=True)
    assert payload["serving"]["routes_supported"] is False, \
        "a static-only app claimed it could route paths"
    error = raises(webapp_serving.add_route, web_app, "/api2", opts.upstream.pk)
    assert error is not None, \
        "a static-only app accepted a path it has no renderer branch for"
    assert "straight from your build" in str(error), \
        f"the refusal was not the plain static-only sentence: {error}"


# ---------------------------------------------------------------------------
# fail closed
# ---------------------------------------------------------------------------
WRITES = (
    ("/api/edge/webapp/serving", {"pool": "staging"}),
    ("/api/edge/webapp/certificate", {}),
    ("/api/edge/webapp/add_route", {"path_prefix": "/x", "upstream": 0}),
    ("/api/edge/webapp/remove_route", {"path_prefix": "/x"}),
)


@th.django_unit_test("no serving change is reachable from a CI deployment key")
def test_serving_writes_refuse_a_key_backed_session(opts):
    from mojo.apps.edge.services import webapp_keys

    web_app, _, _, _, _ = _app(opts, with_alias=False)
    _, _, token, _ = webapp_keys.link(web_app)
    _use_apikey(opts, token)
    try:
        for path, payload in WRITES:
            resp = opts.client.post(path, json=dict(webapp=web_app.pk, **payload))
            assert resp.status_code == 403, (
                f"a CI deployment key reached {path}; serving changes are "
                f"human-only by construction (status {resp.status_code})")
        read = opts.client.get(f"/api/edge/webapp/serving?webapp={web_app.pk}")
        assert read.status_code == 403, \
            f"a CI deployment key read the serving tab (status {read.status_code})"
    finally:
        _clear_apikey(opts)


@th.django_unit_test("a read-only viewer may look at serving and change nothing")
def test_serving_writes_refuse_a_view_only_viewer(opts):
    web_app, _, _, _, _ = _app(opts, with_alias=False)
    login(opts, opts.viewer_email, opts.viewer_pw)
    for path, payload in WRITES:
        resp = opts.client.post(path, json=dict(webapp=web_app.pk, **payload))
        assert resp.status_code in (401, 403), (
            f"a view_dns-only holder changed serving through {path} "
            f"(status {resp.status_code})")


@th.django_unit_test("group-scoped authority does not reach another group's app")
def test_serving_writes_refuse_a_member_of_another_group(opts):
    web_app, _, _, _, _ = _app(opts, with_alias=False)
    stranger = make_group("edge-serving-outsider")
    _, email, password, _ = make_group_member(
        ["manage_webapp", "manage_dns"], group=stranger)
    login(opts, email, password)
    for path, payload in WRITES:
        # `group=` is what carries their OWN grant past the verb gate; the
        # object check must then rebind to the app's real tenant and refuse.
        resp = opts.client.post(path, json=dict(
            webapp=web_app.pk, group=stranger.pk, **payload))
        assert resp.status_code in (401, 403, 404), (
            f"a member of another group changed serving through {path} "
            f"(status {resp.status_code})")
