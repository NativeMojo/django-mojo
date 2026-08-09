"""mojo.deploy.node_setup — the idempotent node convergence step.

Two layers, and the lower one is the point.

The three actions take explicit target paths, so they are driven DIRECTLY
against tempdirs as a normal user: real mkdir, real chmod, real copies, real
writes. A suite that only ever exercised `--dry-run` would pass against an
implementation that chmods `os.walk`'s `dirnames` and therefore never touches
`var/` itself — losing exactly the setgid bit the whole ownership scheme rests
on. That case is an explicit assertion below.

The CLI layer on top is `--dry-run` only: it owns the plan text, the `would `
prefix, the converged `nothing to change` line, the non-root refusal, and the
standing guarantee that no plan ever contains a cert cron.

Out of reach and deliberately not faked: chown to a foreign uid (needs root) and
anything that would run `systemctl`. `install_units` is filesystem-only for
exactly that reason — `enable_timers` owns the systemctl half.
"""

import getpass
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from unittest import mock

from testit import helpers as th

MISSING_USER = "no-such-user-for-item-1610"
MISSING_GROUP = "no-such-group-for-item-1610"


def _repo_root():
    import mojo
    return os.path.dirname(os.path.dirname(os.path.abspath(mojo.__file__)))


def _tempdir():
    return tempfile.mkdtemp(prefix="testit_node_setup.")


def _write(path, text):
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    with open(path, "w") as handle:
        handle.write(text)
    return path


def _read(path):
    with open(path) as handle:
        return handle.read()


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def _run(args):
    """Spawn the real module, settings-free, the way a node invokes it."""
    env = dict(os.environ)
    env.pop("DJANGO_SETTINGS_MODULE", None)
    env["PYTHONPATH"] = _repo_root()
    return subprocess.run(
        [sys.executable, "-m", "mojo.deploy.node_setup"] + args,
        env=env, capture_output=True, text=True, timeout=120)


# ---------------------------------------------------------------------------
# var directories — real effects, no root
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_var_dirs_creates_the_tree_and_sets_setgid_on_var_itself(opts):
    from mojo.deploy import node_setup as ns

    base = _tempdir()
    try:
        var_root = os.path.join(base, "var")
        # owner "" means "leave ownership alone" — chown to a foreign uid needs
        # root, and ownership is not what this assertion is about.
        changes = ns.sync_var_dirs(var_root, "", False)

        th.assert_true(changes,
                       "creating the var tree from nothing must be reported as "
                       "a change, or a fresh node looks converged")
        th.assert_eq(_mode(var_root), 0o2775,
                     f"var/ ITSELF must be 2775. The setgid bit there is the "
                     f"whole point — it is what makes everything created later "
                     f"inherit the group the app reads through. An "
                     f"implementation that chmods only os.walk's dirnames "
                     f"misses this directory. Got: {oct(_mode(var_root))}")
        for name in ("logs", "pids", "keys"):
            path = os.path.join(var_root, name)
            th.assert_true(os.path.isdir(path),
                           f"var/{name} must be created — the jobs cron "
                           f"redirects into var/logs and jobman writes "
                           f"var/pids, so both have to exist first")
            th.assert_eq(_mode(path), 0o2775,
                         f"var/{name} must be 2775, got {oct(_mode(path))}")
    finally:
        shutil.rmtree(base, ignore_errors=True)


@th.django_unit_test()
def test_var_dirs_fixes_file_modes_and_then_changes_nothing(opts):
    from mojo.deploy import node_setup as ns

    base = _tempdir()
    try:
        var_root = os.path.join(base, "var")
        ns.sync_var_dirs(var_root, "", False)

        log_path = _write(os.path.join(var_root, "logs", "jobman.log"), "hi\n")
        os.chmod(log_path, 0o600)

        changes = ns.sync_var_dirs(var_root, "", False)
        th.assert_true(changes,
                       "a file with the wrong mode must be reported as a change")
        th.assert_eq(_mode(log_path), 0o664,
                     f"files under var/ must land at 0664 so the www group can "
                     f"read them, got {oct(_mode(log_path))}")

        th.assert_eq(ns.sync_var_dirs(var_root, "", False), [],
                     "a converged var tree must report no changes at all — "
                     "this action runs on every deploy")
    finally:
        shutil.rmtree(base, ignore_errors=True)


