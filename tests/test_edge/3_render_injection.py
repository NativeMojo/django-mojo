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
    declare_pools,
    cleanup, make_certificate, make_domain, make_group,
    make_route, make_upstream, make_vhost, raises,
)


HOSTILE_DOMAIN = "example.com; } server { listen 443; root /etc; #"


@th.django_unit_setup()
def setup_render(opts):
    cleanup()
    declare_pools()
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
        label="", kind="site")
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


@th.django_unit_test("a rendered vhost emits exactly two server blocks (443 + 80)")
def test_exactly_two_server_blocks(opts):
    """The injection pin. One 443 block carrying the kind's contract, one
    port-80 ACME/redirect shell — a third block anywhere means a value
    escaped its whitelist."""
    from mojo.apps.edge.services import render

    cases = [
        ("two1", dict(kind="site")),
        ("two2", dict(kind="site", spa=True)),
        ("two3", dict(kind="api", upstream=opts.upstream,
                      quiet_paths=["/health"], serve_static=True)),
        ("two4", dict(kind="site_api")),
        ("two5", dict(kind="redirect", redirect_to="www.example.com")),
    ]
    for label, kwargs in cases:
        vhost = make_vhost(opts.domain, opts.certificate, label=label, **kwargs)
        text = render.render_vhost(vhost, opts.generation)
        assert text.count("server {") == 2, (
            f"{kwargs['kind']}: expected exactly two server blocks, "
            f"found {text.count('server {')}")
        assert "/etc" not in text, "the rendered config referenced /etc"
        assert "listen 80;" in text, \
            f"{kwargs['kind']}: the port-80 block is missing"
        assert "location /.well-known/acme-challenge/ {" in text, \
            f"{kwargs['kind']}: the ACME webroot location is missing"
        assert "return 301 https://$host$request_uri;" in text, \
            f"{kwargs['kind']}: the port-80 redirect is missing"
        assert f"client_max_body_size {vhost.body_size_mb}m;" in text, \
            f"{kwargs['kind']}: client_max_body_size is not rendered"


@th.django_unit_test("a site vhost never emits proxy_pass")
def test_site_never_proxies(opts):
    from mojo.apps.edge.services import render

    vhost = make_vhost(opts.domain, opts.certificate, label="stat", kind="site")
    text = render.render_vhost(vhost, opts.generation)

    assert "proxy_pass" not in text, \
        "a site vhost emitted proxy_pass"
    assert "root " in text, "a site vhost emitted no root"


@th.django_unit_test("an spa-flagged site falls back to index.html and never proxies")
def test_spa_shape(opts):
    from mojo.apps.edge.services import render

    vhost = make_vhost(opts.domain, opts.certificate, label="spa", kind="site", spa=True)
    text = render.render_vhost(vhost, opts.generation)

    assert "proxy_pass" not in text, "an spa site emitted proxy_pass"
    assert "try_files $uri $uri/ /index.html;" in text, \
        "an spa site has no history fallback"


@th.django_unit_test("an api vhost never emits a filesystem root")
def test_api_never_serves_files(opts):
    from mojo.apps.edge.services import render

    vhost = make_vhost(opts.domain, opts.certificate, label="prox",
                       kind="api", upstream=opts.upstream)
    text = render.render_vhost(vhost, opts.generation)

    assert f"proxy_pass http://edge_up_{opts.upstream.pk};" in text, \
        f"the proxy destination is wrong:\n{text}"
    assert "\n    root " not in text, "an api vhost emitted a filesystem root"
    assert "127.0.0.1" not in text, (
        "a literal upstream target leaked into a vhost file — targets live "
        "only in http.d/10_upstreams.conf")


