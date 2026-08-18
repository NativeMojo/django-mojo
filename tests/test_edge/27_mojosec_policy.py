from testit import helpers as th

from tests.test_edge._helpers import (
    cleanup, declare_pools, make_certificate, make_domain, make_group, make_vhost,
    raises,
)


@th.django_unit_setup()
def setup_mojosec_policy(opts):
    cleanup()
    declare_pools()
    opts.group = make_group("edgemojosec")
    opts.domain = make_domain(group=opts.group)
    opts.certificate = make_certificate(opts.domain)
    opts.generation = "9" * 64


@th.django_unit_test()
def test_vhost_policy_is_opt_in_versioned_and_fail_closed(opts):
    from mojo.apps.edge.models import Vhost
    from mojo.apps.edge.services import render

    legacy = make_vhost(opts.domain, opts.certificate, label="legacy", kind="site", spa=True)
    legacy_text = render.render_vhost(legacy, opts.generation)
    th.assert_true("MojoSec impossible-path family" not in legacy_text,
                   "an existing vhost must retain behavior until explicitly configured")

    policy = {
        "version": 7,
        "impossible_path_families": ["wordpress", "secret_files"],
        "response_class": "spa_fallback",
    }
    guarded = make_vhost(
        opts.domain, opts.certificate, label="guarded", kind="site", spa=True,
        mojosec_policy=policy)
    text = render.render_vhost(guarded, opts.generation)
    th.assert_in("MojoSec impossible-path family: wordpress", text,
                 "the configured registered family must render an edge rejection")
    th.assert_in("return 404;", text,
                 "impossible paths must be rejected at the serving edge")
    th.assert_in("try_files $uri $uri/ /index.html;", text,
                 "ordinary SPA history fallback must remain intact")

    invalid = Vhost(
        domain=opts.domain, certificate=opts.certificate, label="invalid",
        kind="site", mojosec_policy={
            "version": 1, "impossible_path_families": ["arbitrary-regex"],
            "response_class": "content_guessed_from_length",
        })
    th.assert_true(raises(invalid.save) is not None,
                   "unregistered families/classes must fail closed before rendering")


@th.django_unit_test()
def test_edge_log_emits_only_registered_response_identity(opts):
    from mojo.apps.edge.services import render

    guarded = make_vhost(
        opts.domain, opts.certificate, label="evidence", kind="site", spa=True,
        mojosec_policy={
            "version": 2, "impossible_path_families": ["wordpress"],
            "response_class": "spa_fallback",
        })
    knobs = render.http_knobs()
    knobs["mojosec_mode"] = "observe"
    text = render.render_http_base(knobs, security=[], vhosts=[guarded])
    for field in ("response_class", "resource_id", "edge_policy_version"):
        th.assert_in(f'"{field}"', text,
                     f"the security stream must emit trusted bounded {field}")
    th.assert_in(f'vhost:{guarded.pk}', text,
                 "resource identity must be server-derived from the VHost primary key")
    for forbidden in ("$request_body", "$http_cookie", "$http_authorization"):
        th.assert_true(forbidden not in text,
                       f"the edge evidence stream must exclude {forbidden}")
