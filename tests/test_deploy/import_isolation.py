"""mojo/deploy/ must import and run with NO Django settings configured.

This is the highest-value coverage in the package. `config_sync` is what puts
django.conf on the node in the first place, so it runs before settings can
possibly exist — and `python3 -m mojo.deploy.config_sync` imports the whole
`mojo` package on the way in. If any future release adds a settings-touching
import to that path, config fetch breaks on every node of every project at
once, and nothing else in this repo would notice.

Each test spawns a real subprocess with DJANGO_SETTINGS_MODULE stripped,
because an in-process check would be answered by a test runner that has
already configured Django.

THE MODULE LIST COMES FROM THE FILESYSTEM, not from a tuple maintained by hand.
It used to name six modules; the package had thirteen, so more than half of it
was uncovered and a new one arrived uncovered by default — which is the exact
failure mode this file exists to prevent.

Deliberately `os.walk` and NOT `pkgutil.walk_packages`: pkgutil imports each
package it descends into, in THIS process, which has Django configured. It would
answer the question with the wrong interpreter and report a clean result for a
module that cannot import on a bootstrap node.
"""

import os
import subprocess
import sys
import tempfile

from testit import helpers as th


def _repo_root():
    import mojo
    return os.path.dirname(os.path.dirname(os.path.abspath(mojo.__file__)))


def _deploy_root():
    import mojo.deploy
    return os.path.dirname(os.path.abspath(mojo.deploy.__file__))


def _discover_modules():
    """Every importable module under mojo/deploy/, as dotted names.

    Skipped: `__init__.py` (imported as a side effect of importing anything
    inside the package), `__main__.py` (running it IS the import, and it is
    covered by the `-m` tests below), anything else starting with an underscore
    (private by convention), and `__pycache__`.
    """
    root = _deploy_root()
    found = []
    for directory, subdirectories, files in os.walk(root):
        subdirectories[:] = [name for name in subdirectories
                             if name != "__pycache__"]
        for name in files:
            if not name.endswith(".py") or name.startswith("_"):
                continue
            relative = os.path.relpath(os.path.join(directory, name), root)
            dotted = relative[:-3].replace(os.sep, ".")
            found.append(f"mojo.deploy.{dotted}")
    return sorted(found)


def _settings_free_env():
    env = dict(os.environ)
    env.pop("DJANGO_SETTINGS_MODULE", None)
    env["PYTHONPATH"] = _repo_root()
    return env


def _run(args):
    return subprocess.run(
        [sys.executable] + args,
        env=_settings_free_env(), capture_output=True, text=True, timeout=120)


DEPLOY_MODULES = _discover_modules()

_IMPORT_ALL = "import " + ", ".join(DEPLOY_MODULES)


@th.django_unit_test("the module list is read off disk, so a new module cannot arrive uncovered")
def test_discovery_finds_every_module_on_disk(opts):
    found = _discover_modules()

    th.assert_true(len(found) >= 13,
                   f"mojo/deploy has had at least thirteen modules since "
                   f"before this test was written — finding {len(found)} means "
                   f"the walk is looking in the wrong place: {found}")
    for expected in ("mojo.deploy.config_sync", "mojo.deploy.check_setup",
                     "mojo.deploy.check_node", "mojo.deploy.mojosec",
                     "mojo.deploy.firewall_broker", "mojo.deploy.audit"):
        th.assert_in(expected, found,
                     f"{expected} exists on disk and must be covered")

    th.assert_in("mojo.deploy.provision.spec", found,
                 f"modules inside a subpackage must be discovered too — that "
                 f"is most of what was added after this walk replaced a "
                 f"hardcoded tuple: {found}")
    th.assert_in("mojo.deploy.provision.plan", found,
                 "the provision DAG module must be covered")

    for dotted in found:
        leaf = dotted.rsplit(".", 1)[-1]
        th.assert_eq(leaf.startswith("_"), False,
                     f"{dotted} should have been skipped — dunder and private "
                     f"modules are not part of this contract")

    root = _deploy_root()
    on_disk = sum(1 for directory, subs, files in os.walk(root)
                  for name in files
                  if name.endswith(".py") and not name.startswith("_")
                  and "__pycache__" not in directory)
    th.assert_eq(len(found), on_disk,
                 "every non-private .py file under mojo/deploy must appear in "
                 "the list — a module that silently drops out of this test is "
                 "a module that can break config fetch on every node")


