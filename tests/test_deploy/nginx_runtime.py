"""Persistent nginx spill-path convergence and first-upgrade activation."""

import os
import shutil
import tempfile
from types import SimpleNamespace
from unittest import mock

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
def test_converge_refuses_web_user_mismatch_before_mutation(opts):
    from mojo.deploy import nginx_runtime as runtime

    with mock.patch.object(runtime.os, "geteuid", return_value=0), \
            mock.patch.object(runtime, "nginx_dump", return_value="user nginx;\n"), \
            mock.patch.object(runtime, "_mkdir_exact") as mkdir:
        try:
            runtime.converge("www")
        except runtime.NginxRuntimeError as err:
            th.assert_in("does not match", str(err),
                         "worker mismatch should explain the deployment refusal")
        else:
            assert False, "WEB_USER/nginx worker mismatch must abort convergence"
    th.assert_eq(mkdir.call_count, 0,
                 "identity mismatch must be detected before any host mutation")


@th.django_unit_test()
def test_converge_verifies_active_graph_and_rolls_fragment_back(opts):
    from mojo.deploy import nginx_runtime as runtime

    root = tempfile.mkdtemp(prefix="nginx-runtime-rollback-")
    try:
        nginx_etc = os.path.join(root, "etc", "nginx")
        fragment = runtime.fragment_path(nginx_etc)
        os.makedirs(os.path.dirname(fragment))
        with open(fragment, "w") as handle:
            handle.write("# prior rollback-compatible fragment\n")
        installs = []

        def install(path, text):
            installs.append(text)
            mode = "wb" if isinstance(text, bytes) else "w"
            with open(path, mode) as handle:
                handle.write(text)

        fake_user = SimpleNamespace(pw_uid=123, pw_gid=456)
        with mock.patch.object(runtime.os, "geteuid", return_value=0), \
                mock.patch.object(runtime, "nginx_dump",
                                  side_effect=("user www;\n", "user www;\n",
                                               "user www;\n")), \
                mock.patch.object(runtime, "_resolve_user", return_value=fake_user), \
                mock.patch.object(runtime, "_mkdir_exact"), \
                mock.patch.object(runtime, "_converge_selinux"), \
                mock.patch.object(runtime, "_probe_as_worker"), \
                mock.patch.object(runtime, "_install_fragment", side_effect=install):
            try:
                runtime.converge("www", nginx_etc=nginx_etc,
                                 root="/var/lib/django-mojo/nginx")
            except runtime.NginxRuntimeError as err:
                th.assert_in("exactly once", str(err),
                             "post-install nginx -T must prove every active directive")
            else:
                assert False, "a fragment absent from active nginx -T must abort deployment"
        th.assert_eq(installs[-1], b"# prior rollback-compatible fragment\n",
                     "failed activation must atomically restore the prior fragment")
        th.assert_eq(open(fragment).read(), "# prior rollback-compatible fragment\n",
                     "rollback must leave the prior nginx graph byte-identical")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_inactive_fragment_failure_names_the_missing_include(opts):
    """The bare zero-count message sends an operator hunting for a corrupt
    fragment. The fragment is fine — nginx.conf simply has no include for
    conf.d, so the active graph never reads it. The diagnosis must say so, and
    must be derived before the rollback removes the file it names."""
    from mojo.deploy import nginx_runtime as runtime

    root = tempfile.mkdtemp(prefix="nginx-runtime-include-")
    try:
        nginx_etc = os.path.join(root, "etc", "nginx")
        fragment = runtime.fragment_path(nginx_etc)
        os.makedirs(os.path.dirname(fragment))
        def install(path, text):
            # The real installer fchowns to root; this harness is not root.
            mode = "wb" if isinstance(text, bytes) else "w"
            with open(path, mode) as handle:
                handle.write(text)

        fake_user = SimpleNamespace(pw_uid=123, pw_gid=456)
        with mock.patch.object(runtime.os, "geteuid", return_value=0), \
                mock.patch.object(runtime, "nginx_dump",
                                  side_effect=("user www;\n", "user www;\n",
                                               "user www;\n")), \
                mock.patch.object(runtime, "_resolve_user", return_value=fake_user), \
                mock.patch.object(runtime, "_mkdir_exact"), \
                mock.patch.object(runtime, "_converge_selinux"), \
                mock.patch.object(runtime, "_probe_as_worker"), \
                mock.patch.object(runtime, "_install_fragment", side_effect=install):
            try:
                runtime.converge("www", nginx_etc=nginx_etc,
                                 root="/var/lib/django-mojo/nginx")
            except runtime.NginxRuntimeError as err:
                message = str(err)
            else:
                assert False, "an unread fragment must still abort convergence"
        th.assert_in("exactly once; got 0", message,
                     f"the original activation failure must survive: {message}")
        th.assert_in(fragment, message,
                     f"the diagnosis must name the installed fragment: {message}")
        th.assert_in("include %s/*.conf;" % os.path.dirname(fragment), message,
                     f"the diagnosis must name the missing include: {message}")
        th.assert_true(not os.path.exists(fragment),
                       "a failed activation must still roll the fragment back")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_only_canonical_root_deploy_render_activates_runtime(opts):
    from mojo.deploy import __main__ as deploy_main

    root = tempfile.mkdtemp(prefix="nginx-runtime-render-")
    try:
        project = os.path.join(root, "project")
        canonical = os.path.join(project, "var", "deploy")
        args = SimpleNamespace(
            project_path=project, dest=canonical, app_user="app",
            web_user="www", workers="4")
        os.makedirs(project)
        with mock.patch.object(deploy_main.os, "geteuid", return_value=0), \
                mock.patch.object(deploy_main.os, "getcwd", return_value=project), \
                mock.patch("mojo.deploy.nginx_runtime.converge") as converge:
            result = deploy_main.cmd_render(args)
        th.assert_eq(result, 0, "canonical deploy render must still materialize templates")
        th.assert_eq(converge.call_count, 1,
                     "the unchanged old-shell render argv must activate the new module")

        args.dest = os.path.join(root, "ordinary-output")
        with mock.patch.object(deploy_main.os, "geteuid", return_value=0), \
                mock.patch.object(deploy_main.os, "getcwd", return_value=project), \
                mock.patch("mojo.deploy.nginx_runtime.converge") as converge:
            result = deploy_main.cmd_render(args)
        th.assert_eq(result, 0, "ordinary render must remain portable and side-effect-free")
        th.assert_eq(converge.call_count, 0,
                     "a non-deploy render may not mutate host nginx state")
    finally:
        shutil.rmtree(root, ignore_errors=True)


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