@th.django_unit_test("a unix upstream renders nginx's socket syntax")
def test_unix_upstream_syntax(opts):
    """Inside an `upstream {}` block the socket is `server unix:/path;` —
    no trailing `:/`, which only the inline proxy_pass form needed."""
    from mojo.apps.edge.services import render

    sock = make_upstream(kind="unix", socket_path="/run/mojo/app.sock")
    vhost = make_vhost(opts.domain, opts.certificate, label="sock",
                       kind="api", upstream=sock)
    text = render.render_vhost(vhost, opts.generation)

    assert f"proxy_pass http://edge_up_{sock.pk};" in text, \
        f"unix-backed api vhost does not reference its named upstream:\n{text}"

    blocks = render.render_upstreams([sock])
    assert f"upstream edge_up_{sock.pk} {{" in blocks, \
        f"no named block for the unix upstream:\n{blocks}"
    assert "server unix:/run/mojo/app.sock;" in blocks, \
        f"unix socket server line is malformed:\n{blocks}"


@th.django_unit_test("every rendered vhost carries the TLS floor")
def test_tls_floor_present(opts):
    from mojo.apps.edge.services import render

    for kind, extra in (
            ("site", {}),
            ("api", dict(upstream=opts.upstream)),
            ("site_api", {}),
            ("redirect", dict(redirect_to="www.example.com"))):
        label = f"tls{kind.replace('_', '')}"
        vhost = make_vhost(opts.domain, opts.certificate, label=label,
                           kind=kind, **extra)
        text = render.render_vhost(vhost, opts.generation)
        assert f"ssl_protocols {render.tls_protocols()};" in text, \
            f"{kind} vhost is missing the TLS protocol floor"
        assert "ssl_ciphers " in text, f"{kind} vhost is missing the cipher floor"
        assert "ssl_prefer_server_ciphers off;" in text, \
            f"{kind} vhost is missing the cipher-preference setting"


@th.django_unit_test("a redirect vhost emits neither root nor proxy_pass")
def test_redirect_emits_only_the_redirect(opts):
    from mojo.apps.edge.services import render

    vhost = make_vhost(opts.domain, opts.certificate, label="redir",
                       kind="redirect", redirect_to="www.example.com")
    text = render.render_vhost(vhost, opts.generation)

    assert "proxy_pass" not in text, "a redirect vhost emitted proxy_pass"
    assert "\n    root " not in text, "a redirect vhost emitted a filesystem root"
    assert "return 301 https://www.example.com$request_uri;" in text, \
        f"the redirect target is wrong:\n{text}"


@th.django_unit_test("a site vhost never proxies even when route rows exist via bypass")
def test_site_refuses_routes(opts):
    """Kind isolation as a property of code shape: `proxy_pass` is not in the
    site builder's scope, so even routes smuggled past the validators by a
    queryset write cannot make a site block proxy."""
    from mojo.apps.edge.models import Vhost, VhostRoute
    from mojo.apps.edge.services import render

    vhost = make_vhost(opts.domain, opts.certificate, label="siteroutes",
                       kind="site_api", is_enabled=False)
    make_route(vhost, "/api", opts.upstream)
    # A queryset update bypasses save() and the routes-forbidden check.
    Vhost.objects.filter(pk=vhost.pk).update(kind="site")
    poisoned = (Vhost.objects.select_related("domain", "certificate")
                .prefetch_related("routes__upstream").get(pk=vhost.pk))

    text = render.render_vhost(poisoned, opts.generation)
    assert "proxy_pass" not in text, (
        "a site vhost with bypass-written routes emitted proxy_pass — "
        "kind isolation broke")
    VhostRoute.objects.filter(vhost_id=vhost.pk).delete()


@th.django_unit_test("api knobs render: quiet paths, static alias, upgrade headers")
def test_api_knob_shapes(opts):
    from mojo.apps.edge.services import render

    vhost = make_vhost(
        opts.domain, opts.certificate, label="knobs", kind="api",
        upstream=opts.upstream, body_size_mb=200,
        quiet_paths=["/healthz", "/api/status"], serve_static=True)
    text = render.render_vhost(vhost, opts.generation)

    assert "client_max_body_size 200m;" in text, \
        "body_size_mb did not reach client_max_body_size"
    for path in ("/healthz", "/api/status"):
        assert f"location = {path} {{" in text, \
            f"quiet path {path} has no exact-match location"
    # Each quiet location REPLACES the inherited access log with the watch
    # log only — quiet for the main log, never blind for the security watch.
    assert text.count("edge_watch.log edge_watch if=$edge_watch;") == 2, \
        "each quiet path must swap the main access log for the watch log"
    assert "access_log off" not in text, \
        "a quiet path silenced the security watch with `access_log off`"
    assert "location /static/ {" in text, "serve_static rendered no alias"
    assert "alias " in text, "the static location is not an alias"
    # Every proxied location carries the upgrade pair (three here: two quiet
    # paths plus the whole-host location; MojoSec is off by default).
    assert text.count("proxy_set_header Upgrade $http_upgrade;") == 3, \
        "a proxied location is missing the websocket upgrade header"
    assert text.count("proxy_set_header Connection $connection_upgrade;") == 3, \
        "a proxied location is missing the Connection upgrade header"