@th.django_unit_test()
def test_var_dirs_refuses_symlinks_without_mutating_their_targets(opts):
    from mojo.deploy import node_setup as ns

    base = _tempdir()
    try:
        var_root = os.path.join(base, "var")
        outside = _write(os.path.join(base, "outside-secret"), "secret\n")
        os.chmod(outside, 0o600)
        ns.sync_var_dirs(var_root, "", False)
        os.symlink(outside, os.path.join(var_root, "logs", "linked"))

        changes = ns.sync_var_dirs(var_root, "", False)

        th.assert_eq(_mode(outside), 0o600,
                     "var convergence must never chmod a symlink target")
        th.assert_true(any("refused 1 unsafe" in change for change in changes),
                       f"the symlink refusal must be visible: {changes}")
    finally:
        shutil.rmtree(base, ignore_errors=True)


@th.django_unit_test()
def test_var_dirs_refuses_file_to_symlink_race(opts):
    from mojo.deploy import node_setup as ns

    base = _tempdir()
    try:
        var_root = os.path.join(base, "var")
        ns.sync_var_dirs(var_root, "", False)
        victim = _write(os.path.join(var_root, "logs", "victim"), "safe\n")
        outside = _write(os.path.join(base, "outside-race"), "secret\n")
        os.chmod(outside, 0o600)
        real_open = ns.os.open
        swapped = {"done": False}

        def swap_before_open(path, flags, *args, **kwargs):
            if (path == "victim" and kwargs.get("dir_fd") is not None and
                    not swapped["done"]):
                os.unlink(victim)
                os.symlink(outside, victim)
                swapped["done"] = True
            return real_open(path, flags, *args, **kwargs)

        with mock.patch.object(ns.os, "open", side_effect=swap_before_open):
            changes = ns.sync_var_dirs(var_root, "", False)

        th.assert_eq(_mode(outside), 0o600,
                     "a file-to-symlink race must not chmod the outside target")
        th.assert_true(any("refused" in change for change in changes),
                       f"the race refusal must be visible: {changes}")
    finally:
        shutil.rmtree(base, ignore_errors=True)


@th.django_unit_test()
def test_var_dirs_unresolvable_owner_is_a_warning_not_a_refusal(opts):
    from mojo.deploy import node_setup as ns

    uid, gid = ns.resolve_owner(getpass.getuser())
    th.assert_eq(uid, os.getuid(),
                 f"resolving the current user must yield the current uid, got "
                 f"{uid} for {getpass.getuser()!r}")
    th.assert_true(gid is not None,
                   "a spec with no ':group' must fall back to the user's own "
                   "primary group rather than returning nothing")

    th.assert_eq(ns.resolve_owner("%s:%s" % (MISSING_USER, MISSING_GROUP)),
                 (None, None),
                 "an owner that does not exist on this box resolves to "
                 "(None, None) so the ownership pass is skipped")
    th.assert_eq(ns.resolve_owner(""), (None, None),
                 "an empty --owner means 'leave ownership alone'")

    base = _tempdir()
    try:
        var_root = os.path.join(base, "var")
        ns.sync_var_dirs(var_root, "%s:%s" % (MISSING_USER, MISSING_GROUP), False)

        th.assert_true(os.path.isdir(os.path.join(var_root, "pids")),
                       "an unresolvable --owner is a WARNING, not a refusal: an "
                       "operator on a box with no 'www' group still wants the "
                       "directories. A wrong group is recoverable; a node with "
                       "no var/pids is not")
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ---------------------------------------------------------------------------
# systemd units — filesystem half only, no systemctl
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_install_units_copies_timers_as_well_as_services(opts):
    from mojo.deploy import node_setup as ns

    base = _tempdir()
    try:
        units = os.path.join(base, "units")
        systemd = os.path.join(base, "systemd")
        os.makedirs(units)
        os.makedirs(systemd)
        _write(os.path.join(units, "config-sync.service"), "[Service]\n")
        _write(os.path.join(units, "config-sync.timer"), "[Timer]\n")
        _write(os.path.join(units, "mojo-asgi.service"), "[Service]\n")
        _write(os.path.join(units, "README.md"), "not a unit\n")

        changes, timers = ns.install_units(units, systemd, False)

        th.assert_true(os.path.isfile(os.path.join(systemd, "config-sync.timer")),
                       "*.timer must be copied as well as *.service — copying "
                       "only services is exactly why config-sync.timer sat in "
                       "every repo and was never installed on any box")
        th.assert_true(os.path.isfile(os.path.join(systemd, "config-sync.service")),
                       "services are still copied")
        th.assert_true(os.path.isfile(os.path.join(systemd, "mojo-asgi.service")),
                       "every service in the directory is copied")
        th.assert_true(not os.path.exists(os.path.join(systemd, "README.md")),
                       "only *.service and *.timer are installed — anything "
                       "else in the units directory is left alone")
        th.assert_eq(timers, ["config-sync.timer"],
                     f"only TIMERS are handed on to be enabled. mojo-asgi "
                     f"cannot start before var/django.conf exists, so enabling "
                     f"it here would fail on every fresh node. Got: {timers}")
        th.assert_eq(len(changes), 3,
                     f"one change per unit actually copied, got: {changes}")
        th.assert_eq(_mode(os.path.join(systemd, "config-sync.timer")), 0o644,
                     "installed units land at 0644")
    finally:
        shutil.rmtree(base, ignore_errors=True)