@th.django_unit_test()
def test_selinux_policy_is_durable_and_labels_are_read_back(opts):
    from mojo.deploy import nginx_runtime as runtime

    calls = []

    def run(argv, timeout=30):
        calls.append(argv)
        if argv[0] == "/usr/bin/ls":
            return "system_u:object_r:httpd_sys_rw_content_t:s0 %s\n" % argv[-1]
        return ""

    binaries = {
        "semanage": "/usr/sbin/semanage",
        "restorecon": "/usr/sbin/restorecon",
        "ls": "/usr/bin/ls",
    }
    with mock.patch.object(runtime, "_selinux_enforcing", return_value=True), \
            mock.patch.object(runtime.shutil, "which",
                              side_effect=lambda name: binaries.get(name)), \
            mock.patch.object(runtime, "_run", side_effect=run):
        runtime._converge_selinux("/var/lib/django-mojo/nginx")

    th.assert_true(any(argv[1:3] == ["fcontext", "-a"] for argv in calls),
                   f"SELinux convergence must persist an fcontext rule: {calls}")
    th.assert_true(any(argv[0] == "/usr/sbin/restorecon" for argv in calls),
                   f"SELinux convergence must apply the durable policy: {calls}")
    readbacks = [argv for argv in calls if argv[0] == "/usr/bin/ls"]
    th.assert_eq(len(readbacks), 5,
                 "every private nginx leaf must have its SELinux type read back")


