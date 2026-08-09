#!/usr/bin/env python3
"""Converge a node's var directories, systemd units and jobs cron.

    sudo python3 -m mojo.deploy.node_setup --root /opt/api
    python3 -m mojo.deploy.node_setup --dry-run          # plan only, no root

Three idempotent actions, each safe to re-run on every deploy:

    var dirs   create var/{logs,pids,keys}, chown --owner, 2775 on directories
               (INCLUDING var/ itself — the setgid bit there is the point) and
               0664 on files; preserve var/mojosec.json as root:root 0600
    systemd    copy *.service AND *.timer whose bytes differ, daemon-reload only
               when something changed, then `enable --now` the TIMERS
    cron       write /etc/cron.d/3_mojo_jobs, whose user field is --cron-user

Nothing is written when nothing differs, and a converged node says so:

    node_setup: nothing to change

WHAT THIS DOES NOT TOUCH, ON PURPOSE:

    - Certificates. It writes NO cert cron and removes no cron file it did not
      write. The skeleton's `4_certbot_sync` pull tick and its gated `1_certbot`
      renew are a SAFETY UNIT — the pull is what creates a synced certificate
      lineage, and an ungated `certbot renew` against one corrupts it, so the
      hazard and its gate have to be installed by the same block of the same
      run. They stay in the project's own deploy script until the cert plane is
      retired in one change. Slicing half of that pair into framework code is
      how a node ends up with a puller and no gate.
    - nginx configuration. Per-project layout, not framework behaviour.
    - Services. Only timers are enabled. `mojo-asgi` cannot start before
      var/django.conf exists, so it waits for the operator; the sync timers are
      the opposite — they are what FETCHES django.conf and each no-ops
      harmlessly on an unconfigured box, so a fresh node that never enabled them
      would look fully installed and never converge.

--owner AND --cron-user ARE SEPARATE for a reason. --owner is var-directory
ownership only; --cron-user is the account the job engine runs as. Overloading
one flag onto both means an ownership fix silently changes which account runs
the engine on every node in the fleet.

Root is required for a real run — it writes under /etc — but --dry-run works as
anyone, which is how you inspect a node without touching it.

See `docs/django_developer/deploy/README.md`.
"""

import argparse
import grp
import os
import pwd
import shutil
import stat
import subprocess
import sys

DEFAULT_ROOT = "/opt/api"
DEFAULT_OWNER = "ec2-user:www"
DEFAULT_CRON_USER = "ec2-user"

SYSTEMD_DIR = "/etc/systemd/system"
CRON_PATH = "/etc/cron.d/3_mojo_jobs"

VAR_SUBDIRS = ("logs", "pids", "keys")

# setgid on directories so everything created under var/ inherits the group the
# app reads through; 0664 on files so the owner writes and the group reads.
DIR_MODE = 0o2775
FILE_MODE = 0o664
PROTECTED_VAR_FILES = {"mojosec.json": 0o600}
# Units and cron files are read by root daemons and by nobody else in particular.
INSTALL_MODE = 0o644

UNIT_SUFFIXES = (".service", ".timer")

# Byte-for-byte the block this replaces, with the user field parameterised.
CRON_TEMPLATE = """SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
* * * * * %(user)s %(root)s/bin/jobman start >> %(root)s/var/logs/jobman.log 2>&1
"""

# Logging: the repo's standard graceful-fallback idiom. On a node the except
# branch ALWAYS fires — mojo.helpers.logit reads paths.LOG_ROOT at module level
# and that attribute only exists once Django settings have configured the paths.
# See mojo/deploy/__init__.py for the full contract.
try:
    from mojo.helpers import logit
    log = logit.get_logger("node_setup", "node_setup.log")
except Exception:
    import logging
    log = logging.getLogger("node_setup")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def resolve_owner(spec):
    """"user:group" -> (uid, gid), or (None, None) when unset or unresolvable.

    Unresolvable is a WARNING, not a refusal. An operator running this on a box
    where `www` does not exist wants the directories and the cron anyway; a
    wrong group is recoverable, a node with no var/pids is not.
    """
    if not spec:
        return None, None
    user, _, group = spec.partition(":")
    try:
        uid = pwd.getpwnam(user).pw_uid
        gid = grp.getgrnam(group).gr_gid if group else pwd.getpwnam(user).pw_gid
        return uid, gid
    except KeyError as err:
        log.warning("cannot resolve --owner %r (%s) — leaving ownership alone",
                    spec, err)
        return None, None


