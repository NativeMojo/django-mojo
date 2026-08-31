"""The node's application-account resolver, and the callers that must obey it.

Item #3429: a root jobs engine ran `sudo -n` on the packaged updater, the
nested sudo set SUDO_USER=root, and `update.sh` accepted it — every Git
operation then ran as root, which has no SSH identity for the checkout. The
fix derives APP_USER from trusted configuration (the deployed jobs cron entry,
an explicit candidate, the checkout owner — never SUDO_USER) and fails closed
before any mutation when nothing trusted resolves.

The two `update.sh` tests here are the bug's regression tests: they run the
real packaged script and fail on the pre-fix version, which accepted `root`
and entered the transaction. `flock` is stubbed where a scenario must get past
the lock (macOS ships no flock(1)); everything else on PATH is real.
"""

import os
import shutil
import subprocess
import sys
import tempfile

from testit import helpers as th


def _repo_root():
    import mojo
    return os.path.dirname(os.path.dirname(os.path.abspath(mojo.__file__)))


def _update_script():
    return os.path.join(
        _repo_root(), "mojo", "deploy", "project_scripts", "update.sh")


def _write_executable(path, body):
    with open(path, "w") as handle:
        handle.write("#!/bin/bash\n" + body)
    os.chmod(path, 0o755)


def _script_environment(root, project):
    """PATH = stubs, the running python's dir, and the system dirs — so
    `python3` inside update.sh is the interpreter that can import mojo."""
    stubs = os.path.join(root, "stubs")
    os.makedirs(stubs, exist_ok=True)
    empty_cron = os.path.join(root, "cron-empty")
    os.makedirs(empty_cron, exist_ok=True)
    environment = os.environ.copy()
    environment.update({
        "PATH": ":".join([stubs, os.path.dirname(sys.executable),
                          "/usr/bin", "/bin"]),
        "PROJ_PATH": project,
        "CRON_ETC": empty_cron,
        "MOJO_DEPLOY_NO_SYSTEMD": "1",
        "MOJO_DEPLOY_STATE_ROOT": os.path.join(root, "state"),
    })
    return environment, stubs


# ---------------------------------------------------------------------------
# update.sh — the regression tests
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_update_refuses_a_root_app_user_before_any_mutation(opts):
    """APP_USER=root with no trusted source must die at resolution.

    PROJ_PATH is `/` so the checkout-owner rung yields root, CRON_ETC is an
    empty directory so the cron rung misses, and the candidate is root — every
    rung fails, and the transaction must refuse before creating any state.
    The pre-fix script accepted root at its APP_USER default line, created the
    state root, and failed much later with an unrelated error.
    """
    with tempfile.TemporaryDirectory() as root:
        environment, _stubs = _script_environment(root, "/")
        environment["APP_USER"] = "root"
        done = subprocess.run(
            ["bash", _update_script(), "--manual"],
            env=environment, capture_output=True, text=True, timeout=30)

        th.assert_true(done.returncode != 0,
                       "update.sh accepted a root application account")
        th.assert_in("cannot resolve a non-root application account",
                     done.stderr,
                     "the refusal must name the fail-closed resolution")
        th.assert_true(
            not os.path.exists(environment["MOJO_DEPLOY_STATE_ROOT"]),
            "resolution must refuse before any transaction state exists")


@th.django_unit_test()
def test_update_overrides_a_poisoned_candidate_with_the_checkout_owner(opts):
    """APP_USER=root over a checkout owned by a real account self-heals.

    The poisoned candidate is rejected and the owner of PROJ_PATH (this test's
    own user) resolves instead — the script proceeds past resolution and dies
    at the first Git read, because the directory is not a repository. The
    pre-fix script kept root itself, so reaching this later error with a
    poisoned candidate proves resolution replaced it.
    """
    with tempfile.TemporaryDirectory() as root:
        project = os.path.join(root, "project")
        os.makedirs(project)
        environment, stubs = _script_environment(root, project)
        environment["APP_USER"] = "root"
        _write_executable(os.path.join(stubs, "flock"), "exit 0\n")
        done = subprocess.run(
            ["bash", _update_script(), "--manual"],
            env=environment, capture_output=True, text=True, timeout=30)

        th.assert_true(done.returncode != 0,
                       "a git-less checkout cannot deploy")
        th.assert_true(
            "cannot resolve a non-root application account" not in done.stderr,
            "a resolvable checkout owner must override the poisoned candidate")
        th.assert_in("cannot read current commit", done.stderr,
                     "the transaction must proceed past resolution and fail "
                     "at the first Git read, not at the account gate")


# ---------------------------------------------------------------------------
# the resolver
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_name_validation_rejects_every_dangerous_shape(opts):
    from mojo.deploy import app_user

    for name in ("", None, "root", "UNKNOWN", "-r", "-u", "0", "1000",
                 "bad name", "bad:name", "bad*name"):
        th.assert_true(not app_user.valid_app_user_name(name),
                       "name shape %r must be rejected" % (name,))
    for name in ("ec2-user", "www", "app.user", "a_b-c.d"):
        th.assert_true(app_user.valid_app_user_name(name),
                       "name shape %r must be accepted" % (name,))


@th.django_unit_test()
def test_full_validation_requires_a_real_non_root_account(opts):
    import getpass

    from mojo.deploy import app_user

    me = getpass.getuser()
    th.assert_true(app_user.valid_app_user(me),
                   "the current (non-root) account must validate")
    th.assert_true(not app_user.valid_app_user("root"),
                   "root must never validate")
    th.assert_true(
        not app_user.valid_app_user("no-such-account-3429"),
        "an account that does not exist must not validate")


