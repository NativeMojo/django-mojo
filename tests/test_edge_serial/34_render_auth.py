"""WebApp-auth renderer coverage that owns global release-bucket settings.

This contract creates a WebApp and therefore must declare
``EDGE_RELEASE_BUCKETS``.  It lives in the serial Edge package so a parallel
module cannot replace that process-global deployment setting between the
declaration and the model save.
"""

from testit import helpers as th

from tests.test_edge._helpers import (
    cleanup,
    declare_pools,
    declare_release_buckets,
    make_certificate,
    make_domain,
    make_group,
    make_route,
    make_upstream,
    make_vhost,
    make_webapp,
)


@th.django_unit_setup()
def setup_render_auth(opts):
    cleanup()
    declare_pools()
    declare_release_buckets()
    opts.group = make_group("edge-render-auth")
    opts.domain = make_domain(
        name="edge-render-auth.example.com", group=opts.group)
    opts.certificate = make_certificate(opts.domain)
    opts.upstream = make_upstream(host="127.0.0.1", port=8000)
    opts.generation = "0" * 64


@th.django_unit_test(
    "WebApp auth renders exact legacy honeypots without capturing SPA signin")
def test_webapp_auth_honeypots_are_exact(opts):
    from mojo.apps.edge.services import render, webapp_auth_routes

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