def read_bytes(path):
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError:
        return None


def walk_tree(root):
    """(path, is_dir) for `root` and everything beneath it, root first.

    Yields the root itself explicitly and every subdirectory through its
    parent's dirnames, so each path appears exactly once. A chmod pass that
    walked only `dirnames` would silently skip var/ itself — which is the one
    directory whose setgid bit the whole scheme depends on.
    """
    if not os.path.isdir(root):
        return []
    entries = [(root, True)]
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames:
            entries.append((os.path.join(dirpath, name), True))
        for name in filenames:
            entries.append((os.path.join(dirpath, name), False))
    return entries


# ---------------------------------------------------------------------------
# actions — each takes explicit target paths so it can be driven from a test
# ---------------------------------------------------------------------------

def sync_var_dirs(var_root, owner, dry_run):
    """Create var/{logs,pids,keys} and normalise ownership and modes.

    Returns the list of changes; an already-converged tree returns [].
    """
    changes = []
    for path in [var_root] + [os.path.join(var_root, n) for n in VAR_SUBDIRS]:
        if os.path.isdir(path):
            continue
        changes.append("create %s" % path)
        if not dry_run:
            os.makedirs(path, exist_ok=True)

    uid, gid = resolve_owner(owner)
    wrong_mode = 0
    wrong_owner = 0
    for path, is_dir in walk_tree(var_root):
        try:
            info = os.stat(path)
        except OSError as err:
            log.debug("cannot stat %s: %s", path, err)
            continue

        relative = os.path.relpath(path, var_root)
        protected_mode = PROTECTED_VAR_FILES.get(relative)
        wanted = protected_mode if protected_mode is not None else (DIR_MODE if is_dir else FILE_MODE)
        if stat.S_IMODE(info.st_mode) != wanted:
            wrong_mode += 1
            if not dry_run:
                try:
                    os.chmod(path, wanted)
                except OSError as err:
                    log.warning("cannot chmod %s: %s", path, err)

        wanted_uid = 0 if protected_mode is not None else uid
        wanted_gid = 0 if protected_mode is not None else gid
        if wanted_uid is not None and (info.st_uid != wanted_uid or info.st_gid != wanted_gid):
            wrong_owner += 1
            if not dry_run:
                try:
                    os.chown(path, wanted_uid, wanted_gid)
                except OSError as err:
                    log.warning("cannot chown %s: %s", path, err)

    if wrong_mode:
        changes.append("chmod %d path(s) under %s (2775 dirs, 0664 files, "
                       "mojosec.json 0600)"
                       % (wrong_mode, var_root))
    if wrong_owner:
        changes.append("chown standard/protected ownership on %d path(s) under %s"
                       % (wrong_owner, var_root))
    return changes


def install_units(units_dir, systemd_dir, dry_run):
    """Copy every changed *.service and *.timer into `systemd_dir`.

    Returns (changes, timer names). Filesystem only — `systemctl` is
    enable_timers()'s job, which keeps this half drivable from a test and keeps
    a dry run from shelling out at all.

    Copying only *.service is why config-sync.timer sat in every repo and was
    never installed on any box.
    """
    if not os.path.isdir(units_dir):
        log.debug("no units directory at %s — nothing to install", units_dir)
        return [], []

    names = sorted(name for name in os.listdir(units_dir)
                   if name.endswith(UNIT_SUFFIXES))
    changes = []
    for name in names:
        source = os.path.join(units_dir, name)
        dest = os.path.join(systemd_dir, name)
        payload = read_bytes(source)
        if payload is None:
            log.warning("cannot read %s — skipping it", source)
            continue
        if payload == read_bytes(dest):
            continue
        changes.append("install %s -> %s" % (name, dest))
        if not dry_run:
            os.makedirs(systemd_dir, exist_ok=True)
            shutil.copyfile(source, dest)
            os.chmod(dest, INSTALL_MODE)

    timers = [name for name in names if name.endswith(".timer")]
    return changes, timers


def enable_timers(timers, reload_needed, dry_run):
    """daemon-reload when a unit changed, then `enable --now` each timer.

    Services are never enabled here — see the module docstring.
    """
    changes = []
    if not timers and not reload_needed:
        return changes

    if reload_needed:
        changes.append("systemctl daemon-reload")
        if not dry_run:
            run_systemctl(["daemon-reload"])

    for name in timers:
        if timer_enabled(name):
            continue
        changes.append("systemctl enable --now %s" % name)
        if not dry_run:
            run_systemctl(["enable", "--now", name])
    return changes


