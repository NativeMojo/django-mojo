"""
Cache policy for file-serving vhosts (#8669, #8670).

Pages are always rechecked (`no-cache`); a file whose name carries a build hash
is cached for a year; any other static file is rechecked. A static file missing
from the current release falls back to the two releases served before it, so a
tab opened before a deploy can still load its lazy chunks. Pages never fall
back.

String asserts on `render.render_vhost`, as `3_render_injection.py` does.
"""

import pathlib
import re

from testit import helpers as th

from tests.test_edge._helpers import (
    declare_pools,
    cleanup, make_certificate, make_domain, make_group,
    make_route, make_upstream, make_vhost, raises,
)


GENERATION = "a" * 64
IMMUTABLE = 'add_header Cache-Control "public, max-age=31536000, immutable" always;'
NO_CACHE = 'add_header Cache-Control "no-cache" always;'
SECURITY_HEADERS = (
    "add_header X-Content-Type-Options",
    "add_header X-Frame-Options",
    "add_header Referrer-Policy",
    "add_header Strict-Transport-Security",
)
GENERAL_STATIC = ("location ~* \\.(css|js|mjs|map|ico|svg|gif|png|jpe?g|webp"
                  "|avif|woff2?|ttf|otf|eot)$ {")
VENDORED_ASSETS = (pathlib.Path(__file__).resolve().parents[2]
                   / "mojo/apps/account/admin_portal_v2/assets")


@th.django_unit_setup()
def setup_cache_policy(opts):
    cleanup()
    declare_pools()
    opts.group = make_group("edgecache")
    opts.domain = make_domain(name="edge-cache-example.com", group=opts.group)
    opts.certificate = make_certificate(opts.domain)
    opts.upstream = make_upstream(host="127.0.0.1", port=8000)


def _render(opts, routes=None, **kwargs):
    from django.db.models import prefetch_related_objects

    from mojo.apps.edge.models import Vhost
    from mojo.apps.edge.services import render

    vhost = make_vhost(opts.domain, opts.certificate, is_enabled=False, **kwargs)
    for prefix in routes or []:
        make_route(vhost, prefix, opts.upstream)
    vhost.is_enabled = True
    vhost.save()
    vhost = (Vhost.objects.select_related("domain", "certificate", "upstream")
             .get(pk=vhost.pk))
    prefetch_related_objects([vhost], "routes__upstream")
    return vhost, render.render_vhost(vhost, GENERATION)


def _blocks(text):
    """Every `location ... { ... }` block, by brace depth."""
    found = []
    for match in re.finditer(r"^\s*location [^\n]*\{\s*$", text, re.M):
        # Count from the opener's LAST brace: the hashed pattern holds `{8,}`.
        depth, i = 0, text.rindex("{", match.start(), match.end())
        while i < len(text):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        found.append(text[match.start():i + 1])
    return found


def _block(text, opener):
    for block in _blocks(text):
        if block.strip().startswith(opener):
            return block
    return None


def _location_root(text):
    return _block(text, "location / {")


@th.django_unit_test("SPA site: pages are no-cache with the security headers")
def test_spa_page_no_cache(opts):
    _vhost, text = _render(opts, label="spacache", kind="site", spa=True)
    page = _location_root(text)
    assert page is not None, f"no `location / {{` block:\n{text}"
    assert NO_CACHE in page, f"the page location sends no Cache-Control:\n{page}"
    for header in SECURITY_HEADERS:
        assert header in page, f"{header} missing from the page location"
    assert "try_files $uri $uri/ /index.html;" in page


@th.django_unit_test("non-SPA site: pages are no-cache, 404 page stays server-level")
def test_site_page_no_cache(opts):
    _vhost, text = _render(opts, label="sitecache", kind="site")
    page = _location_root(text)
    assert page is not None and NO_CACHE in page, (
        f"the page location sends no Cache-Control:\n{page}")
    assert "try_files $uri $uri/ =404;" in page
    assert "    error_page 404 /404.html;" in text
    assert "error_page" not in page, "404 page moved into a location"


@th.django_unit_test("site_api: pages no-cache, route locations keep no-store")
def test_site_api_page_and_routes(opts):
    _vhost, text = _render(opts, routes=["/api/v1"], label="siteapicache",
                           kind="site_api")
    page = _location_root(text)
    assert page is not None and NO_CACHE in page
    route = _block(text, "location ^~ /api/v1 {")
    assert route is not None, f"route location missing:\n{text}"
    assert "no-store, no-cache, must-revalidate, max-age=0" in route, (
        f"the proxied route lost its no-store header:\n{route}")


