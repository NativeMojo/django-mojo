"""Real predecessor shell plus crash-cutpoint retention contracts."""

import json
import os
from pathlib import Path
import subprocess
import tempfile

from testit import helpers as th


def executable(path, source):
    Path(path).write_text(source)
    os.chmod(path, 0o700)


@th.django_unit_test()
def test_retention_crash_cutpoints_preserve_original_and_forward_arguments(opts):
    from mojo.deploy.mojosec_refresh import retain, durable_write

    for cut in (1, 2, 3):
        with tempfile.TemporaryDirectory() as root:
            helper = os.path.join(root, "source.py")
            executable(helper, "# retained helper fixture\n")
            previous = os.path.join(root, "previous_post.sh")
            body = '#!/bin/bash\nprintf "%s\\n" "$@" > "$(dirname "$0")/args"\n'
            executable(previous, body)
            count = [0]

            def interrupted(path, data, **kwargs):
                durable_write(path, data, **kwargs)
                count[0] += 1
                if count[0] == cut:
                    raise OSError("simulated interruption after durable rename")

            try:
                retain(root, helper, os.getuid(), writer=interrupted)
            except OSError:
                pass
            retain(root, helper, os.getuid())
            retain(root, helper, os.getuid())
            assert Path(root, "previous_post.original.sh").read_text() == body, "every crash cut must preserve original bytes, never wrapper bytes"
            done = subprocess.run(["bash", previous, "--activate-previous", "space argument"],
                                  capture_output=True, text=True, timeout=10)
            assert done.returncode == 0, "wrapper must preserve original activation outcome"
            assert Path(root, "args").read_text().splitlines() == ["--activate-previous", "space argument"], "wrapper must forward arguments unchanged"


def _exercise_refresh_updaters(opts, closed_stderr=False):
    from mojo.deploy import mojosec_refresh

    assert os.getuid() != 0, "the real updater fixture requires an unprivileged runner so all state remains test-local"
    repo = Path(mojosec_refresh.__file__).resolve().parents[2]
    post_source = (repo / "mojo/deploy/project_scripts/post_deploy.sh").read_text()
    predecessor = Path(__file__).parent / "fixtures/pre_refresh_update.sh"
    modern = repo / "mojo/deploy/project_scripts/update.sh"
    for updater in (predecessor, modern):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            project, state, stubs = root / "project", root / "state", root / "bin"
            package = root / "package"
            for directory in (project / "aws/deploy", stubs, package / "project_scripts"):
                directory.mkdir(parents=True)
            log, version = root / "log", root / "version"
            version.write_text("1\n")
            old_post = package / "project_scripts/old_post.sh"
            executable(old_post, '#!/bin/bash\necho previous-activation >> "$EVENT_LOG"\n')
            candidate_post = package / "project_scripts/post_deploy.sh"
            executable(candidate_post, post_source)
            executable(project / "aws/deploy/worker.sh", '#!/bin/bash\necho "profile-$1" >> "$EVENT_LOG"\n[ "$1" != probe ]\n')
            # Retain the real helper/wrapper through a fake host boundary; this
            # avoids root/systemd and still executes both real updater scripts.
            helper_source = '''import importlib.util, os, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("refresh", REAL_HELPER)
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
state = sys.argv[sys.argv.index("--state") + 1]
if "--prepare" in sys.argv:
    m.retain(state, source=__file__, owner_uid=os.getuid())
else:
    direction = sys.argv[sys.argv.index("--direction") + 1]
    class Host:
        boot_id = "boot"
        def version(self, timeout): return Path(os.environ["VERSION_FILE"]).read_text().strip()
        def jobs(self, timeout): return []
        def observe(self, timeout): return {"active": True, "generation": ["boot",42,100], "proved": False}
        def restart(self, timeout):
            with open(os.environ["EVENT_LOG"], "a") as f: f.write(direction + "-refresh\\n")
            raise RuntimeError("observer failure must not veto activation")
    m.Refresh(str(Path(os.environ["EVENT_LOG"]).parent / "evidence.json"), host=Host(), owner_uid=os.getuid()).run("deployment", direction)
if os.environ.get("OBSERVER_FAIL_EXIT"):
    sys.exit(7)
'''.replace("REAL_HELPER", repr(str(Path(mojosec_refresh.__file__).resolve())))
            executable(package / "mojosec_refresh.py", helper_source)
            executable(stubs / "python3", '''#!/bin/bash
if [ "${1:-}" = - ]; then exec /usr/bin/python3 "$@"; fi
case "$*" in
  '-m mojo.deploy app-user '*) echo test-user ;;
  '-m pip show django-mojo') printf 'Version: '; cat "$VERSION_FILE" ;;
  '-m pip install django-mojo=='*) printf '%s\\n' "${4#django-mojo==}" > "$VERSION_FILE" ;;
  '-m mojo.deploy locate post_deploy.sh')
    if [ "$(cat "$VERSION_FILE")" = 1 ]; then echo "$OLD_POST"; else echo "$CANDIDATE_POST"; fi ;;
  *) exit 0 ;;
esac
''')
            executable(stubs / "git", '#!/bin/bash\ncase "$*" in *rev-parse*) echo 1111111111111111111111111111111111111111 ;; esac\nexit 0\n')
            executable(stubs / "flock", "#!/bin/bash\nexit 0\n")
            env = os.environ.copy()
            env.update(PATH=str(stubs) + ":/usr/bin:/bin", PROJ_PATH=str(project),
                       MOJO_DEPLOY_STATE_ROOT=str(state), MOJO_DEPLOY_NO_SYSTEMD="1",
                       VERSION_FILE=str(version), EVENT_LOG=str(log), OLD_POST=str(old_post),
                       CANDIDATE_POST=str(candidate_post))
            argv = ["bash", str(updater), "--sha", "2" * 40, "--framework", "2",
                    "--deployment", "11111111-1111-4111-8111-111111111111", "--node-type", "worker"]
            if closed_stderr:
                env["OBSERVER_FAIL_EXIT"] = "1"
                argv = ["bash", "-c", 'exec 2>&-; exec "$@"', "closed-stderr", *argv]
            done = subprocess.run(argv,
                                  env=env, capture_output=True, text=True, timeout=20)
            events = log.read_text().splitlines() if log.exists() else []
            assert done.returncode != 0, "application probe failure must retain its original failed deployment outcome"
            assert events.count("candidate-refresh") == 1, f"{updater.name}: modern/bridge failed attempts must deduplicate: {events} {done.stderr}"
            assert events.index("candidate-refresh") < events.index("profile-preflight"), "refresh must precede custom activation"
            assert events.count("rollback-refresh") == 1, f"{updater.name}: rollback hooks must share one durable attempt"
            assert events.index("rollback-refresh") < events.index("previous-activation"), "rollback refresh must run after downgrade before saved activation"
            assert version.read_text().strip() == "1", "application rollback must still restore the prior framework"