def write_cron(cron_path, root, cron_user, dry_run):
    """Install the every-minute `jobman start` tick, only when it differs."""
    content = cron_text(root, cron_user)
    if read_bytes(cron_path) == content.encode("utf-8"):
        return []

    changes = ["write %s" % cron_path]
    if not dry_run:
        parent = os.path.dirname(cron_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(cron_path, "w") as handle:
            handle.write(content)
        os.chmod(cron_path, INSTALL_MODE)
    return changes


def cron_text(root, cron_user):
    return CRON_TEMPLATE % {"user": cron_user, "root": root}


def run_systemctl(args):
    try:
        done = subprocess.run(["systemctl"] + args, capture_output=True,
                              text=True)
    except OSError as err:
        log.warning("systemctl %s failed: %s", " ".join(args), err)
        return False
    if done.returncode != 0:
        log.warning("systemctl %s failed (%d): %s", " ".join(args),
                    done.returncode, done.stderr.strip())
        return False
    return True


def timer_enabled(name):
    """`systemctl is-enabled <name>` says yes. Anything else — including no
    systemctl at all — reads as "not enabled", so the enable is attempted."""
    try:
        done = subprocess.run(["systemctl", "is-enabled", name],
                              capture_output=True, text=True)
    except OSError:
        return False
    return done.returncode == 0 and done.stdout.strip() == "enabled"


# ---------------------------------------------------------------------------
# entry
# ---------------------------------------------------------------------------

def plan(root, owner, cron_user, units_dir, systemd_dir, cron_path, dry_run):
    """Run (or, under dry_run, describe) all three actions. Returns the changes
    in the order they are applied."""
    changes = sync_var_dirs(os.path.join(root, "var"), owner, dry_run)
    unit_changes, timers = install_units(units_dir, systemd_dir, dry_run)
    changes += unit_changes
    changes += enable_timers(timers, bool(unit_changes), dry_run)
    changes += write_cron(cron_path, root, cron_user, dry_run)
    return changes


def main(argv):
    # prog is set explicitly: under `-m` argparse derives it from sys.argv[0]
    # and prints "node_setup.py", which is not an invocation an operator can copy.
    parser = argparse.ArgumentParser(
        prog="python3 -m mojo.deploy.node_setup",
        description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=None,
                        help="project root (default: $MOJO_PROJECT_ROOT, else %s)"
                             % DEFAULT_ROOT)
    parser.add_argument("--owner", default=DEFAULT_OWNER,
                        help="user:group owning var/ (default: %s). Ownership "
                             "ONLY — this never decides who runs the engine."
                             % DEFAULT_OWNER)
    parser.add_argument("--cron-user", default=DEFAULT_CRON_USER,
                        help="account the jobs cron runs as (default: %s)"
                             % DEFAULT_CRON_USER)
    parser.add_argument("--units-dir", default=None,
                        help="systemd units to install "
                             "(default: <root>/aws/nginx/systemd)")
    parser.add_argument("--systemd-dir", default=SYSTEMD_DIR,
                        help="where units are installed (default: %s)"
                             % SYSTEMD_DIR)
    parser.add_argument("--cron-file", default=CRON_PATH,
                        help="the jobs cron file (default: %s)" % CRON_PATH)
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change; change nothing")
    parser.add_argument("--verbose", action="store_true",
                        help="log the skipped and no-op paths too")
    args = parser.parse_args(argv)

    import logging
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s [node_setup] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S")

    if not args.dry_run and os.geteuid() != 0:
        print("node_setup: must run as root (it writes under /etc) — "
              "re-run with sudo, or use --dry-run", file=sys.stderr)
        return 1

    root = os.path.abspath(
        args.root or os.environ.get("MOJO_PROJECT_ROOT") or DEFAULT_ROOT)
    units_dir = args.units_dir or os.path.join(root, "aws", "nginx", "systemd")

    changes = plan(root, args.owner, args.cron_user, units_dir,
                   args.systemd_dir, args.cron_file, args.dry_run)

    prefix = "would " if args.dry_run else ""
    for change in changes:
        print("node_setup: %s%s" % (prefix, change))
    if not changes:
        print("node_setup: nothing to change")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
