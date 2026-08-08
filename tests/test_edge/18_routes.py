"""
VhostRoute REST — tenancy through the vhost chain, and the house guard.

A route is the only place a tenant hands the platform a (path, destination)
pair, so the surface mirrors the vhost endpoints exactly: tenancy resolves
through `vhost__domain__group`, house rows are platform-only in both
directions (read AND create), and the destination is always a declared
`Upstream` the caller's tenant may see.
"""

from testit import helpers as th

from tests.test_edge._helpers import (
    declare_pools,
    cleanup, declare_reserved_names, login, make_certificate, make_domain,
    make_group, make_group_member, make_route, make_upstream, make_user,
    make_vhost,
)


@th.django_unit_setup()
def setup_routes(opts):
    cleanup()
    declare_reserved_names()
    declare_pools()

    opts.group = make_group("edgeroutes")
    opts.other_group = make_group("edgeroutesb")

    _, opts.tenant_email, opts.tenant_pw, _ = make_group_member(
        ["manage_dns"], group=opts.group)
    opts.manager, opts.manager_email, opts.manager_pw = make_user(["manage_dns"])
    opts.admin, opts.admin_email, opts.admin_pw = make_user(
        ["manage_dns"], is_superuser=True)

    opts.domain = make_domain(group=opts.group)
    opts.certificate = make_certificate(opts.domain)
    opts.vhost = make_vhost(opts.domain, opts.certificate, label="app",
                            kind="site_api")

    opts.other_domain = make_domain(group=opts.other_group)
    opts.other_cert = make_certificate(opts.other_domain)
    opts.other_vhost = make_vhost(opts.other_domain, opts.other_cert,
                                  label="app", kind="site_api")

    opts.house_domain = make_domain(group=None)
    opts.house_cert = make_certificate(opts.house_domain)
    opts.house_vhost = make_vhost(opts.house_domain, opts.house_cert,
                                  label="app", kind="site_api")

    opts.shared_upstream = make_upstream(host="127.0.0.1", port=8300)
    opts.tenant_upstream = make_upstream(
        group=opts.group, host="127.0.0.1", port=8301)
    opts.other_upstream = make_upstream(
        group=opts.other_group, host="127.0.0.1", port=8302)

    opts.route = make_route(opts.vhost, "/api", opts.shared_upstream)
    opts.other_route = make_route(
        opts.other_vhost, "/api", opts.other_upstream)
    opts.house_route = make_route(
        opts.house_vhost, "/api", opts.shared_upstream)


@th.django_unit_test("anonymous callers are refused on the route surface")
def test_anonymous_refused(opts):
    opts.client.logout()
    for path in ["/api/edge/route", f"/api/edge/route/{opts.route.pk}"]:
        resp = opts.client.get(path)
        assert resp.status_code in (401, 403), \
            f"{path} served an anonymous caller (status {resp.status_code})"


@th.django_unit_test("a tenant admin creates a route on their own site_api vhost")
def test_tenant_creates_route(opts):
    login(opts, opts.tenant_email, opts.tenant_pw)
    resp = opts.client.post(f"/api/edge/route?group={opts.group.pk}", json=dict(
        vhost=opts.vhost.pk, path_prefix="/api/v2",
        upstream=opts.tenant_upstream.pk))
    assert resp.status_code == 200, (
        f"a tenant admin could not create a route: {resp.status_code} "
        f"{resp.body}")

    from mojo.apps.edge.models import VhostRoute
    assert VhostRoute.objects.filter(
        vhost=opts.vhost, path_prefix="/api/v2").exists(), \
        "the route did not land"


@th.django_unit_test("a hostile path prefix is rejected over REST, not escaped")
def test_hostile_prefix_rejected(opts):
    login(opts, opts.tenant_email, opts.tenant_pw)
    for prefix in ["/api;", "/api\n", "/api{", "/", "api", "/a/../b"]:
        resp = opts.client.post(
            f"/api/edge/route?group={opts.group.pk}", json=dict(
                vhost=opts.vhost.pk, path_prefix=prefix,
                upstream=opts.tenant_upstream.pk))
        assert resp.status_code not in (200, 201), (
            f"a hostile prefix {prefix!r} was accepted over REST "
            f"(status {resp.status_code})")


@th.django_unit_test("a route cannot point at another tenant's upstream")
def test_route_cross_tenant_upstream_refused(opts):
    login(opts, opts.tenant_email, opts.tenant_pw)
    resp = opts.client.post(f"/api/edge/route?group={opts.group.pk}", json=dict(
        vhost=opts.vhost.pk, path_prefix="/steal",
        upstream=opts.other_upstream.pk))
    assert resp.status_code not in (200, 201), (
        "a tenant routed their prefix into ANOTHER tenant's upstream "
        f"(status {resp.status_code})")


