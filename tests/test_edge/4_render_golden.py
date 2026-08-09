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
    cleanup, make_certificate, make_domain, make_group,
    make_route, make_upstream, make_vhost, raises,
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
    """security=[] — no rows, so the STRUCTURAL shape is pinned: the maps
    and watch plumbing render (empty) even on a deployment with no
    blocklist, because every server block's guards reference them."""
    from mojo.apps.edge.services import render

    _compare("http_base.conf", render.render_http_base(security=[]))


@th.django_unit_test("golden: the http base with the default-server catch-alls")
def test_golden_http_base_default(opts):
    from mojo.apps.edge.services import render

    knobs = render.http_knobs()
    knobs["default_server"] = True
    _compare("http_base_default.conf",
             render.render_http_base(knobs, security=[]))


@th.django_unit_test("golden: the http base with blocklist rows of every mode")
def test_golden_http_base_blocklists(opts):
    """Fixed synthetic rows with pinned ids — allow-first ordering, both
    geo blocks, both ua maps, and the off row's absence, all as bytes."""
    from mojo.apps.edge.services import render

    rows = [
        dict(id=81, kind="ua", value="^Lynx", mode="allow"),
        dict(id=82, kind="ua", value="badbot", mode="enforce"),
        dict(id=83, kind="ua", value="watchbot", mode="log"),
        dict(id=84, kind="ua", value="offbot", mode="off"),
        dict(id=85, kind="ip", value="192.0.2.1/32", mode="allow"),
        dict(id=86, kind="ip", value="203.0.113.0/24", mode="enforce"),
        dict(id=87, kind="ip", value="198.51.100.7/32", mode="log"),
    ]
    # `off` rows never reach the render input in production
    # (blocklist_payload excludes them); mirror that here.
    rows = [row for row in rows if row["mode"] != "off"]
    _compare("http_base_blocklists.conf", render.render_http_base(security=rows))


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


def _listen_split(line):
    """(port, params) of a `listen` directive line.

    The port is parsed as a TOKEN out of the address field — substring
    checks are a trap (`61443` contains `443`, `61080` contains `80`, and
    the upstreams file legitimately carries `127.0.0.1:8000`).
    """
    tokens = line.strip().rstrip(";").split()
    address = tokens[1]
    port = address.rsplit(":", 1)[-1] if ":" in address else address
    return int(port), tokens[2:]


@th.django_unit_test("staged variant: every listen remapped, nothing else moves")
def test_staged_variant_remaps_only_listens(opts):
    """The staged `nginx -t` runs unprivileged and nginx binds every listen
    during -t, so the staged copies must carry no privileged port — while
    every non-listen byte stays identical, or the pre-filter validates
    something other than what swaps in (item 1623)."""
    from mojo.apps.edge.services import render

    rendered = [
        _render(opts, label="sv-www", kind="site"),
        _render(opts, label="sv-api", kind="api", upstream=opts.http_upstream),
        _render(opts, label="sv-mix", kind="site_api",
                routes=[("/api", opts.http_upstream)]),
        _render(opts, label="sv-old", kind="redirect",
                redirect_to="www.edge-golden.example.com"),
    ]
    knobs = render.http_knobs()
    knobs["default_server"] = True
    rendered.append(render.render_http_base(knobs, security=[]))

    staged_ports = {render.staged_http_port(), render.staged_https_port()}
    for text in rendered:
        staged = render.render_staged_variant(text)
        before_lines = text.split("\n")
        after_lines = staged.split("\n")
        assert len(before_lines) == len(after_lines), \
            "the staged variant added or dropped lines"
        listens = 0
        for before, after in zip(before_lines, after_lines):
            if before.strip().startswith("access_log"):
                assert after.strip() == "access_log off;", \
                    "the unprivileged staged check must not open protected production logs"
                continue
            if not before.strip().startswith("listen"):
                assert before == after, (
                    f"a non-listen line moved in the staged variant:\n"
                    f"  before: {before!r}\n  after:  {after!r}")
                continue
            listens += 1
            real_port, real_params = _listen_split(before)
            staged_port, staged_params = _listen_split(after)
            assert real_port in (443, 80), \
                f"unexpected real listen port {real_port}: {before!r}"
            assert staged_port in staged_ports, (
                f"staged listen port {staged_port} is not a configured "
                f"staged port: {after!r}")
            assert staged_port >= 1024, \
                f"staged listen is still privileged: {after!r}"
            assert real_params == staged_params, (
                f"listen parameters moved in the staged variant:\n"
                f"  before: {before!r}\n  after:  {after!r}")
            assert ("[::]" in before) == ("[::]" in after), \
                f"the address family moved in the staged variant: {after!r}"
        assert listens > 0, "a rendered server file carried no listen at all"

    # The upstreams file has no listen lines and must pass through untouched.
    ups = render.render_upstreams([opts.http_upstream, opts.unix_upstream])
    assert render.render_staged_variant(ups) == ups, \
        "the staged variant altered the upstreams file"


@th.django_unit_test("staged variant: an unknown listen shape is refused")
def test_staged_variant_fail_closed(opts):
    """A builder growing a new listen shape must teach the staged remap
    before it can ship — an unmapped line renders nothing, loudly."""
    from mojo.apps.edge.services import render

    err = raises(render.render_staged_variant,
                 "server {\n    listen 8443 ssl;\n}\n")
    assert err is not None, (
        "an unknown listen line was silently accepted — a new listen shape "
        "would reach the staged check with a privileged or unmapped bind")

    # Whitespace spelling must not dodge the refusal: the guard matches the
    # first TOKEN, so a tab-separated listen still refuses rather than
    # passing through unremapped (and binding 443 where the OS allows it).
    err = raises(render.render_staged_variant,
                 "server {\n    listen\t443 ssl;\n}\n")
    assert err is not None, \
        "a tab-spelled listen line slipped past the staged remap unrenamed"


@th.django_unit_test("staged ports: bad settings are refused by name")
def test_staged_ports_refused(opts):
    """A privileged, equal, or non-numeric staged port is an operator error
    the render refuses outright — ≤1023 silently reintroduces the EACCES
    bind failure the staged tree exists to avoid."""
    from unittest import mock

    from mojo import errors as me
    from mojo.apps.edge.services import render

    cases = [
        ("privileged", {"EDGE_STAGED_HTTP_PORT": 80}),
        ("out-of-range", {"EDGE_STAGED_HTTPS_PORT": 70000}),
        ("equal", {"EDGE_STAGED_HTTP_PORT": 61443}),
        ("non-numeric", {"EDGE_STAGED_HTTPS_PORT": "junk"}),
    ]
    for label, overrides in cases:
        def fake_static(name, default=None, kind=None, _o=overrides):
            return _o.get(name, default)

        with mock.patch.object(render.settings, "get_static",
                               side_effect=fake_static):
            err = raises(render.staged_http_port)
        assert err is not None, f"a {label} staged port was accepted"
        assert isinstance(err, me.ValueException), (
            f"a {label} staged port raised {type(err).__name__}, not the "
            "named ValueException refusal")
