"""
edge models — the invariants that hold even when nobody calls the validators.

Two layers are asserted separately on purpose:

- **`save()`** validates every write path, so a service-layer or shell write is
  as safe as a REST one.
- **DB constraints** hold when `save()` is bypassed entirely. Each of those
  tests uses a queryset `.update()`, which never calls `Model.save()` — that is
  the real bypass, not a contrived one, and it is how a future bulk operation
  would reach the table.
"""

from testit import helpers as th

from tests.test_edge._helpers import (
    declare_pools,
    API_HOSTNAME, cleanup, declare_reserved_names, make_certificate,
    make_domain, make_group, make_route, make_upstream, make_vhost, raises,
)


@th.django_unit_setup()
def setup_models(opts):
    cleanup()
    declare_reserved_names()
    declare_pools()
    opts.group = make_group("edgemodel")
    opts.domain = make_domain(group=opts.group)
    opts.certificate = make_certificate(opts.domain)
    opts.upstream = make_upstream(host="127.0.0.1", port=8000)


@th.django_unit_test("a static vhost stores and derives its server name")
def test_vhost_basics(opts):
    vhost = make_vhost(opts.domain, opts.certificate, label="www")

    assert vhost.server_name == f"www.{opts.domain.name}", \
        f"server_name derived wrongly: {vhost.server_name}"
    assert vhost.web_root_name == str(vhost.pk), \
        "the web root must be the row's own pk, not a caller-supplied string"


@th.django_unit_test("a proxy vhost requires an upstream and a static one forbids it")
def test_kind_upstream_pairing(opts):
    from mojo.apps.edge.models import Vhost

    err = raises(
        Vhost.objects.create, domain=opts.domain, certificate=opts.certificate,
        label="noups", kind="api", upstream=None)
    assert err is not None, "an api vhost was created with no upstream"

    err = raises(
        Vhost.objects.create, domain=opts.domain, certificate=opts.certificate,
        label="badsite", kind="site", upstream=opts.upstream)
    assert err is not None, "a site vhost was allowed to carry an upstream"


@th.django_unit_test("the kind/upstream pairing survives a save() bypass")
def test_kind_upstream_db_constraint(opts):
    """A queryset update never calls Model.save(), so only the DB stands here."""
    from django.db import IntegrityError, transaction

    from mojo.apps.edge.models import Vhost

    vhost = make_vhost(opts.domain, opts.certificate, label="dbpair",
                       kind="api", upstream=opts.upstream)
    err = None
    try:
        with transaction.atomic():
            Vhost.objects.filter(pk=vhost.pk).update(upstream=None)
    except IntegrityError as caught:
        err = caught
    assert err is not None, \
        "the DB allowed an api vhost with no upstream — the CheckConstraint is not enforcing"


@th.django_unit_test("the kind matrix refuses knobs on kinds they do not apply to")
def test_kind_matrix(opts):
    from mojo.apps.edge.models import Vhost

    cases = [
        # (label, kwargs, why)
        ("mxspa", dict(kind="api", upstream=opts.upstream, spa=True),
         "spa applies to site/site_api only"),
        ("mxstat", dict(kind="site", serve_static=True),
         "serve_static applies to api/site_api only"),
        ("mxquiet", dict(kind="site", quiet_paths=["/health"]),
         "quiet_paths applies to api/site_api only"),
        ("mxredir", dict(kind="site", redirect_to="www.example.com"),
         "a site vhost has no redirect target"),
        ("mxbody", dict(kind="site", body_size_mb=0),
         "body_size_mb below 1"),
        ("mxbody2", dict(kind="site", body_size_mb=5000),
         "body_size_mb above 4096"),
        ("mxdupq", dict(kind="api", upstream=opts.upstream,
                        quiet_paths=["/health", "/health"]),
         "duplicate quiet paths"),
        ("mxkind", dict(kind="proxy", upstream=opts.upstream),
         "the old kind names are gone"),
    ]
    for label, kwargs, why in cases:
        err = raises(
            Vhost.objects.create, domain=opts.domain,
            certificate=opts.certificate, label=label, **kwargs)
        assert err is not None, f"the kind matrix let this through: {why}"


@th.django_unit_test("a redirect vhost requires a target and the DB enforces the pairing")
def test_redirect_pairing(opts):
    from django.db import IntegrityError, transaction

    from mojo.apps.edge.models import Vhost

    err = raises(
        Vhost.objects.create, domain=opts.domain, certificate=opts.certificate,
        label="redirnone", kind="redirect", redirect_to=None)
    assert err is not None, "a redirect vhost was created with no target"

    # The DB constraint holds when save() is bypassed entirely.
    vhost = make_vhost(opts.domain, opts.certificate, label="redirdb")
    err = None
    try:
        with transaction.atomic():
            Vhost.objects.filter(pk=vhost.pk).update(kind="redirect")
    except IntegrityError as caught:
        err = caught
    assert err is not None, (
        "the DB allowed a redirect vhost with no target — "
        "edge_vhost_kind_fields is not enforcing")


