"""Regression for a typed deploy path that can replace broken Django.

The permanent small project shims locate the installed framework scripts.
The update transaction remains plain shell until it installs the candidate,
then deliberately locates the post-deploy body from that candidate version.
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
                    "trusted_change", "mojosec", "vhost_install",
                    "sanity_check"):
                th.assert_true(forbidden not in source,
                               "%s still contains %s" % (name, forbidden))
            if name == "update.sh":
                th.assert_true("import django" not in source
                               and "import mojo" not in source,
                               "the launcher must not import the release it replaces")
                th.assert_in("systemd-run", source,
                             "the transaction must survive restarting its own engine")
                install = source.index("python3 -m pip install \"django-mojo==$framework\"")
                locate = source.index("locate post_deploy.sh", install)
                th.assert_true(install < locate,
                               "the installed candidate must supply the post-deploy body")
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
            "PROBE_SECONDS": "0",
        })
        active = os.path.join(transaction_root, "active")
        os.makedirs(active, mode=0o700)
        for name, value in (
                ("previous_sha", "1" * 40),
                ("previous_framework", "1.16.2"),
                ("previous_node_type", "api"),
                ("candidate_node_type", "api"),
                ("deployment", "11111111-1111-1111-1111-111111111111"),
                ("started_at", "1")):
            with open(os.path.join(active, name), "w") as handle:
                handle.write(value + "\n")
        argv = ["bash", os.path.join(project, "aws", "post_deploy.sh"),
                "--activate", "--node-type", "api", "--state", active]
        done = subprocess.run(
            argv, env=environment, capture_output=True, text=True, timeout=30)
        th.assert_eq(done.returncode, 0, done.stderr)
        th.assert_true(os.path.isfile(sentinel),
                       "root recovery must ignore application-writable metadata")
        commands = ""
        if os.path.exists(command_log):
            with open(command_log) as handle:
                commands = handle.read()
        th.assert_in("nginx -t", commands,
                     "nginx itself must be the configuration gate")
        th.assert_in("curl -ksS", commands,
                     "the restarted API must be probed over HTTP")
        with open(os.path.join(nginx_etc, "conf.d", "app.conf")) as handle:
            th.assert_in("server_name _;", handle.read(),
                         "ordinary nginx syntax must not be semantically refused")

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

        redirect_env = environment.copy()
        redirect_env["CURL_CODE"] = "301"
        redirect = subprocess.run(
            argv, env=redirect_env, capture_output=True, text=True, timeout=30)
        th.assert_true(redirect.returncode != 0,
                       "a redirect must not pass as a live candidate API")


@th.django_unit_test()
def test_custom_node_uses_only_its_typed_deploy_profile(opts):
    """A Sites-style worker must never be forced through the API path."""
    import mojo

    repo = os.path.dirname(os.path.dirname(os.path.abspath(mojo.__file__)))
    source = os.path.join(
        repo, "mojo", "deploy", "project_scripts", "post_deploy.sh")
    with tempfile.TemporaryDirectory() as root:
        project = os.path.join(root, "project")
        stubs = os.path.join(root, "bin")
        profile_dir = os.path.join(project, "aws", "deploy")
        state_root = os.path.join(root, "state")
        os.makedirs(profile_dir)
        os.makedirs(stubs)
        shutil.copyfile(source, os.path.join(project, "aws", "post_deploy.sh"))

        profile_log = os.path.join(root, "profile.log")
        command_log = os.path.join(root, "commands.log")
        _write_executable(
            os.path.join(profile_dir, "sites.sh"),
            "case \"${1:-}\" in preflight|restart|probe) ;; *) exit 64 ;; esac\n"
            "printf '%s:%s\\n' \"${MOJO_DEPLOY_ROLLBACK:-}\" \"$1\" >> \"$PROFILE_LOG\"\n")
        for name in ("pip", "git", "nginx", "systemctl", "curl", "python3"):
            _write_executable(
                os.path.join(stubs, name),
                "printf '%s %s\\n' \"$(basename \"$0\")\" \"$*\" >> \"$COMMAND_LOG\"\n"
                "exit 0\n")

        environment = os.environ.copy()
        environment.update({
            "PATH": stubs + ":/usr/bin:/bin",
            "PROJ_PATH": project,
            "PROFILE_LOG": profile_log,
            "COMMAND_LOG": command_log,
            "MOJO_DEPLOY_STATE_ROOT": state_root,
            "MOJO_PREVIOUS_SHA": "1" * 40,
            "MOJO_PREVIOUS_FRAMEWORK": "1.16.2",
        })
        active = os.path.join(state_root, "active")
        os.makedirs(active)
        for name, value in (
                ("previous_sha", "1" * 40),
                ("previous_framework", "1.16.2"),
                ("previous_node_type", "sites"),
                ("candidate_node_type", "sites"),
                ("deployment", "11111111-1111-1111-1111-111111111111"),
                ("started_at", "1")):
            with open(os.path.join(active, name), "w") as handle:
                handle.write(value + "\n")
        done = subprocess.run(
            ["bash", os.path.join(project, "aws", "post_deploy.sh"),
             "--activate", "--node-type", "sites", "--state", active],
            env=environment, capture_output=True, text=True, timeout=30)

        th.assert_eq(done.returncode, 0, done.stderr)
        with open(profile_log) as handle:
            th.assert_eq(handle.read().splitlines(), [
                "0:preflight", "0:restart", "0:probe",
            ], "custom nodes need one small project-owned lifecycle")
        commands = ""
        if os.path.exists(command_log):
            with open(command_log) as handle:
                commands = handle.read()
        for forbidden in ("manage.py", "nginx ", "systemctl ", "curl "):
            th.assert_true(
                forbidden not in commands,
                "custom node unexpectedly entered the API deploy path: " + forbidden)

        _write_executable(
            os.path.join(active, "previous_profile.sh"),
            "printf 'previous:%s:%s\\n' \"${MOJO_DEPLOY_ROLLBACK:-}\" \"$1\" >> \"$PROFILE_LOG\"\n")
        cleanup_env = environment.copy()
        cleanup_env["MOJO_DEPLOY_ROLLBACK"] = "1"
        for action in ("--rollback-candidate", "--activate-previous"):
            recovered = subprocess.run(
                ["bash", os.path.join(project, "aws", "post_deploy.sh"),
                 action, "--node-type", "sites", "--state", active],
                env=cleanup_env, capture_output=True, text=True, timeout=30)
            th.assert_eq(recovered.returncode, 0, recovered.stderr)
        with open(profile_log) as handle:
            th.assert_eq(handle.read().splitlines()[-3:], [
                "1:restart", "previous:1:restart", "previous:1:probe",
            ], "rollback must clean the candidate then prove the saved profile")


@th.django_unit_test()
def test_code_node_runs_only_common_checkout_and_declared_dependencies(opts):
    """The reserved code type deliberately has no activation lifecycle."""
    import mojo

    repo = os.path.dirname(os.path.dirname(os.path.abspath(mojo.__file__)))
    update = os.path.join(repo, "mojo", "deploy", "project_scripts", "update.sh")
    post = os.path.join(repo, "mojo", "deploy", "project_scripts", "post_deploy.sh")
    with tempfile.TemporaryDirectory() as root:
        project = os.path.join(root, "project")
        origin = os.path.join(root, "origin.git")
        stubs = os.path.join(root, "bin")
        os.makedirs(project)
        os.makedirs(stubs)

        def git(*args, cwd=project):
            done = subprocess.run(
                ["git", *args], cwd=cwd, capture_output=True, text=True)
            th.assert_eq(done.returncode, 0, done.stderr)
            return done.stdout.strip()

        git("init")
        git("config", "user.email", "deploy-test@example.invalid")
        git("config", "user.name", "Deploy Test")
        with open(os.path.join(project, ".gitignore"), "w") as handle:
            handle.write("var/\n")
        with open(os.path.join(project, "requirements.txt"), "w") as handle:
            handle.write("old-dependency==1\n")
        git("add", ".gitignore", "requirements.txt")
        git("commit", "-m", "previous")
        previous = git("rev-parse", "HEAD")
        with open(os.path.join(project, "requirements.txt"), "w") as handle:
            handle.write("root-fallback==2\n")
        os.makedirs(os.path.join(project, "aws", "deploy"))
        with open(os.path.join(project, "aws", "deploy", "requirements.txt"), "w") as handle:
            handle.write("deploy-manifest==3\n")
        git("add", "requirements.txt", "aws/deploy/requirements.txt")
        git("commit", "-m", "candidate")
        candidate = git("rev-parse", "HEAD")
        git("init", "--bare", origin, cwd=root)
        git("remote", "add", "origin", origin)
        git("push", "origin", "HEAD:main")
        git("checkout", "--force", previous)

        command_log = os.path.join(root, "commands")
        version_file = os.path.join(root, "version")
        with open(version_file, "w") as handle:
            handle.write("1.16.2\n")
        _write_executable(
            os.path.join(stubs, "python3"),
            "printf 'python3 %s\\n' \"$*\" >> \"$COMMAND_LOG\"\n"
            "if [ \"${1:-}\" = - ]; then printf 'api\\n'; exit 0; fi\n"
            "if [ \"$*\" = '-m pip show django-mojo' ]; then printf 'Version: '; cat \"$VERSION_FILE\"; exit 0; fi\n"
            "case \"$*\" in\n"
            "  '-m pip install django-mojo=='*) printf '%s\\n' \"${4#django-mojo==}\" > \"$VERSION_FILE\"; exit 0 ;;\n"
            "  '-m pip install -r '*) exit 0 ;;\n"
            "  '-m mojo.deploy locate post_deploy.sh') printf '%s\\n' \"$POST_SCRIPT\"; exit 0 ;;\n"
            "esac\n"
            "exit 64\n")
        _write_executable(os.path.join(stubs, "flock"), "exit 0\n")
        environment = os.environ.copy()
        environment.update({
            "PATH": stubs + ":/usr/bin:/bin",
            "PROJ_PATH": project,
            "COMMAND_LOG": command_log,
            "VERSION_FILE": version_file,
            "POST_SCRIPT": post,
            "MOJO_DEPLOY_NO_SYSTEMD": "1",
            "MOJO_DEPLOY_STATE_ROOT": os.path.join(root, "state"),
            "MOJO_DEPLOY_PARENT_STATUS": "1",
        })
        done = subprocess.run(
            ["bash", update, "--sha", candidate, "--framework", "1.17.2",
             "--deployment", "11111111-1111-4111-8111-111111111111",
             "--node-type", "code"],
            env=environment, capture_output=True, text=True, timeout=30)
        th.assert_eq(done.returncode, 0, done.stderr)
        th.assert_eq(git("rev-parse", "HEAD"), candidate,
                     "the common transaction did not install the candidate")
        with open(command_log) as handle:
            commands = handle.read()
        th.assert_in("pip install -r aws/deploy/requirements.txt", commands,
                     "the explicit deploy manifest must beat root requirements.txt")
        th.assert_true("pip install -r requirements.txt" not in commands,
                       "the fallback manifest ran alongside the explicit one")
        for forbidden in ("manage.py", "nginx", "systemctl", "curl"):
            th.assert_true(forbidden not in commands,
                           "code-only deploy entered host activation: " + forbidden)
        with open(os.path.join(project, "var", "deploy_identity.json")) as handle:
            identity = handle.read()
        th.assert_in('"node_type":"code"', identity,
                     "the successful identity lost the local lifecycle")


@th.django_unit_test()
def test_update_enters_one_bounded_transient_unit_before_mutation(opts):
    import mojo

    repo = os.path.dirname(os.path.dirname(os.path.abspath(mojo.__file__)))
    update = os.path.join(repo, "mojo", "deploy", "project_scripts", "update.sh")
    with tempfile.TemporaryDirectory() as root:
        stubs = os.path.join(root, "bin")
        os.makedirs(stubs)
        command_log = os.path.join(root, "systemd-run")
        _write_executable(
            os.path.join(stubs, "systemd-run"),
            "printf '%s\\n' \"$*\" > \"$COMMAND_LOG\"\nexit 0\n")
        environment = os.environ.copy()
        environment.update({
            "PATH": stubs + ":/usr/bin:/bin",
            "COMMAND_LOG": command_log,
            "PROJ_PATH": os.path.join(root, "must-not-be-touched"),
        })
        done = subprocess.run(
            ["bash", update, "--sha", "a" * 40, "--framework", "1.17.2",
             "--deployment", "11111111-1111-4111-8111-111111111111",
             "--node-type", "sites"],
            env=environment, capture_output=True, text=True, timeout=30)
        th.assert_eq(done.returncode, 0, done.stderr)
        th.assert_true(not os.path.exists(environment["PROJ_PATH"]),
                       "checkout state changed before entering the transient unit")
        with open(command_log) as handle:
            command = handle.read()
        th.assert_in("RuntimeMaxSec=1800", command,
                     "the transient transaction needs a hard runtime bound")
        th.assert_in("TimeoutStopSec=900", command,
                     "systemd must leave a realistic rollback window")
        th.assert_in("--transaction", command,
                     "the transient unit did not enter the internal transaction")