@th.django_unit_test()
def test_install_units_only_copies_what_differs(opts):
    from mojo.deploy import node_setup as ns

    base = _tempdir()
    try:
        units = os.path.join(base, "units")
        systemd = os.path.join(base, "systemd")
        os.makedirs(units)
        os.makedirs(systemd)
        _write(os.path.join(units, "config-sync.service"), "[Service]\n")
        _write(os.path.join(units, "config-sync.timer"), "[Timer]\n")
        ns.install_units(units, systemd, False)

        changes, timers = ns.install_units(units, systemd, False)
        th.assert_eq(changes, [],
                     f"byte-identical units must not be recopied — a needless "
                     f"copy makes every deploy report a change and triggers a "
                     f"daemon-reload for nothing. Got: {changes}")
        th.assert_eq(timers, ["config-sync.timer"],
                     "the timer list is what gets enabled, so it is reported "
                     "whether or not the unit needed copying")

        _write(os.path.join(units, "config-sync.timer"),
               "[Timer]\nOnUnitActiveSec=1min\n")
        changes, _ = ns.install_units(units, systemd, False)

        th.assert_eq(len(changes), 1,
                     f"only the unit whose bytes changed is recopied, got: "
                     f"{changes}")
        th.assert_in("config-sync.timer", changes[0],
                     f"the change must name the unit that moved, got: {changes}")
        th.assert_in("OnUnitActiveSec",
                     _read(os.path.join(systemd, "config-sync.timer")),
                     "the new bytes must actually land in /etc/systemd/system")
    finally:
        shutil.rmtree(base, ignore_errors=True)


@th.django_unit_test()
def test_install_units_missing_directory_is_a_quiet_skip(opts):
    from mojo.deploy import node_setup as ns

    base = _tempdir()
    try:
        changes, timers = ns.install_units(
            os.path.join(base, "no-units-here"), os.path.join(base, "systemd"),
            False)

        th.assert_eq((changes, timers), ([], []),
                     f"a project with no units directory is a normal shape, not "
                     f"an error — skip quietly and change nothing. Got: "
                     f"{(changes, timers)}")
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ---------------------------------------------------------------------------
# the jobs cron
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_cron_names_the_cron_user_and_writes_only_on_diff(opts):
    from mojo.deploy import node_setup as ns

    base = _tempdir()
    try:
        cron = os.path.join(base, "cron.d", "3_mojo_jobs")

        changes = ns.write_cron(cron, "/opt/api", "deploy", False)
        th.assert_eq(changes, ["write %s" % cron],
                     f"writing the cron must be reported once, got: {changes}")

        text = _read(cron)
        th.assert_in("* * * * * deploy /opt/api/bin/jobman start "
                     ">> /opt/api/var/logs/jobman.log 2>&1", text,
                     f"the user field comes from --cron-user, kept separate "
                     f"from --owner so an ownership fix cannot silently change "
                     f"which account runs the engine fleet-wide. Got: {text!r}")
        th.assert_true(text.startswith(
            "SHELL=/bin/bash\n"
            "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n"),
            f"the SHELL and PATH preamble must match the block this replaces "
            f"— cron runs with almost no environment. Got: {text!r}")
        th.assert_eq(_mode(cron), 0o644,
                     f"the cron file lands at 0644, got {oct(_mode(cron))}")

        th.assert_eq(ns.write_cron(cron, "/opt/api", "deploy", False), [],
                     "identical content must not be rewritten — this runs on "
                     "every deploy")
        th.assert_true(ns.write_cron(cron, "/opt/api", "ec2-user", False),
                       "a different --cron-user is different content and must "
                       "be written")
        th.assert_in("* * * * * ec2-user ", _read(cron),
                     "the rewritten cron must carry the new user")
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ---------------------------------------------------------------------------
# the CLI, --dry-run only
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_dry_run_plans_the_whole_change_set_and_writes_nothing(opts):
    from mojo.deploy import node_setup  # noqa: F401 — the module under test

    base = _tempdir()
    try:
        root = os.path.join(base, "proj")
        cron = os.path.join(base, "cron.d", "3_mojo_jobs")
        os.makedirs(root)

        done = _run(["--dry-run", "--root", root, "--cron-file", cron,
                     "--systemd-dir", os.path.join(base, "systemd"),
                     "--owner", ""])

        th.assert_eq(done.returncode, 0,
                     f"--dry-run must exit 0 as any user, stderr: {done.stderr}")
        for expected in [
                "would create %s" % os.path.join(root, "var"),
                "would create %s" % os.path.join(root, "var", "logs"),
                "would create %s" % os.path.join(root, "var", "pids"),
                "would create %s" % os.path.join(root, "var", "keys"),
                "would write %s" % cron]:
            th.assert_in(expected, done.stdout,
                         f"the plan must name every action, and --dry-run "
                         f"prefixes each with 'would '. Missing from: "
                         f"{done.stdout!r}")

        th.assert_true(not os.path.exists(os.path.join(root, "var")),
                       "--dry-run must create nothing — it is what an operator "
                       "reaches for to inspect a node without touching it")
        th.assert_true(not os.path.exists(cron),
                       "--dry-run must not write the cron file")
    finally:
        shutil.rmtree(base, ignore_errors=True)


