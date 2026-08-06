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
"""

import os
import subprocess
import sys

from testit import helpers as th


def _repo_root():
    import mojo
    return os.path.dirname(os.path.dirname(os.path.abspath(mojo.__file__)))


def _settings_free_env():
    env = dict(os.environ)
    env.pop("DJANGO_SETTINGS_MODULE", None)
    env["PYTHONPATH"] = _repo_root()
    return env


def _run(args):
    return subprocess.run(
        [sys.executable] + args,
        env=_settings_free_env(), capture_output=True, text=True, timeout=120)


@th.django_unit_test()
def test_both_modules_import_without_django_settings(opts):
    done = _run(["-c", "import mojo.deploy.config_sync, mojo.deploy.check_setup"])

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
def test_mojo_helpers_logit_is_never_left_imported(opts):
    """mojo.helpers.logit reads paths.LOG_ROOT at module level, and paths.py
    only creates that attribute inside configure_paths(). A well-meaning
    `from mojo.helpers import logit` added to mojo/deploy/ therefore fails on a
    bootstrap node — this catches it at test time rather than at 3am on a fleet
    reboot."""
    done = _run(["-c", (
        "import sys, mojo.deploy.config_sync, mojo.deploy.check_setup; "
        "print('logit' if 'mojo.helpers.logit' in sys.modules else 'clean')")])

    th.assert_eq(done.returncode, 0,
                 f"the probe itself must run: {done.stderr}")
    th.assert_eq(done.stdout.strip(), "clean",
                 "mojo.helpers.logit must not survive importing mojo/deploy "
                 "with no settings configured — mojo.helpers.* is off-limits "
                 "inside that package (see mojo/deploy/__init__.py)")
