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