@th.django_unit_test("every kind can be enabled once its fields pair correctly")
def test_all_kinds_enable(opts):
    cases = [
        ("enapi", dict(kind="api", upstream=opts.upstream)),
        ("ensite", dict(kind="site")),
        ("enspa", dict(kind="site", spa=True)),
        ("enboth", dict(kind="site_api")),
        ("enredir", dict(kind="redirect", redirect_to="www.example.com")),
    ]
    for label, kwargs in cases:
        err = raises(make_vhost, opts.domain, opts.certificate,
                     label=label, **kwargs)
        assert err is None, \
            f"an enabled {kwargs['kind']} vhost was refused: {err}"


@th.django_unit_test("routes attach only to site_api vhosts and stay in-tenant")
def test_route_rules(opts):
    from mojo.apps.edge.models import VhostRoute

    site_api = make_vhost(
        opts.domain, opts.certificate, label="rapi", kind="site_api",
        is_enabled=False)
    plain = make_vhost(opts.domain, opts.certificate, label="rsite")

    err = raises(
        VhostRoute.objects.create, vhost=plain, path_prefix="/api",
        upstream=opts.upstream)
    assert err is not None, "a route attached to a plain site vhost"

    route = VhostRoute.objects.create(
        vhost=site_api, path_prefix="/api", upstream=opts.upstream)
    assert route.pk, "a legitimate route failed to save"

    err = raises(
        VhostRoute.objects.create, vhost=site_api, path_prefix="/api",
        upstream=opts.upstream)
    assert err is not None, \
        "two routes claimed the same prefix on one vhost"

    other_group = make_group("edgeroute")
    foreign = make_upstream(group=other_group)
    err = raises(
        VhostRoute.objects.create, vhost=site_api, path_prefix="/other",
        upstream=foreign)
    assert err is not None, (
        "a route proxied into ANOTHER tenant's upstream — that is the "
        "cross-tenant read the FK check exists to stop")

    # A kind change cannot strand the routes.
    site_api.kind = "site"
    err = raises(site_api.save)
    assert err is not None, \
        "a vhost changed kind while still carrying routes"


@th.django_unit_test("site_api quiet paths must sit under a declared route prefix")
def test_site_api_quiet_path_coverage(opts):
    from mojo.apps.edge.models import Vhost

    vhost = make_vhost(
        opts.domain, opts.certificate, label="qcov", kind="site_api",
        is_enabled=False)
    make_route(vhost, "/api", opts.upstream)

    vhost.quiet_paths = ["/api/health"]
    err = raises(vhost.save)
    assert err is None, \
        f"a quiet path under a declared prefix was refused: {err}"

    vhost.quiet_paths = ["/uncovered/health"]
    err = raises(vhost.save)
    assert err is not None, \
        "a quiet path outside every route prefix was accepted — it silences nothing"
    assert "/api" in str(err), \
        f"the refusal must name the declared prefix set, got: {err}"

    # An api vhost's quiet paths are whole-host; no coverage rule applies.
    api = make_vhost(
        opts.domain, opts.certificate, label="qapi", kind="api",
        upstream=opts.upstream, quiet_paths=["/anything"])
    assert api.pk, "an api vhost with quiet paths failed to save"


@th.django_unit_test("two enabled vhosts cannot claim the same server name")
def test_enabled_name_uniqueness(opts):
    from mojo.apps.edge.models import Vhost

    make_vhost(opts.domain, opts.certificate, label="dup")
    err = raises(
        Vhost.objects.create, domain=opts.domain, certificate=opts.certificate,
        label="dup", kind="site")
    assert err is not None, \
        "two enabled vhosts claimed the same name — nginx would silently drop one"


@th.django_unit_test("a disabled vhost may sit alongside an enabled one")
def test_disabled_may_shadow(opts):
    """Staging a replacement must be possible; only ENABLED rows collide."""
    from mojo.apps.edge.models import Vhost

    make_vhost(opts.domain, opts.certificate, label="staged")
    err = raises(
        Vhost.objects.create, domain=opts.domain, certificate=opts.certificate,
        label="staged", kind="site", is_enabled=False)
    assert err is None, \
        f"a disabled duplicate was refused, so a replacement cannot be staged: {err}"


