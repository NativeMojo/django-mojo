"""Regression for a deploy path that can replace a broken Django release.

The project owns stable copies of the two shell entry points.  Exporting those
copies must not import Django, and the entry points themselves must remain
plain shell rather than locating and executing code from the currently
installed framework.
"""

import os
import shutil
import stat
import subprocess
import sys
import tempfile

from testit import helpers as th


def _write_executable(path, body):
    with open(path, "w") as handle:
        handle.write("#!/bin/bash\n" + body)
    os.chmod(path, 0o755)


@th.django_unit_test()
def test_project_scripts_export_without_django_bootstrap(opts):
    with tempfile.TemporaryDirectory() as root:
        aws = os.path.join(root, "aws")
        env = os.environ.copy()
        env["DJANGO_SETTINGS_MODULE"] = "this.module.must.not.import"
        done = subprocess.run(
            [sys.executable, "-m", "mojo.deploy", "export-scripts",
             "--dest", aws],
            capture_output=True,
            env=env,
            text=True,
        )

        th.assert_eq(done.returncode, 0, done.stderr)
        for name in ("update.sh", "post_deploy.sh"):
            path = os.path.join(aws, name)
            th.assert_true(os.path.isfile(path), path)
            th.assert_true(os.stat(path).st_mode & stat.S_IXUSR, path)
            parsed = subprocess.run(
                ["bash", "-n", path], capture_output=True, text=True)
            th.assert_eq(parsed.returncode, 0, parsed.stderr)

            with open(path) as handle:
                source = handle.read().lower()
            for forbidden in (
                    "mojo.deploy locate", "trusted_change", "mojosec",
                    "vhost_install", "sanity_check"):
                th.assert_true(forbidden not in source,
                               "%s still contains %s" % (name, forbidden))
            if name == "update.sh":
                th.assert_true("import django" not in source
                               and "import mojo" not in source,
                               "the launcher must not import the release it replaces")
                handoff = source.index("sudo -e bash ./aws/post_deploy.sh")
                legacy_callback = source.index("bin/manage.py deploy_status")
                th.assert_true(handoff < legacy_callback,
                               "the predecessor callback may run only after a healthy candidate")
            else:
                th.assert_true("deploy_status" not in source,
                               "the transaction must never need Django to report or roll back")