@th.django_unit_test("a route cannot be created on another tenant's vhost")
def test_route_cross_tenant_vhost_refused(opts):
    login(opts, opts.tenant_email, opts.tenant_pw)
    resp = opts.client.post(f"/api/edge/route?group={opts.group.pk}", json=dict(
        vhost=opts.other_vhost.pk, path_prefix="/hijack",
        upstream=opts.shared_upstream.pk))
    assert resp.status_code not in (200, 201), (
        "a tenant attached a route to another tenant's vhost "
        f"(status {resp.status_code})")

    from mojo.apps.edge.models import VhostRoute
    assert not VhostRoute.objects.filter(
        vhost=opts.other_vhost, path_prefix="/hijack").exists(), \
        "the cross-tenant route landed despite the refusal"


@th.django_unit_test("a route list is scoped to the caller's tenant")
def test_route_list_scoped(opts):
    login(opts, opts.tenant_email, opts.tenant_pw)
    resp = opts.client.get(f"/api/edge/route?group={opts.group.pk}")
    assert resp.status_code == 200, \
        f"a tenant admin could not list their routes: {resp.status_code}"

    ids = {row.get("id") for row in (resp.json.get("data") or [])}
    assert opts.route.pk in ids, "the tenant's own route is missing"
    assert opts.other_route.pk not in ids, \
        "another tenant's route appeared in a scoped list"
    assert opts.house_route.pk not in ids, \
        "a house route appeared in a tenant's list"


@th.django_unit_test("a global manage_dns grant does not reach a HOUSE route")
def test_house_route_guard(opts):
    login(opts, opts.manager_email, opts.manager_pw)
    resp = opts.client.get(f"/api/edge/route/{opts.house_route.pk}")
    assert resp.status_code in (401, 403), (
        "a global manage_dns holder read a route on the platform's own vhost "
        f"(status {resp.status_code})")


@th.django_unit_test("a global manage_dns grant cannot CREATE a route on a house vhost")
def test_house_route_create_guard(opts):
    login(opts, opts.manager_email, opts.manager_pw)
    resp = opts.client.post("/api/edge/route", json=dict(
        vhost=opts.house_vhost.pk, path_prefix="/phish",
        upstream=opts.shared_upstream.pk))
    assert resp.status_code in (401, 403), (
        "a global manage_dns holder bolted a proxied prefix onto the "
        f"platform's own site (status {resp.status_code})")

    from mojo.apps.edge.models import VhostRoute
    assert not VhostRoute.objects.filter(
        vhost=opts.house_vhost, path_prefix="/phish").exists(), \
        "the house route landed despite the refusal"


@th.django_unit_test("a platform admin manages house routes")
def test_house_route_admin_allowed(opts):
    login(opts, opts.admin_email, opts.admin_pw)
    resp = opts.client.get(f"/api/edge/route/{opts.house_route.pk}")
    assert resp.status_code == 200, \
        f"a platform admin could not read a house route: {resp.status_code}"

    resp = opts.client.post("/api/edge/route", json=dict(
        vhost=opts.house_vhost.pk, path_prefix="/admin-api",
        upstream=opts.shared_upstream.pk))
    assert resp.status_code == 200, (
        f"a platform admin could not create a house route: "
        f"{resp.status_code} {resp.body}")


@th.django_unit_test("an unauthenticated caller cannot tell a house route from a tenant one")
def test_house_route_guard_is_not_an_oracle(opts):
    opts.client.logout()
    house = opts.client.get(f"/api/edge/route/{opts.house_route.pk}")
    tenant = opts.client.get(f"/api/edge/route/{opts.route.pk}")
    assert house.status_code == tenant.status_code, (
        "a house route and a tenant route answered an anonymous caller "
        f"differently ({house.status_code} vs {tenant.status_code})")


@th.django_unit_test("deleting a route works and deleting its upstream is PROTECTed")
def test_route_delete_and_upstream_protect(opts):
    from mojo.apps.edge.models import VhostRoute

    protected = make_upstream(host="127.0.0.1", port=8399)
    route = make_route(opts.vhost, "/deleteme", protected)

    err = None
    try:
        protected.delete()
    except Exception as caught:
        err = caught
    assert err is not None, \
        "an upstream was deleted while a route still pointed at it"

    login(opts, opts.tenant_email, opts.tenant_pw)
    resp = opts.client.delete(
        f"/api/edge/route/{route.pk}?group={opts.group.pk}")
    assert resp.status_code == 200, \
        f"a tenant admin could not delete their route: {resp.status_code}"
    assert not VhostRoute.objects.filter(pk=route.pk).exists(), \
        "the route survived its delete"