@th.django_unit_test()
def test_selinux_audit_reports_label_drift(opts):
    from mojo.deploy import nginx_runtime as runtime

    with mock.patch.object(runtime, "_selinux_enforcing", return_value=True), \
            mock.patch.object(runtime.shutil, "which", return_value="/usr/bin/ls"), \
            mock.patch.object(runtime, "_run",
                              return_value="system_u:object_r:var_t:s0 path"):
        errors = runtime._audit_selinux("/var/lib/django-mojo/nginx")
    th.assert_eq(len(errors), 5,
                 f"every wrong SELinux leaf label must be reported: {errors}")


def _write_file(path, text):
    # The real installer fchowns to root; test harnesses are not root.
    mode = "wb" if isinstance(text, bytes) else "w"
    with open(path, mode) as handle:
        handle.write(text)


WEDGED_DUMP = (
    'nginx: [emerg] unknown "connection_upgrade" variable\n'
    "nginx: configuration file /etc/nginx/nginx.conf test failed\n")

DUPLICATE_DUMP = (
    'nginx: [emerg] the duplicate "connection_upgrade" variable in '
    "/etc/nginx/conf.d/00_django_mojo_runtime.conf:7\n"
    "nginx: configuration file /etc/nginx/nginx.conf test failed\n")


def _sections_dump(nginx_etc, fragment, bootstrap_extra=""):
    """A healthy nginx -T shape: bootstrap section plus the live fragment."""
    return (
        "# configuration file %s/nginx.conf:\nuser www;\n%s"
        "# configuration file %s:\n%s"
        % (nginx_etc, bootstrap_extra, fragment, open(fragment).read()))


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


@th.django_unit_test("converge heals a graph wedged by the missing upgrade map")
def test_converge_heals_missing_upgrade_map(opts):
    """Regression for the api-wmwx-stage wedge: the live edge generation was
    rewritten without the `$connection_upgrade` map while the bootstrap never
    declared one, so every nginx -T failed and converge died at `got []` —
    on deploy AND on rollback. Converge must repair exactly this state."""
    from mojo.deploy import nginx_runtime as runtime

    root = tempfile.mkdtemp(prefix="nginx-runtime-heal-")
    try:
        nginx_etc = os.path.join(root, "etc", "nginx")
        fragment = runtime.fragment_path(nginx_etc)
        os.makedirs(os.path.dirname(fragment))
        _write_file(fragment, runtime.render_http_fragment(
            "/var/lib/django-mojo/nginx"))

        state = {"dumps": 0}

        def dump(nginx_binary="nginx", allow_failure=False):
            state["dumps"] += 1
            if state["dumps"] == 1:
                assert allow_failure, "the first dump must tolerate the wedge"
                return WEDGED_DUMP
            return _sections_dump(nginx_etc, fragment)

        fake_user = SimpleNamespace(pw_uid=123, pw_gid=456)
        with mock.patch.object(runtime.os, "geteuid", return_value=0), \
                mock.patch.object(runtime, "nginx_dump", side_effect=dump), \
                mock.patch.object(runtime, "_resolve_user", return_value=fake_user), \
                mock.patch.object(runtime, "_mkdir_exact"), \
                mock.patch.object(runtime, "_converge_selinux"), \
                mock.patch.object(runtime, "_probe_as_worker"), \
                mock.patch.object(runtime, "_install_fragment",
                                  side_effect=_write_file):
            runtime.converge("www", nginx_etc=nginx_etc,
                             root="/var/lib/django-mojo/nginx")

        healed = open(fragment).read()
        th.assert_in("map $http_upgrade $connection_upgrade", healed,
                     "the fragment must carry the map no other file declares")
        th.assert_in("client_body_temp_path", healed,
                     "healing must not displace the spill-path contract")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test("converge leaves the fragment plain when the bootstrap owns the map")
