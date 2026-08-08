"""
Golden-file rendering.

The workspec asks for this so an accidental template change shows up as a
reviewable diff rather than silently altering every deployment's web server.
That is the entire value: these assertions are not clever, and they are not
supposed to be.

Regenerating: set EDGE_GOLDEN_REWRITE=1 and run this module. **Read the diff
before committing it** — a rewrite is exactly as dangerous as the silent change
this file exists to catch.
"""

import os
import pathlib

from testit import helpers as th

from tests.test_edge._helpers import (
    declare_pools,
    cleanup, declare_reserved_names, make_certificate, make_domain, make_group,
    make_route, make_upstream, make_vhost,
)


GOLDEN_DIR = pathlib.Path(__file__).parent / "golden"

# Fixed so the rendered paths are stable across runs.
GENERATION = "a" * 64


def _compare(name, actual):
    GOLDEN_DIR.mkdir(exist_ok=True)
    path = GOLDEN_DIR / name

    if os.environ.get("EDGE_GOLDEN_REWRITE") == "1":
        path.write_text(actual)
        return

    assert path.exists(), (
        f"golden file {name} is missing — run with EDGE_GOLDEN_REWRITE=1 to "
        f"create it, then READ the diff before committing")
    expected = path.read_text()
    assert actual == expected, (
        f"rendered output drifted from {name}.\n"
        f"--- expected ---\n{expected}\n--- actual ---\n{actual}")


@th.django_unit_setup()
def setup_golden(opts):
    cleanup()
    declare_reserved_names()
    declare_pools()
    opts.group = make_group("edgegolden")
    # A FIXED domain name: the golden files contain it verbatim.
    opts.domain = make_domain(name="edge-golden.example.com", group=opts.group)
    opts.certificate = make_certificate(opts.domain)
    opts.http_upstream = make_upstream(
        name="up-golden-http", host="127.0.0.1", port=8000)
    opts.unix_upstream = make_upstream(
        name="up-golden-unix", kind="unix", socket_path="/run/mojo/golden.sock")


def _pin_upstream_ids(vhost):
    """Upstream pks are autoincrement and land in the `edge_up_<pk>` names,
    so they get pinned like the vhost pk does: distinct upstreams in
    creation (pk) order become 71, 72, ...

    Only safe on FRESH instances — `_render` refetches before calling this,
    so the shared fixtures in `opts` are never mutated.
    """
    rows = {}
    if vhost.upstream_id:
        rows.setdefault(vhost.upstream.pk, []).append(vhost.upstream)
    for route in vhost.routes.all():
        rows.setdefault(route.upstream.pk, []).append(route.upstream)
    for index, original in enumerate(sorted(rows), start=71):
        for row in rows[original]:
            row.pk = index


def _render(opts, routes=None, **kwargs):
    """Render one vhost with FIXED ids so paths are reproducible.

    The vhost pk, the certificate id and every referenced upstream pk are
    autoincrement and land in the rendered text (paths and `edge_up_<pk>`
    names), so all are swapped for constants after the row is built.
    Everything else — the TLS floor, the header block, the location shape —
    is genuinely rendered, which is what the golden file is protecting.

    `routes` ([(prefix, upstream), ...]) are created against the REAL pk;
    the vhost is then REFETCHED (so pinning ids cannot corrupt the shared
    fixtures) and its routes prefetched, so the renderer's `routes.all()`
    serves the cache rather than querying for the fixed pk.
    """
    from django.db.models import prefetch_related_objects

    from mojo.apps.edge.models import Vhost
    from mojo.apps.edge.services import render

    vhost = make_vhost(opts.domain, opts.certificate, **kwargs)
    for prefix, upstream in routes or []:
        make_route(vhost, prefix, upstream)
    vhost = (Vhost.objects
             .select_related("domain", "certificate", "upstream")
             .get(pk=vhost.pk))
    prefetch_related_objects([vhost], "routes__upstream")
    vhost.pk = 4242
    vhost.certificate_id = 99
    _pin_upstream_ids(vhost)
    return render.render_vhost(vhost, GENERATION)


@th.django_unit_test("golden: site vhost")
def test_golden_site(opts):
    _compare("site.conf", _render(opts, label="www", kind="site"))


@th.django_unit_test("golden: spa-flagged site vhost")
def test_golden_site_spa(opts):
    _compare("site_spa.conf", _render(opts, label="app", kind="site", spa=True))


@th.django_unit_test("golden: api vhost over http")
def test_golden_api_http(opts):
    _compare("api.conf", _render(
        opts, label="api", kind="api", upstream=opts.http_upstream))


@th.django_unit_test("golden: api vhost over a unix socket")
def test_golden_api_unix(opts):
    _compare("api_unix.conf", _render(
        opts, label="sock", kind="api", upstream=opts.unix_upstream))


@th.django_unit_test("golden: api vhost with every knob turned")
def test_golden_api_knobs(opts):
    _compare("api_knobs.conf", _render(
        opts, label="knobs", kind="api", upstream=opts.http_upstream,
        body_size_mb=200, quiet_paths=["/healthz", "/api/status"],
        serve_static=True))


@th.django_unit_test("golden: site_api vhost with routes and a quiet path")
def test_golden_site_api(opts):
    _compare("site_api.conf", _render(
        opts, label="mixed", kind="site_api", is_enabled=False,
        routes=[("/api", opts.http_upstream), ("/ws", opts.unix_upstream)],
        quiet_paths=[]))


@th.django_unit_test("golden: redirect vhost")
def test_golden_redirect(opts):
    _compare("redirect.conf", _render(
        opts, label="old", kind="redirect",
        redirect_to="www.edge-golden.example.com"))


@th.django_unit_test("golden: apex and wildcard names")
def test_golden_apex_and_wildcard(opts):
    _compare("apex.conf", _render(opts, label="", kind="site"))
    _compare("wildcard.conf", _render(opts, label="*", kind="site"))


@th.django_unit_test("golden: the staging nginx harness")
def test_golden_harness(opts):
    from mojo.apps.edge.services import render

    _compare("harness.conf", render.render_nginx_harness(GENERATION))


@th.django_unit_test("golden: the rendered http base")
def test_golden_http_base(opts):
    from mojo.apps.edge.services import render

    _compare("http_base.conf", render.render_http_base())


@th.django_unit_test("golden: the http base with the default-server catch-alls")
def test_golden_http_base_default(opts):
    from mojo.apps.edge.services import render

    knobs = render.http_knobs()
    knobs["default_server"] = True
    _compare("http_base_default.conf", render.render_http_base(knobs))


@th.django_unit_test("golden: the upstreams file")
def test_golden_upstreams(opts):
    """Named blocks with pinned pks — the only file carrying literal
    targets; every vhost file references these names."""
    from mojo.apps.edge.models import Upstream
    from mojo.apps.edge.services import render

    http = Upstream.objects.get(pk=opts.http_upstream.pk)
    unix = Upstream.objects.get(pk=opts.unix_upstream.pk)
    http.pk, unix.pk = 71, 72
    _compare("upstreams.conf", render.render_upstreams([http, unix]))