@th.django_unit_test("Edge MojoSec is opt-in and renders the same bounded stream")
def test_edge_mojosec_mode_contract(opts):
    from unittest import mock

    from mojo.apps.edge.services import render

    knobs = render.http_knobs()
    knobs["mojosec_mode"] = "off"
    off_base = render.render_http_base(knobs, security=[])
    assert "log_format mojosec_v1" not in off_base, \
        "Edge mode=off produced the noisy MojoSec security stream"

    knobs["mojosec_mode"] = "observe"
    knobs["mojosec_trusted_proxy_cidrs"] = ["10.0.0.0/8"]
    observed_base = render.render_http_base(knobs, security=[])
    assert "log_format mojosec_v1 escape=json" in observed_base, \
        "Edge observe mode omitted the structured security stream"
    security_log = observed_base[observed_base.index("# MojoSec"):
                                 observed_base.index("map $http_upgrade")]
    assert '"request_uri":"$request_uri"' in security_log, \
        "Edge security logging must use the shared bounded raw request target"
    assert '"user_agent":"$http_user_agent"' in security_log, \
        "Edge security logging must match the standard rich evidence renderer"
    assert '"request_length":"$request_length"' in security_log, \
        "Edge security logging must include the shared request-byte measurement"
    assert '"response_bytes":"$bytes_sent"' in security_log, \
        "Edge security logging must include the shared response-byte measurement"
    assert "access_log /var/log/nginx/mojosec.json.log mojosec_v1;" in security_log, \
        "Edge raw evidence must use the root-owned nginx master-opened path"
    assert "set_real_ip_from 10.0.0.0/8;" in observed_base, \
        "Edge observe mode omitted its exact trusted-proxy boundary"

    vhost = make_vhost(opts.domain, opts.certificate, label="mojosec",
                       kind="api", upstream=opts.upstream)
    with mock.patch.object(render, "mojosec_mode", return_value="observe"):
        text = render.render_vhost(vhost, opts.generation)
    assert "location = /api/incident/mojosec/batch {" in text, \
        "Edge observe mode omitted the exact receiver route"
    assert "location = /api/incident/mojosec/batch/ {" in text, \
        "Edge observe mode omitted the capped trailing-slash alias"
    assert text.count("client_max_body_size 512k;") == 2, \
        "both Edge receiver spellings need the compressed wire-body cap"


@th.django_unit_test("site_api renders one location per route, quiet paths on the longest prefix")
def test_site_api_shapes(opts):
    from mojo.apps.edge.services import render

    api_up = make_upstream(host="127.0.0.1", port=8100)
    ws_up = make_upstream(host="127.0.0.1", port=8200)
    vhost = make_vhost(opts.domain, opts.certificate, label="siteapi",
                       kind="site_api", is_enabled=False)
    make_route(vhost, "/api", api_up)
    make_route(vhost, "/api/ws", ws_up)
    vhost.quiet_paths = ["/api/ws/ping"]
    vhost.is_enabled = True
    vhost.save()
    vhost = (type(vhost).objects.select_related("domain", "certificate")
             .prefetch_related("routes__upstream").get(pk=vhost.pk))

    text = render.render_vhost(vhost, opts.generation)

    assert "location ^~ /api {" in text, "the /api route has no prefix location"
    assert "location ^~ /api/ws {" in text, \
        "the /api/ws route has no prefix location"
    assert f"proxy_pass http://edge_up_{api_up.pk};" in text, \
        "the /api route does not reach its upstream"
    assert f"proxy_pass http://edge_up_{ws_up.pk};" in text, \
        "the /api/ws route does not reach its upstream"
    assert "root " in text, "the site half of site_api is missing"

    # The quiet path must proxy to the LONGEST covering prefix's upstream —
    # /api/ws, not /api.
    quiet_at = text.index("location = /api/ws/ping {")
    quiet_block = text[quiet_at:text.index("}", quiet_at)]
    assert f"proxy_pass http://edge_up_{ws_up.pk};" in quiet_block, (
        "the quiet path proxied to the wrong route — longest-prefix "
        f"association broke:\n{quiet_block}")


