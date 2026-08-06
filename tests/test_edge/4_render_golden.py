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
    make_upstream, make_vhost,
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


def _render(opts, **kwargs):
    """Render one vhost with FIXED ids so paths are reproducible.

    Both the vhost pk and the certificate id are autoincrement and land in the
    rendered paths, so both are swapped for constants after the row is built.
    Everything else — the TLS floor, the header block, the location shape — is
    genuinely rendered, which is what the golden file is protecting.
    """
    from mojo.apps.edge.services import render

    vhost = make_vhost(opts.domain, opts.certificate, **kwargs)
    vhost.pk = 4242
    vhost.certificate_id = 99
    return render.render_vhost(vhost, GENERATION)


@th.django_unit_test("golden: static vhost")
def test_golden_static(opts):
    _compare("static.conf", _render(opts, label="www", kind="static"))


@th.django_unit_test("golden: spa vhost")
def test_golden_spa(opts):
    _compare("spa.conf", _render(opts, label="app", kind="spa"))


@th.django_unit_test("golden: proxy vhost over http")
def test_golden_proxy_http(opts):
    _compare("proxy_http.conf", _render(
        opts, label="api", kind="proxy", upstream=opts.http_upstream))


@th.django_unit_test("golden: proxy vhost over a unix socket")
def test_golden_proxy_unix(opts):
    _compare("proxy_unix.conf", _render(
        opts, label="sock", kind="proxy", upstream=opts.unix_upstream))


@th.django_unit_test("golden: apex and wildcard names")
def test_golden_apex_and_wildcard(opts):
    _compare("apex.conf", _render(opts, label="", kind="static"))
    _compare("wildcard.conf", _render(opts, label="*", kind="static"))


@th.django_unit_test("golden: the staging nginx harness")
def test_golden_harness(opts):
    from mojo.apps.edge.services import render

    _compare("harness.conf", render.render_nginx_harness(GENERATION))