@th.django_unit_test("a vhost cannot claim the API's own hostname")
def test_vhost_cannot_shadow_api(opts):
    """The shadowing attack, at the model layer.

    The API hostname is reserved, so even a domain whose name IS that hostname
    cannot be served. Note this is the check that matters — `nginx -t` treats a
    duplicate server_name as a warning and exits 0.
    """
    from mojo.apps.edge.models import Vhost

    hostile = make_domain(name=API_HOSTNAME, group=opts.group)
    certificate = make_certificate(hostile)
    err = raises(
        Vhost.objects.create, domain=hostile, certificate=certificate,
        label="", kind="site")
    assert err is not None, \
        "a tenant vhost claimed the API's own hostname"


@th.django_unit_test("a certificate from another domain cannot be attached")
def test_certificate_must_match_domain(opts):
    from mojo.apps.edge.models import Vhost

    other_domain = make_domain(group=opts.group)
    other_cert = make_certificate(other_domain)

    err = raises(
        Vhost.objects.create, domain=opts.domain, certificate=other_cert,
        label="wrongcert", kind="site")
    assert err is not None, \
        "a vhost attached a certificate belonging to a different domain"


@th.django_unit_test("a certificate that does not cover the name is refused")
def test_certificate_must_cover_name(opts):
    from mojo.apps.edge.models import Vhost

    # Apex-only certificate: covers example.com, not a.b.example.com.
    narrow = make_certificate(
        opts.domain, common_name=opts.domain.name, sans=[opts.domain.name])
    err = raises(
        Vhost.objects.create, domain=opts.domain, certificate=narrow,
        label="deep", kind="site")
    assert err is not None, \
        "a vhost was enabled with a certificate that does not cover its name"

    err = raises(
        Vhost.objects.create, domain=opts.domain, certificate=narrow,
        label="", kind="site")
    assert err is None, \
        f"the apex vhost should be covered by an apex certificate: {err}"


@th.django_unit_test("a DISABLED vhost may hold a stale certificate")
def test_disabled_vhost_skips_coverage(opts):
    """Parking a vhost while its certificate is reissued must be possible."""
    from mojo.apps.edge.models import Vhost

    other_domain = make_domain(group=opts.group)
    other_cert = make_certificate(other_domain)
    err = raises(
        Vhost.objects.create, domain=opts.domain, certificate=other_cert,
        label="parked", kind="site", is_enabled=False)
    assert err is None, \
        f"a disabled vhost was blocked by an enable-time check: {err}"


@th.django_unit_test("an upstream's kind decides which fields it may carry")
def test_upstream_kind_fields(opts):
    from mojo.apps.edge.models import Upstream

    err = raises(
        Upstream.objects.create, name="up-badhttp", kind="http",
        host="127.0.0.1", port=8000, socket_path="/run/mojo/x.sock")
    assert err is not None, "an http upstream was allowed a socket path"

    err = raises(
        Upstream.objects.create, name="up-badunix", kind="unix",
        host="127.0.0.1", port=8000, socket_path="/run/mojo/x.sock")
    assert err is not None, "a unix upstream was allowed a host and port"


@th.django_unit_test("an upstream cannot be declared against the metadata service")
def test_upstream_metadata_refused(opts):
    from mojo.apps.edge.models import Upstream

    err = raises(
        Upstream.objects.create, name="up-imds", kind="http",
        host="169.254.169.254", port=80)
    assert err is not None, \
        "an upstream pointing at the instance metadata service was created"


@th.django_unit_test("two house upstreams cannot share a name")
def test_house_upstream_name_uniqueness(opts):
    """A NULL group does not collide in a plain unique index — the partial
    constraint is what keeps house names distinct."""
    from mojo.apps.edge.models import Upstream

    make_upstream(name="up-house1", group=None)
    err = raises(
        Upstream.objects.create, name="up-house1", group=None, kind="http",
        host="127.0.0.1", port=9000)
    assert err is not None, "two house upstreams shared a name"


@th.django_unit_test("the same name is fine in two different tenants")
def test_upstream_name_scoped_per_group(opts):
    group_a = make_group("upa")
    group_b = make_group("upb")
    make_upstream(name="up-shared", group=group_a)
    err = raises(make_upstream, name="up-shared", group=group_b)
    assert err is None, \
        f"two tenants were blocked from using the same upstream name: {err}"


@th.django_unit_test("an upstream target renders as nginx will name it")
def test_upstream_target(opts):
    http = make_upstream(host="10.0.0.5", port=8443)
    assert http.target == "10.0.0.5:8443", f"http target wrong: {http.target}"

    sock = make_upstream(kind="unix", socket_path="/run/mojo/app.sock")
    assert sock.target == "unix:/run/mojo/app.sock", \
        f"unix target wrong: {sock.target}"