@th.django_unit_test()
def test_the_cron_entry_outranks_an_explicit_candidate(opts):
    """The deployed cron entry is the fleet's statement of intent (#2246)."""
    import getpass

    from mojo.deploy import app_user

    me = getpass.getuser()
    with tempfile.TemporaryDirectory() as root:
        cron_path = os.path.join(root, "3_mojo_jobs")
        with open(cron_path, "w") as handle:
            handle.write("SHELL=/bin/bash\n"
                         "PATH=/usr/bin:/bin\n"
                         "# the schedule line follows\n"
                         "* * * * * %s /opt/api/bin/jobman start\n" % me)
        # `daemon` exists on both macOS and Linux and is not root, so it is a
        # VALID candidate — the cron entry must still win.
        resolved = app_user.resolve_app_user(
            root, candidate="daemon", cron_path=cron_path)
        th.assert_eq(resolved, me,
                     "the cron entry must outrank a valid explicit candidate")


@th.django_unit_test()
def test_the_ladder_falls_through_to_the_checkout_owner(opts):
    import getpass

    from mojo.deploy import app_user

    me = getpass.getuser()
    with tempfile.TemporaryDirectory() as root:
        missing_cron = os.path.join(root, "no-such-cron")
        th.assert_eq(
            app_user.resolve_app_user(
                root, candidate="daemon", cron_path=missing_cron),
            "daemon",
            "with no cron entry a valid candidate must win")
        th.assert_eq(
            app_user.resolve_app_user(
                root, candidate="root", cron_path=missing_cron),
            me,
            "a poisoned candidate must fall through to the checkout owner")
        th.assert_eq(
            app_user.resolve_app_user(
                root, candidate=None, cron_path=missing_cron),
            me,
            "with nothing else the checkout owner must resolve")
        th.assert_eq(
            app_user.resolve_app_user(
                "/", candidate=None, cron_path=missing_cron),
            None,
            "a root-owned checkout and no other rung must resolve to nothing")


@th.django_unit_test()
def test_the_cron_parser_reads_field_six_of_the_first_schedule_line(opts):
    from mojo.deploy import app_user

    with tempfile.TemporaryDirectory() as root:
        cron_path = os.path.join(root, "3_mojo_jobs")
        with open(cron_path, "w") as handle:
            handle.write("# comment first\n"
                         "SHELL=/bin/bash\n"
                         "MAILTO=\n"
                         "*/5 * * * * first-user /bin/true run\n"
                         "* * * * * second-user /bin/true run\n")
        th.assert_eq(app_user.cron_app_user(cron_path), "first-user",
                     "the first schedule line's sixth field must win")
        th.assert_eq(app_user.cron_app_user(os.path.join(root, "absent")),
                     None, "a missing cron file must read as nothing")


# ---------------------------------------------------------------------------
# the CLI faces — app-user and render
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_app_user_subcommand_resolves_and_fails_closed(opts):
    import getpass

    me = getpass.getuser()
    with tempfile.TemporaryDirectory() as root:
        empty_cron = os.path.join(root, "cron-empty")
        os.makedirs(empty_cron)
        environment = os.environ.copy()
        environment["CRON_ETC"] = empty_cron

        resolved = subprocess.run(
            [sys.executable, "-m", "mojo.deploy", "app-user",
             "--root", root, "--candidate", "root"],
            env=environment, capture_output=True, text=True)
        th.assert_eq(resolved.returncode, 0, resolved.stderr)
        th.assert_eq(resolved.stdout.strip(), me,
                     "the subcommand must print the resolved account")

        refused = subprocess.run(
            [sys.executable, "-m", "mojo.deploy", "app-user",
             "--root", "/", "--candidate", "root"],
            env=environment, capture_output=True, text=True)
        th.assert_true(refused.returncode != 0,
                       "an unresolvable node must fail closed")
        th.assert_eq(refused.stdout, "",
                     "a refusal must print no account at all")


@th.django_unit_test()
def test_render_refuses_a_root_app_user(opts):
    with tempfile.TemporaryDirectory() as root:
        done = subprocess.run(
            [sys.executable, "-m", "mojo.deploy", "render",
             "--dest", os.path.join(root, "out"),
             "--project-path", os.path.join(root, "proj"),
             "--app-user", "root"],
            capture_output=True, text=True)
        th.assert_true(done.returncode != 0,
                       "render must never stamp root into cron/systemd units")
        th.assert_true(
            not os.path.exists(os.path.join(root, "out")),
            "a refused render must write nothing")


# ---------------------------------------------------------------------------
# jobman demotion
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_demotion_argv_is_the_documented_sudo_form(opts):
    from mojo.deploy import jobman

    sudo = shutil.which("sudo") or "/usr/bin/sudo"
    th.assert_eq(
        jobman.demotion_argv("appu", "/opt/api", component="engine"),
        [sudo, "-H", "-u", "appu", "--", sys.executable,
         "-m", "mojo.deploy.jobman", "start", "engine",
         "--root", "/opt/api"],
        "a component start must re-exec exactly as sudo -H -u")
    th.assert_eq(
        jobman.demotion_argv("appu", "/opt/api"),
        [sudo, "-H", "-u", "appu", "--", sys.executable,
         "-m", "mojo.deploy.jobman", "start", "--root", "/opt/api"],
        "a bare start must re-exec both components")
    th.assert_eq(
        jobman.demotion_argv("appu", "/opt/api", runner="bin/other.py",
                             verbose=True),
        [sudo, "-H", "-u", "appu", "--", sys.executable,
         "-m", "mojo.deploy.jobman", "start", "--root", "/opt/api",
         "--runner", "bin/other.py", "--verbose"],
        "the re-exec must preserve the runner and verbosity")
