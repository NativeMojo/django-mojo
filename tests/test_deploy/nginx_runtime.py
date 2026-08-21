"""Persistent nginx spill-path convergence and first-upgrade activation.

The pure rendering/parsing/decision contracts live here. The converge and
SELinux orchestration tests, which mock `mojo.deploy.nginx_runtime` internals
process-globally, moved to tests/test_deploy_extended_serial/nginx_runtime.py
(maestro #2558).
"""

import os
import shutil
import tempfile

from testit import helpers as th


@th.django_unit_test()
def test_mapping_and_fragment_cover_all_private_temp_directives(opts):
    from mojo.deploy import nginx_runtime as runtime

    expected = (
        ("client_body_temp_path", "client_body"),
        ("proxy_temp_path", "proxy"),
        ("fastcgi_temp_path", "fastcgi"),
        ("uwsgi_temp_path", "uwsgi"),
        ("scgi_temp_path", "scgi"),
    )
    th.assert_eq(runtime.TEMP_PATHS, expected,
                 "production and Edge staging must share one exact five-path mapping")
    text = runtime.render_http_fragment("/private/nginx")
    for directive, leaf in expected:
        line = f"{directive} /private/nginx/{leaf};"
        th.assert_eq(text.count(line), 1,
                     f"the global fragment must declare {line!r} exactly once")


@th.django_unit_test()
def test_worker_parser_requires_one_authoritative_identity(opts):
    from mojo.deploy import nginx_runtime as runtime

    th.assert_eq(runtime.parse_worker_user("user www;\nevents {}\n"), "www",
                 "nginx -T worker parsing must preserve the configured identity")
    for config in ("events {}\n", "user www;\nuser nginx;\n"):
        try:
            runtime.parse_worker_user(config)
        except runtime.NginxRuntimeError:
            pass
        else:
            assert False, f"ambiguous/missing nginx user must fail closed: {config!r}"


@th.django_unit_test()
def test_active_graph_requires_each_exact_directive_once(opts):
    from mojo.deploy import nginx_runtime as runtime

    root = "/var/lib/django-mojo/nginx"
    config = "user www;\nhttp {\n%s}\n" % runtime.render_http_fragment(
        root, indent="    ")
    runtime._verify_active(config, root)
    duplicate = config.replace(
        "http {\n", "http {\n    client_body_temp_path "
        "/var/lib/django-mojo/nginx/client_body;\n")
    try:
        runtime._verify_active(duplicate, root)
    except runtime.NginxRuntimeError as err:
        th.assert_in("got 2", str(err),
                     "duplicate active temp directives must be diagnosed exactly")
    else:
        assert False, "duplicate active nginx temp directives must fail convergence"


@th.django_unit_test()
def test_symlinked_runtime_ancestor_is_refused(opts):
    from mojo.deploy import nginx_runtime as runtime

    root = tempfile.mkdtemp(prefix="nginx-runtime-symlink-")
    try:
        real = os.path.join(root, "real")
        link = os.path.join(root, "link")
        os.mkdir(real)
        os.symlink(real, link)
        try:
            runtime._assert_absolute_safe(os.path.join(link, "client_body"))
        except runtime.NginxRuntimeError as err:
            th.assert_in("symlink", str(err),
                         "no-follow convergence must name a symlink refusal")
        else:
            assert False, "runtime convergence must never traverse a symlink"
    finally:
        shutil.rmtree(root, ignore_errors=True)


WEDGED_DUMP = (
    'nginx: [emerg] unknown "connection_upgrade" variable\n'
    "nginx: configuration file /etc/nginx/nginx.conf test failed\n")

DUPLICATE_DUMP = (
    'nginx: [emerg] the duplicate "connection_upgrade" variable in '
    "/etc/nginx/conf.d/00_django_mojo_runtime.conf:7\n"
    "nginx: configuration file /etc/nginx/nginx.conf test failed\n")


@th.django_unit_test("a failed nginx -T surfaces nginx's own diagnosis")
def test_worker_parse_failure_carries_nginx_emerg(opts):
    """Regression: the stage estate wedge reported `got []` while nginx's
    stderr named the exact missing map. The [emerg] must reach the operator."""
    from mojo.deploy import nginx_runtime as runtime

    try:
        runtime.parse_worker_user(WEDGED_DUMP)
    except runtime.NginxRuntimeError as err:
        th.assert_in('unknown "connection_upgrade" variable', str(err),
                     "the real [emerg] must reach the operator, not a bare got []")
    else:
        assert False, "an error-only dump must not parse a worker user"


@th.django_unit_test("the upgrade-map decision yields to any other declaration")
def test_upgrade_map_decision(opts):
    from mojo.deploy import nginx_runtime as runtime

    fragment = "/etc/nginx/conf.d/00_django_mojo_runtime.conf"
    declared = "map $http_upgrade $connection_upgrade {\n    default upgrade;\n}\n"
    bootstrap = "# configuration file /etc/nginx/nginx.conf:\nuser www;\n"
    ours = "# configuration file %s:\n" % fragment

    cases = (
        (bootstrap + declared + ours + "sendfile on;\n", None, False,
         "a bootstrap declaration must win over the fragment"),
        (bootstrap + ours + declared, None, True,
         "a fragment-only declaration must be kept"),
        (bootstrap + ours + "sendfile on;\n", None, True,
         "an undeclared map must be carried by the fragment"),
        (WEDGED_DUMP, None, True,
         "an unknown-variable failure proves no declaration exists"),
        (DUPLICATE_DUMP, declared.encode(), False,
         "a duplicate failure means someone else declares it"),
        ("nginx: [emerg] something unrelated\n", declared.encode(), True,
         "an unrelated breakage must keep a map-bearing fragment"),
        ("nginx: [emerg] something unrelated\n", b"sendfile on;\n", False,
         "an unrelated breakage must keep a plain fragment"),
        ("nginx: [emerg] something unrelated\n", None, False,
         "an unrelated breakage with no fragment must not invent one"),
    )
    for dump, prior, expected, why in cases:
        th.assert_eq(
            runtime._upgrade_map_decision(dump, fragment, prior), expected, why)