@th.django_unit_test()
def test_post_deploy_has_only_nginx_and_exact_200_release_gates(opts):
    import mojo

    repo = os.path.dirname(os.path.dirname(os.path.abspath(mojo.__file__)))
    source = os.path.join(
        repo, "mojo", "deploy", "project_scripts", "post_deploy.sh")
    with tempfile.TemporaryDirectory() as root:
        project = os.path.join(root, "project")
        stubs = os.path.join(root, "bin")
        nginx_etc = os.path.join(root, "nginx")
        systemd_etc = os.path.join(root, "systemd")
        cron_etc = os.path.join(root, "cron")
        transaction_root = os.path.join(root, "root-owned-transaction")
        for path in (
                os.path.join(project, "aws", "nginx", "conf.d"),
                os.path.join(project, "bin"), stubs,
                os.path.join(nginx_etc, "conf.d"), systemd_etc, cron_etc):
            os.makedirs(path, exist_ok=True)
        shutil.copyfile(source, os.path.join(project, "aws", "post_deploy.sh"))

        files = {
            os.path.join(project, "aws", "nginx", "nginx.conf"):
                "events {}\nhttp {}\n",
            os.path.join(project, "aws", "nginx", "django.inc"):
                "location / {}\n",
            os.path.join(project, "aws", "nginx", "conf.d", "app.conf"):
                "server { server_name _; }\n",
            os.path.join(nginx_etc, "nginx.conf"): "old nginx\n",
            os.path.join(nginx_etc, "django.inc"): "old django\n",
        }
        for path, body in files.items():
            with open(path, "w") as handle:
                handle.write(body)
        os.makedirs(os.path.join(project, "var"), exist_ok=True)
        # The application-writable legacy location must be ignored.  A broken
        # or compromised candidate may leave arbitrary rollback metadata here;
        # root recovery must never consume it.
        legacy_active = os.path.join(
            project, "var", "deploy-rollback", "active")
        os.makedirs(legacy_active, exist_ok=True)
        sentinel = os.path.join(root, "must-not-be-removed")
        with open(sentinel, "w") as handle:
            handle.write("safe\n")
        with open(os.path.join(legacy_active, "files"), "w") as handle:
            handle.write("attacker\t%s\n" % sentinel)
        with open(os.path.join(project, "var", "previous_sha"), "w") as handle:
            handle.write("1" * 40 + "\n")
        with open(os.path.join(project, "var", "previous_framework"), "w") as handle:
            handle.write("1.16.2\n")

        command_log = os.path.join(root, "commands")
        _write_executable(
            os.path.join(stubs, "python3"),
            "echo \"python3 $*\" >> \"$COMMAND_LOG\"\n"
            "if [ \"${1:-}\" = -m ] && [ \"${2:-}\" = mojo.deploy ]; then\n"
            "  mkdir -p \"$PROJ_PATH/var/deploy/systemd\" \"$PROJ_PATH/var/deploy/cron.d\"\n"
            "  printf '[Service]\\n' > \"$PROJ_PATH/var/deploy/systemd/mojo-asgi.service\"\n"
            "  printf '* * * * * root true\\n' > \"$PROJ_PATH/var/deploy/cron.d/mojo\"\n"
            "  exit 0\nfi\n"
            "case \"$*\" in *manage.py*) exit \"${MANAGE_RC:-0}\" ;; esac\n"
            "exit 0\n")
        _write_executable(
            os.path.join(stubs, "install"),
            "src=\"${@: -2:1}\"; dest=\"${@: -1}\"\n"
            "mkdir -p \"$(dirname \"$dest\")\"; cp -f \"$src\" \"$dest\"\n")
        for name in ("pip", "git", "systemctl", "nginx"):
            _write_executable(
                os.path.join(stubs, name),
                "echo \"%s $*\" >> \"$COMMAND_LOG\"\nexit 0\n" % name)
        _write_executable(
            os.path.join(stubs, "curl"),
            "echo \"curl $*\" >> \"$COMMAND_LOG\"\n"
            "printf '%s' \"${CURL_CODE:-200}\"\n")

        environment = os.environ.copy()
        environment.update({
            "PATH": stubs + ":/usr/bin:/bin",
            "COMMAND_LOG": command_log,
            "PROJ_PATH": project,
            "NGINX_ETC": nginx_etc,
            "SYSTEMD_ETC": systemd_etc,
            "CRON_ETC": cron_etc,
            "MOJO_DEPLOY_STATE_ROOT": transaction_root,
        })
        argv = ["bash", os.path.join(project, "aws", "post_deploy.sh"),
                "--framework", "1.17.2"]
        done = subprocess.run(
            argv, env=environment, capture_output=True, text=True, timeout=30)
        th.assert_eq(done.returncode, 0, done.stderr)
        th.assert_true(os.path.isfile(sentinel),
                       "root recovery must ignore application-writable metadata")
        with open(command_log) as handle:
            commands = handle.read()
        th.assert_in("nginx -t", commands,
                     "nginx itself must be the configuration gate")
        th.assert_in("curl -ksS", commands,
                     "the restarted API must be probed over HTTP")
        with open(os.path.join(nginx_etc, "conf.d", "app.conf")) as handle:
            th.assert_in("server_name _;", handle.read(),
                         "ordinary nginx syntax must not be semantically refused")

        active = os.path.join(transaction_root, "active")
        os.makedirs(active, mode=0o700)
        with open(os.path.join(active, "previous_sha"), "w") as handle:
            handle.write("1" * 40 + "\n")
        with open(os.path.join(active, "previous_framework"), "w") as handle:
            handle.write("1.16.2\n")
        with open(os.path.join(active, "files"), "w") as handle:
            handle.write("123\t%s\n" % sentinel)
        unsafe = subprocess.run(
            ["bash", os.path.join(project, "aws", "post_deploy.sh"),
             "--recover-only"], env=environment, capture_output=True,
            text=True, timeout=30)
        th.assert_true(unsafe.returncode != 0,
                       "recovery must reject a destination it did not generate")
        th.assert_true(os.path.isfile(sentinel),
                       "rejected rollback metadata must not mutate its target")

        shutil.rmtree(os.path.join(transaction_root, "active"),
                      ignore_errors=True)
        with open(os.path.join(nginx_etc, "nginx.conf"), "w") as handle:
            handle.write("old nginx\n")
        failed_env = environment.copy()
        failed_env["MANAGE_RC"] = "1"
        failed = subprocess.run(
            argv, env=failed_env, capture_output=True, text=True, timeout=30)
        th.assert_true(failed.returncode != 0,
                       "a candidate that cannot load Django must roll back")
        with open(os.path.join(nginx_etc, "nginx.conf")) as handle:
            th.assert_eq(handle.read(), "old nginx\n",
                         "rollback must restore the previous nginx bytes")

        shutil.rmtree(os.path.join(transaction_root, "active"),
                      ignore_errors=True)
        redirect_env = environment.copy()
        redirect_env["CURL_CODE"] = "301"
        redirect = subprocess.run(
            argv, env=redirect_env, capture_output=True, text=True, timeout=30)
        th.assert_true(redirect.returncode != 0,
                       "a redirect must not pass as a live candidate API")