def test_converge_keeps_fragment_plain_beside_bootstrap_map(opts):
    from mojo.deploy import nginx_runtime as runtime

    root = tempfile.mkdtemp(prefix="nginx-runtime-plain-")
    try:
        nginx_etc = os.path.join(root, "etc", "nginx")
        fragment = runtime.fragment_path(nginx_etc)
        os.makedirs(os.path.dirname(fragment))
        _write_file(fragment, runtime.render_http_fragment(
            "/var/lib/django-mojo/nginx"))
        declared = ("map $http_upgrade $connection_upgrade {\n"
                    "    default upgrade;\n}\n")

        fake_user = SimpleNamespace(pw_uid=123, pw_gid=456)
        with mock.patch.object(runtime.os, "geteuid", return_value=0), \
                mock.patch.object(
                    runtime, "nginx_dump",
                    side_effect=lambda *a, **k: _sections_dump(
                        nginx_etc, fragment, bootstrap_extra=declared)), \
                mock.patch.object(runtime, "_resolve_user", return_value=fake_user), \
                mock.patch.object(runtime, "_mkdir_exact"), \
                mock.patch.object(runtime, "_converge_selinux"), \
                mock.patch.object(runtime, "_probe_as_worker"), \
                mock.patch.object(runtime, "_install_fragment",
                                  side_effect=_write_file):
            runtime.converge("www", nginx_etc=nginx_etc,
                             root="/var/lib/django-mojo/nginx")

        th.assert_true(
            "connection_upgrade" not in open(fragment).read(),
            "a bootstrap-declared map must not be duplicated in the fragment")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test("reconcile withdraws the fragment map when the bootstrap gains one")
def test_reconcile_yields_map_to_bootstrap(opts):
    """post_deploy installs the project nginx.conf AFTER converge ran. A
    bootstrap upgraded to declare the map would leave two declarations —
    reconcile re-decides against the post-install graph."""
    from mojo.deploy import nginx_runtime as runtime

    root = tempfile.mkdtemp(prefix="nginx-runtime-reconcile-")
    try:
        nginx_etc = os.path.join(root, "etc", "nginx")
        fragment = runtime.fragment_path(nginx_etc)
        os.makedirs(os.path.dirname(fragment))
        _write_file(fragment, runtime.render_http_fragment(
            "/var/lib/django-mojo/nginx", include_map=True))

        state = {"dumps": 0}

        def dump(nginx_binary="nginx", allow_failure=False):
            state["dumps"] += 1
            if state["dumps"] == 1:
                return DUPLICATE_DUMP
            return _sections_dump(
                nginx_etc, fragment,
                bootstrap_extra="map $http_upgrade $connection_upgrade {\n"
                                "    default upgrade;\n}\n")

        with mock.patch.object(runtime.os, "geteuid", return_value=0), \
                mock.patch.object(runtime, "nginx_dump", side_effect=dump), \
                mock.patch.object(runtime, "_install_fragment",
                                  side_effect=_write_file):
            changed = runtime.reconcile_upgrade_map(nginx_etc=nginx_etc)

        th.assert_true(changed, "a duplicate map must move the fragment")
        text = open(fragment).read()
        th.assert_true("connection_upgrade" not in text,
                       "the fragment must yield the map to the bootstrap")
        th.assert_in("client_body_temp_path", text,
                     "yielding the map must not displace the spill paths")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_worker_probe_creates_and_removes_sentinel_in_every_leaf(opts):
    from mojo.deploy import nginx_runtime as runtime

    root = tempfile.mkdtemp(prefix="nginx-worker-probe-")
    try:
        for _directive, leaf in runtime.TEMP_PATHS:
            os.mkdir(os.path.join(root, leaf), 0o700)
        user = SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())
        with mock.patch.object(runtime.os, "setgroups"), \
                mock.patch.object(runtime.os, "setgid"), \
                mock.patch.object(runtime.os, "setuid"):
            runtime._probe_as_worker(user, root)
        for _directive, leaf in runtime.TEMP_PATHS:
            th.assert_eq(os.listdir(os.path.join(root, leaf)), [],
                         f"worker sentinel must be removed from {leaf}")
    finally:
        shutil.rmtree(root, ignore_errors=True)