@th.django_unit_test()
def test_every_deploy_module_imports_without_django_settings(opts):
    done = _run(["-c", _IMPORT_ALL])

    th.assert_eq(done.returncode, 0,
                 f"mojo/deploy must import with no settings configured.\n"
                 f"stdout: {done.stdout}\nstderr: {done.stderr}")
    th.assert_eq(done.stderr.strip(), "",
                 f"importing mojo/deploy must be silent — a warning here means "
                 f"something on the boot path is reaching for settings: "
                 f"{done.stderr}")


@th.django_unit_test()
def test_config_sync_help_works_under_dash_m(opts):
    """Exactly the invocation shape the systemd unit uses."""
    done = _run(["-m", "mojo.deploy.config_sync", "--help"])

    th.assert_eq(done.returncode, 0,
                 f"`python3 -m mojo.deploy.config_sync --help` must exit 0.\n"
                 f"stderr: {done.stderr}")
    th.assert_in("usage:", done.stdout,
                 f"argparse usage must be printed, got: {done.stdout!r}")
    th.assert_in("mojo.deploy.config_sync", done.stdout,
                 f"prog must name the -m invocation an operator can copy, not "
                 f"config_sync.py: {done.stdout!r}")


@th.django_unit_test()
def test_check_setup_help_works_under_dash_m(opts):
    done = _run(["-m", "mojo.deploy.check_setup", "--help"])

    th.assert_eq(done.returncode, 0,
                 f"`python3 -m mojo.deploy.check_setup --help` must exit 0.\n"
                 f"stderr: {done.stderr}")
    th.assert_in("usage:", done.stdout,
                 f"argparse usage must be printed, got: {done.stdout!r}")
    th.assert_in("mojo.deploy.check_setup", done.stdout,
                 f"prog must name the -m invocation: {done.stdout!r}")


@th.django_unit_test()
def test_jobman_help_works_under_dash_m(opts):
    """The shape the shim execs, on a node with no settings on disk."""
    done = _run(["-m", "mojo.deploy.jobman", "--help"])

    th.assert_eq(done.returncode, 0,
                 f"`python3 -m mojo.deploy.jobman --help` must exit 0.\n"
                 f"stderr: {done.stderr}")
    th.assert_in("usage:", done.stdout,
                 f"argparse usage must be printed, got: {done.stdout!r}")
    th.assert_in("mojo.deploy.jobman", done.stdout,
                 f"prog must name the -m invocation an operator can copy, not "
                 f"jobman.py: {done.stdout!r}")


@th.django_unit_test()
def test_node_setup_help_works_under_dash_m(opts):
    done = _run(["-m", "mojo.deploy.node_setup", "--help"])

    th.assert_eq(done.returncode, 0,
                 f"`python3 -m mojo.deploy.node_setup --help` must exit 0.\n"
                 f"stderr: {done.stderr}")
    th.assert_in("usage:", done.stdout,
                 f"argparse usage must be printed, got: {done.stdout!r}")
    th.assert_in("mojo.deploy.node_setup", done.stdout,
                 f"prog must name the -m invocation: {done.stdout!r}")


@th.django_unit_test()
def test_mojo_helpers_logit_is_never_left_imported(opts):
    """mojo.helpers.logit reads paths.LOG_ROOT at module level, and paths.py
    only creates that attribute inside configure_paths(). A well-meaning
    `from mojo.helpers import logit` added to mojo/deploy/ therefore fails on a
    bootstrap node — this catches it at test time rather than at 3am on a fleet
    reboot."""
    done = _run(["-c", (
        "import sys; " + _IMPORT_ALL + "; "
        "print('logit' if 'mojo.helpers.logit' in sys.modules else 'clean')")])

    th.assert_eq(done.returncode, 0,
                 f"the probe itself must run: {done.stderr}")
    th.assert_eq(done.stdout.strip(), "clean",
                 "mojo.helpers.logit must not survive importing mojo/deploy "
                 "with no settings configured — mojo.helpers.* is off-limits "
                 "inside that package (see mojo/deploy/__init__.py)")


@th.django_unit_test()
def test_export_scripts_works_settings_free_under_dash_m(opts):
    with tempfile.TemporaryDirectory() as root:
        done = _run([
            "-m", "mojo.deploy", "export-scripts", "--dest", root])
        th.assert_eq(done.returncode, 0,
                     f"export-scripts must not need Django settings: {done.stderr}")
        th.assert_true(os.path.isfile(os.path.join(root, "update.sh")),
                       "the settings-free command must export update.sh")
