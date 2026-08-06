"""
The renderer is security-critical — these are the tests that say so.

Item #1433's workspec is explicit that the point is **preventing** bad configs,
not detecting them, and that operator-controllable values must be **rejected**
rather than escaped. Every test here asserts on rendered text or on a raise;
none of them asserts that a backslash appeared somewhere.

The canonical case is pinned twice: once at the model layer (a hostile
`Domain.name` cannot produce a second `server {`) and once at the renderer
boundary (a row mutated straight into the DB still cannot).
"""

from testit import helpers as th

from tests.test_edge._helpers import (
    cleanup, declare_reserved_names, make_certificate, make_domain, make_group,
    make_upstream, make_vhost, raises,
)


HOSTILE_DOMAIN = "example.com; } server { listen 443; root /etc; #"


@th.django_unit_setup()
def setup_render(opts):
    cleanup()
    declare_reserved_names()
    opts.group = make_group("edgerender")
    opts.domain = make_domain(name="edge-render-example.com", group=opts.group)
    opts.certificate = make_certificate(opts.domain)
    opts.upstream = make_upstream(host="127.0.0.1", port=8000)
    opts.generation = "0" * 64


@th.django_unit_test("a vhost cannot be created on a domain carrying nginx syntax")
def test_canonical_injection_case(opts):
    """The workspec's canonical case, asserted where it is actually decidable.

    Scoping assumed `Domain.name` could never hold this, because the column is
    documented as normalised. It can: `Domain` normalises in
    `on_rest_pre_save`, which is the REST path ONLY — a row created through the
    ORM, a service, a migration or a shell is not normalised. This test found
    that, and `validators.validate_server_name` now checks the whole derived
    name rather than trusting the domain half.

    So the domain row is storable (that is dnsman's business), and the vhost on
    it is not.
    """
    from mojo.apps.dnsman.models import Domain
    from mojo.apps.edge.models import Vhost

    Domain.objects.filter(name=HOSTILE_DOMAIN).delete()
    hostile = Domain.objects.create(
        name=HOSTILE_DOMAIN, group=opts.group, provider="godaddy",
        status="active", verified=True)
    certificate = make_certificate(
        hostile, common_name=HOSTILE_DOMAIN, sans=[HOSTILE_DOMAIN])

    err = raises(
        Vhost.objects.create, domain=hostile, certificate=certificate,
        label="", kind="static")
    assert err is not None, (
        "a vhost was created on a domain carrying nginx syntax — "
        "that name would reach a config file")

    Domain.objects.filter(pk=hostile.pk).delete()


@th.django_unit_test("even a DB-mutated hostile name is refused at the render boundary")
def test_render_boundary_rejects_hostile_name(opts):
    """A queryset update bypasses every model validator.

    This is the belt-and-braces half of the design: the renderer re-asserts,
    so a future bulk write or a hand-run SQL statement still cannot produce a
    file with two server blocks in it.
    """
    from mojo.apps.dnsman.models import Domain
    from mojo.apps.edge.models import Vhost
    from mojo.apps.edge.services import render

    vhost = make_vhost(opts.domain, opts.certificate, label="inject")
    Domain.objects.filter(pk=opts.domain.pk).update(name=HOSTILE_DOMAIN)
    poisoned = Vhost.objects.select_related("domain", "certificate").get(pk=vhost.pk)

    err = raises(render.render_vhost, poisoned, opts.generation)
    assert err is not None, (
        "the renderer accepted a hostile server name — "
        "re-assertion at the boundary is not happening")

    # Restore so later tests in this module see a sane domain.
    Domain.objects.filter(pk=opts.domain.pk).update(name="edge-render-example.com")


@th.django_unit_test("a rendered vhost emits exactly one server block")
def test_exactly_one_server_block(opts):
    from mojo.apps.edge.services import render

    vhost = make_vhost(opts.domain, opts.certificate, label="one")
    text = render.render_vhost(vhost, opts.generation)

    assert text.count("server {") == 1, \
        f"expected exactly one server block, found {text.count('server {')}"
    assert "/etc" not in text, "the rendered config referenced /etc"


@th.django_unit_test("a static vhost never emits proxy_pass")
def test_static_never_proxies(opts):
    from mojo.apps.edge.services import render

    vhost = make_vhost(opts.domain, opts.certificate, label="stat", kind="static")
    text = render.render_vhost(vhost, opts.generation)

    assert "proxy_pass" not in text, \
        "a static vhost emitted proxy_pass"
    assert "root " in text, "a static vhost emitted no root"


@th.django_unit_test("an spa vhost falls back to index.html and never proxies")
def test_spa_shape(opts):
    from mojo.apps.edge.services import render

    vhost = make_vhost(opts.domain, opts.certificate, label="spa", kind="spa")
    text = render.render_vhost(vhost, opts.generation)

    assert "proxy_pass" not in text, "an spa vhost emitted proxy_pass"
    assert "try_files $uri $uri/ /index.html;" in text, \
        "an spa vhost has no history fallback"