@th.django_unit_test()
def test_dry_run_on_a_converged_node_reports_nothing_to_change(opts):
    from mojo.deploy import node_setup as ns

    base = _tempdir()
    try:
        root = os.path.join(base, "proj")
        cron = os.path.join(base, "cron.d", "3_mojo_jobs")
        os.makedirs(root)
        ns.sync_var_dirs(os.path.join(root, "var"), "", False)
        ns.write_cron(cron, root, "ec2-user", False)

        done = _run(["--dry-run", "--root", root, "--cron-file", cron,
                     "--systemd-dir", os.path.join(base, "systemd"),
                     "--owner", ""])

        th.assert_eq(done.returncode, 0,
                     f"a converged node exits 0, stderr: {done.stderr}")
        th.assert_eq(done.stdout.strip(), "node_setup: nothing to change",
                     f"re-running on a converged node must say so in one line "
                     f"and plan nothing — every deploy runs this. Got: "
                     f"{done.stdout!r}")
    finally:
        shutil.rmtree(base, ignore_errors=True)


@th.django_unit_test()
def test_no_plan_ever_contains_a_cert_cron(opts):
    """The `4_certbot_sync` pull tick and the gated `1_certbot` renew are a
    SAFETY UNIT: the pull is what creates a synced certificate lineage, and an
    ungated renew against one corrupts it, so the hazard and its gate have to be
    installed by the same block of the same run. node_setup taking half of that
    pair would leave nodes with a puller and no gate."""
    from mojo.deploy import node_setup  # noqa: F401 — the module under test

    base = _tempdir()
    try:
        root = os.path.join(base, "proj")
        os.makedirs(root)

        done = _run(["--dry-run", "--root", root,
                     "--cron-file", os.path.join(base, "cron.d", "3_mojo_jobs"),
                     "--systemd-dir", os.path.join(base, "systemd"),
                     "--owner", ""])

        th.assert_eq(done.returncode, 0,
                     f"the plan must be produced, stderr: {done.stderr}")
        th.assert_true("certbot" not in done.stdout.lower(),
                       f"node_setup must never plan a cert cron — that block "
                       f"stays with the project's own deploy script until the "
                       f"cert plane is retired in one change. Got: "
                       f"{done.stdout!r}")
        th.assert_true("1_certbot" not in done.stdout,
                       "no gated-renew cron may appear in any plan")
        th.assert_true("4_certbot_sync" not in done.stdout,
                       "no cert-pull cron may appear in any plan")
    finally:
        shutil.rmtree(base, ignore_errors=True)


@th.django_unit_test()
def test_non_root_without_dry_run_refuses(opts):
    from mojo.deploy import node_setup  # noqa: F401 — the module under test

    if os.geteuid() == 0:
        # The refusal under test cannot fire for root, and running the real
        # thing as root would write under /etc.
        return

    base = _tempdir()
    try:
        root = os.path.join(base, "proj")
        os.makedirs(root)

        done = _run(["--root", root,
                     "--cron-file", os.path.join(base, "cron.d", "3_mojo_jobs"),
                     "--systemd-dir", os.path.join(base, "systemd")])

        th.assert_eq(done.returncode, 1,
                     f"a real run as a non-root user must refuse — it writes "
                     f"under /etc. stdout: {done.stdout!r}")
        th.assert_in("root", done.stderr,
                     f"the refusal must say what is missing, got: "
                     f"{done.stderr!r}")
        th.assert_eq(done.stdout, "",
                     f"a refusal must not also print a plan, got: "
                     f"{done.stdout!r}")
    finally:
        shutil.rmtree(base, ignore_errors=True)