@th.django_unit_test()
def test_predecessor_and_modern_updaters_refresh_candidate_and_rollback_without_veto(opts):
    _exercise_refresh_updaters(opts)


@th.django_unit_test()
def test_closed_stderr_observer_failures_preserve_activation_and_rollback(opts):
    from mojo.deploy import mojosec_refresh

    # Both real updaters must reach candidate preflight, then complete rollback
    # for the genuine application probe failure even if every observer hook
    # exits nonzero and every shell diagnostic writes to a closed descriptor.
    _exercise_refresh_updaters(opts, closed_stderr=True)
    repo = Path(mojosec_refresh.__file__).resolve().parents[2]
    post_source = (repo / "mojo/deploy/project_scripts/post_deploy.sh").read_text()
    with tempfile.TemporaryDirectory() as root:
        root = Path(root)
        scripts, state, project = root / "project_scripts", root / "state", root / "project"
        for directory in (scripts, state, project):
            directory.mkdir()
        post = scripts / "post_deploy.sh"
        executable(post, post_source)
        for helper in (root / "mojosec_refresh.py", state / "mojosec_refresh.py"):
            executable(helper, "raise SystemExit(7)\n")
        env = os.environ.copy()
        env["PROJ_PATH"] = str(project)
        for action, expected in (("--activate", "Code-only deployment complete"),
                                 ("--activate-previous", "Previous code-only node restored")):
            done = subprocess.run(
                ["bash", "-c", 'exec 2>&-; exec bash "$@"', "closed-stderr",
                 str(post), action, "--node-type", "code", "--state", str(state)],
                env=env, capture_output=True, text=True, timeout=10)
            assert done.returncode == 0, f"{action} must retain successful application activation despite observer/stderr failure"
            assert expected in done.stdout, f"{action} must reach the actual application activation branch"


@th.django_unit_test()
def test_modern_publication_recovery_refreshes_before_publishing_and_never_tracks_sensor_as_app_unit(opts):
    from mojo.deploy import mojosec_refresh

    repo = Path(mojosec_refresh.__file__).resolve().parents[2]
    update = (repo / "mojo/deploy/project_scripts/update.sh").read_text()
    recovery = update[update.index('if [ -f "$ACTIVE/activation_succeeded" ]; then'):]
    assert recovery.index("refresh_mojosec candidate") < recovery.index("publish_success"), "publication recovery must recheck candidate sensor before publishing"
    post = (repo / "mojo/deploy/project_scripts/post_deploy.sh").read_text()
    assert 'record_unit "mojosec.service"' not in post, "sensor restart must never enter application unit rollback state"
    previous = (Path(__file__).parent / "fixtures/pre_refresh_update.sh").read_text()
    assert "mojosec_refresh" not in previous, "predecessor fixture must preserve the exact pre-adoption limitation"