@th.django_unit_test("hashed assets: immutable, and checked before the general static regex")
def test_hashed_location_first(opts):
    from mojo.apps.edge.services import render

    _vhost, text = _render(opts, label="hashcache", kind="site", spa=True)
    opener = f'location ~ "{render.HASHED_ASSET_RE}" {{'
    hashed = _block(text, opener)
    assert hashed is not None, f"no hashed-asset location:\n{text}"
    assert IMMUTABLE in hashed
    assert text.index(opener) < text.index(GENERAL_STATIC), (
        "the hashed location must come before the general static regex: "
        "nginx uses the first regex that matches")


@th.django_unit_test("unhashed static files are no-cache, never immutable")
def test_general_static_no_cache(opts):
    _vhost, text = _render(opts, label="staticcache", kind="site", spa=True)
    general = _block(text, GENERAL_STATIC)
    assert general is not None, f"no general static location:\n{text}"
    assert NO_CACHE in general and "immutable" not in general, (
        f"an unhashed file is still cached for a year:\n{general}")


@th.django_unit_test("static files fall back to .prev1 then .prev2, then 404")
def test_fallback_chain(opts):
    from mojo.apps.edge.services import render

    vhost, text = _render(opts, label="chaincache", kind="site", spa=True)
    www = render.www_dir(GENERATION, vhost.pk)
    for name, cache in (("edge_hashed", IMMUTABLE), ("edge_static", NO_CACHE)):
        first = _block(text, f"location @{name}_prev1 {{")
        second = _block(text, f"location @{name}_prev2 {{")
        assert first is not None and second is not None, (
            f"@{name} fallback locations missing:\n{text}")
        assert f"root {www}.prev1;" in first
        assert f"root {www}.prev2;" in second
        assert f"try_files $uri @{name}_prev2;" in first
        assert "try_files $uri =404;" in second
        assert cache in first and cache in second
        assert f"try_files $uri @{name}_prev1;" in text
    assert "@edge_" not in _location_root(text), "pages must never fall back"


@th.django_unit_test("every block that sets a header re-emits the security headers")
def test_every_header_block_has_security_headers(opts):
    for kind, extra in (("site", {"spa": True}), ("site", {}),
                        ("site_api", {})):
        _vhost, text = _render(opts, label=f"hdr{kind.replace('_', '')}"
                               f"{len(extra)}", kind=kind, **extra)
        for block in _blocks(text):
            if "add_header" not in block:
                continue
            for header in SECURITY_HEADERS:
                assert header in block, (
                    f"{header} missing (add_header replaces, it does not "
                    f"merge):\n{block}")


@th.django_unit_test("the hashed-name pattern classifies real build output")
def test_hashed_name_classification(opts):
    from mojo.apps.edge.services import render

    pattern = re.compile(render.HASHED_ASSET_RE)
    names = sorted(p.name for p in VENDORED_ASSETS.iterdir())
    assert len(names) > 100, f"vendored Admin build not found at {VENDORED_ASSETS}"
    missed = [n for n in names if not pattern.search(f"/assets/{n}")]
    assert not missed, f"real hashed build files read as unhashed: {missed}"
    for name in ("config.js", "env.js", "favicon.ico", "sw.js",
                 "service-worker.js", "registerSW.js", "vendor-libraries.js",
                 "jquery-3.7.1.min.js", "index.html", "about-Ab12cd34.html",
                 "bootstrap-icons.woff2", "app.bundle.js"):
        assert not pattern.search(f"/{name}"), f"{name} read as hashed"
    for name in ("workbox-a1b2c3d4.js", "index-0kbck4JK.js",
                 "EventsPage-Dys7Mm-b.js"):
        assert pattern.search(f"/assets/{name}"), f"{name} read as unhashed"


@th.django_unit_test("api and redirect vhosts serve no files and have no fallback")
def test_api_and_redirect_unchanged(opts):
    _vhost, api = _render(opts, label="apicache", kind="api",
                          upstream=opts.upstream)
    _vhost, redirect = _render(opts, label="oldcache", kind="redirect",
                               redirect_to="www.edge-cache-example.com")
    for text in (api, redirect):
        # `\n    root ` is a server-level web root; the ACME webroot is
        # nested deeper and is not a file-serving root.
        assert "\n    root " not in text and "@edge_" not in text


@th.django_unit_test("fallback dirs refuse an unknown slot or a non-int id")
def test_fallback_dir_refuses(opts):
    from mojo.apps.edge.services import render

    assert raises(render.www_fallback_dir, GENERATION, 4242, 3) is not None
    assert raises(render.www_fallback_dir, GENERATION, "../x", 1) is not None
    assert render.www_fallback_dir(GENERATION, 4242, 1).endswith("/www/4242.prev1")