@th.django_unit_test("site_api route and static prefixes outrank the site asset regex")
def test_site_api_prefixes_outrank_asset_regex(opts):
    from mojo.apps.edge.services import render

    vhost = make_vhost(
        opts.domain, opts.certificate, label="siteapiprefix", kind="site_api",
        serve_static=True, is_enabled=False)
    make_route(vhost, "/api/account", opts.upstream)
    vhost.is_enabled = True
    vhost.save()
    vhost = (type(vhost).objects.select_related("domain", "certificate")
             .prefetch_related("routes__upstream").get(pk=vhost.pk))

    text = render.render_vhost(vhost, opts.generation)

    assert "location ^~ /api/account {" in text, (
        "site_api route prefixes must outrank the asset-suffix regex so "
        "proxied asset requests do not fall through to the release root")
    assert "location ^~ /static/ {" in text, (
        "the site_api static alias must outrank the asset-suffix regex so "
        "Django static assets do not fall through to the release root")
    assert "location ~* \\.(css|js|mjs|map|ico|svg|gif|png|jpe?g|webp|avif|woff2?|ttf|otf|eot)$ {" in text, (
        "the regression requires the site asset-cache regex to remain present")


@th.django_unit_test("WebApp auth renders exact legacy honeypots without capturing SPA signin")
def test_webapp_auth_honeypots_are_exact(opts):
    from mojo.apps.edge.services import render, webapp_auth_routes
    from tests.test_edge._helpers import declare_release_buckets, make_webapp

    declare_release_buckets()
    vhost = make_vhost(
        opts.domain, opts.certificate, label="authapp", kind="site_api",
        spa=True, is_enabled=False)
    for prefix in webapp_auth_routes.auth_route_prefixes():
        make_route(vhost, prefix, opts.upstream)
    make_webapp(opts.group, slug="authrender", vhost=vhost)
    vhost.is_enabled = True
    vhost.save()
    vhost = (type(vhost).objects.select_related(
        "domain", "certificate", "web_app")
        .prefetch_related("routes__upstream").get(pk=vhost.pk))

    text = render.render_vhost(vhost, opts.generation)

    for path in webapp_auth_routes.HONEYPOT_PATHS:
        assert f"location = {path} {{" in text, \
            f"legacy honeypot {path} is missing its exact proxy route"
        assert f"location ^~ {path} {{" not in text, \
            f"legacy honeypot {path} became a prefix and captures app pages"
    assert "try_files $uri $uri/ /index.html;" in text, \
        "WebApp auth routes removed the SPA fallback used by /signin/login"

    payload = render.vhost_payload(vhost)
    assert payload["webapp_auth"]["honeypots"] == list(
        webapp_auth_routes.HONEYPOT_PATHS), \
        "renderer-owned honeypots are absent from the generation hash input"


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

    # `domain` is writable but settable ONCE — NO_SAVE_FIELDS is enforced on
    # create too, so pinning it there would make a vhost un-creatable over
    # REST; `Vhost.on_rest_pre_save` freezes it after create instead.
    #
    # Every knob listed here is whitelist-validated before it renders — see
    # validators.validate_vhost — and none can reach the TLS block.
    assert writable == {"domain", "label", "kind", "upstream", "certificate",
                        "pool", "is_enabled", "spa", "body_size_mb",
                        "quiet_paths", "serve_static", "mojosec_policy",
                        "redirect_to"}, (
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

    vhost.spa = True
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