@th.django_unit_test("a proxy vhost never emits a filesystem root")
def test_proxy_never_serves_files(opts):
    from mojo.apps.edge.services import render

    vhost = make_vhost(opts.domain, opts.certificate, label="prox",
                       kind="proxy", upstream=opts.upstream)
    text = render.render_vhost(vhost, opts.generation)

    assert "proxy_pass http://127.0.0.1:8000;" in text, \
        f"the proxy destination is wrong:\n{text}"
    assert "\n    root " not in text, "a proxy vhost emitted a filesystem root"


@th.django_unit_test("a unix upstream renders nginx's socket syntax")
def test_unix_upstream_syntax(opts):
    """`http://unix:/path:/` — the trailing `:/` is not optional."""
    from mojo.apps.edge.services import render

    sock = make_upstream(kind="unix", socket_path="/run/mojo/app.sock")
    vhost = make_vhost(opts.domain, opts.certificate, label="sock",
                       kind="proxy", upstream=sock)
    text = render.render_vhost(vhost, opts.generation)

    assert "proxy_pass http://unix:/run/mojo/app.sock:/;" in text, \
        f"unix socket proxy_pass is malformed:\n{text}"


@th.django_unit_test("every rendered vhost carries the TLS floor")
def test_tls_floor_present(opts):
    from mojo.apps.edge.services import render

    for kind, upstream in (("static", None), ("spa", None),
                           ("proxy", opts.upstream)):
        vhost = make_vhost(opts.domain, opts.certificate, label=f"tls{kind}",
                           kind=kind, upstream=upstream)
        text = render.render_vhost(vhost, opts.generation)
        assert f"ssl_protocols {render.tls_protocols()};" in text, \
            f"{kind} vhost is missing the TLS protocol floor"
        assert "ssl_ciphers " in text, f"{kind} vhost is missing the cipher floor"
        assert "ssl_prefer_server_ciphers off;" in text, \
            f"{kind} vhost is missing the cipher-preference setting"


@th.django_unit_test("no model field can weaken or remove the TLS floor")
def test_tls_floor_is_not_reachable(opts):
    """There is no field to try — that IS the assertion.

    If someone later adds a settable field to Vhost, this test starts failing,
    which is the moment to ask whether it can reach the TLS block.
    """
    from mojo.apps.edge.models import Vhost

    savable = set(Vhost.get_rest_meta_prop("NO_SAVE_FIELDS", []))
    fields = {f.name for f in Vhost._meta.get_fields() if hasattr(f, "attname")}
    writable = fields - savable - {"modified"}

    assert writable == {"label", "kind", "upstream", "certificate", "pool",
                        "is_enabled"}, (
        "Vhost's writable field set changed — confirm no new field can reach "
        f"the TLS block or the rendered paths. Now: {sorted(writable)}")


@th.django_unit_test("certificate paths point INSIDE the generation, not through current")
def test_cert_paths_are_generation_absolute(opts):
    """This is what lets `nginx -t` validate the NEW certificates.

    If the config referenced the `current` symlink instead, the staging check
    would validate the previous generation's material and the swap would not be
    atomic.
    """
    from mojo.apps.edge.services import render

    vhost = make_vhost(opts.domain, opts.certificate, label="paths")
    text = render.render_vhost(vhost, opts.generation)

    expected = render.cert_dir(opts.generation, opts.certificate.pk)
    assert f"ssl_certificate     {expected}/fullchain.pem;" in text, \
        f"certificate path is not generation-absolute:\n{text}"
    assert "/current/" not in text, \
        "the rendered config reaches through the `current` symlink"


@th.django_unit_test("the web root is the vhost pk, never a caller-supplied string")
def test_web_root_is_the_pk(opts):
    from mojo.apps.edge.services import render

    vhost = make_vhost(opts.domain, opts.certificate, label="rootpk")
    text = render.render_vhost(vhost, opts.generation)

    assert f"    root {render.www_dir(opts.generation, vhost.pk)};" in text, \
        f"the web root is not derived from the pk:\n{text}"


@th.django_unit_test("the generation id changes when a vhost changes")
def test_generation_id_moves(opts):
    from mojo.apps.edge.services import render

    vhost = make_vhost(opts.domain, opts.certificate, label="genid")
    first = render.desired_state([vhost])["generation"]

    same = render.desired_state([vhost])["generation"]
    assert first == same, "the generation id is not stable for identical input"

    vhost.kind = "spa"
    vhost.save()
    moved = render.desired_state([vhost])["generation"]
    assert moved != first, \
        "changing a vhost did not move the generation id — nodes would not converge"


@th.django_unit_test("the generation id covers the webapps key")
def test_generation_id_covers_webapps(opts):
    """Item #1435 adds releases to this payload. If the hash did not cover
    them, a promote would not trigger an install."""
    from mojo.apps.edge.services import render

    vhost = make_vhost(opts.domain, opts.certificate, label="genweb")
    without = render.desired_state([vhost])["generation"]
    with_app = render.desired_state(
        [vhost], webapps=[dict(vhost=vhost.pk, release="abc123")])["generation"]

    assert without != with_app, \
        "the generation hash ignores webapps — a promote would never reach a node"
