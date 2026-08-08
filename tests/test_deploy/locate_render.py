"""`python3 -m mojo.deploy` — the locate and render commands.

locate is an execution oracle for shims running under sudo, so its contract is
narrow and asserted exactly: allowlisted names resolve to existing packaged
files on stdout, everything else is one stderr line and exit 2. render is the
var/deploy materializer: full placeholder substitution, the
node_overrides.conf collision policy, and the shipped-file inventory the
downstream shim contract depends on.

Subprocess-based (DJANGO_SETTINGS_MODULE stripped) for the entry points —
these run on nodes with no settings configured, so the tests must too.
"""

import os
import shutil
import stat
import subprocess
import sys
import tempfile

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


def _render(dest, proj, extra=None):
    return _run(["-m", "mojo.deploy", "render", "--dest", dest,
                 "--project-path", proj] + (extra or []))


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        handle.write(text)


def _read(path):
    with open(path) as handle:
        return handle.read()


# ---------------------------------------------------------------------------
# locate
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_locate_resolves_both_packaged_scripts(opts):
    for name in ("update.sh", "post_deploy.sh"):
        done = _run(["-m", "mojo.deploy", "locate", name])
        th.assert_eq(done.returncode, 0,
                     f"locate {name} must exit 0: {done.stderr}")
        path = done.stdout.strip()
        th.assert_true(os.path.isabs(path),
                       f"locate must print an absolute path, got {path!r}")
        th.assert_true(os.path.isfile(path),
                       f"locate {name} must name an existing file: {path}")
        th.assert_true(path.endswith(os.path.join("scripts", name)),
                       f"locate {name} must resolve inside the package's "
                       f"scripts/ dir, got {path}")


@th.django_unit_test()
def test_locate_unknown_name_exits_2_one_stderr_line(opts):
    done = _run(["-m", "mojo.deploy", "locate", "rm_rf.sh"])
    th.assert_eq(done.returncode, 2,
                 "an unknown name must exit 2 — the shim's `|| fallback` "
                 "depends on it")
    th.assert_eq(done.stdout, "",
                 f"nothing may reach stdout for an unknown name (the shim "
                 f"would exec it): {done.stdout!r}")
    th.assert_eq(len(done.stderr.strip().splitlines()), 1,
                 f"exactly one stderr line, got: {done.stderr!r}")


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_render_substitutes_every_placeholder(opts):
    root = tempfile.mkdtemp(prefix="testit_render.")
    try:
        proj = os.path.join(root, "proj")
        dest = os.path.join(root, "out")
        os.makedirs(proj)
        done = _render(dest, proj, ["--app-user", "appu",
                                    "--web-user", "webu", "--workers", "7"])
        th.assert_eq(done.returncode, 0, f"render must exit 0: {done.stderr}")

        for sub in ("cron.d", "systemd"):
            names = os.listdir(os.path.join(dest, sub))
            th.assert_true(len(names) > 0,
                           f"render must populate {sub}/ — an empty set would "
                           "converge nothing into /etc")
            for name in names:
                path = os.path.join(dest, sub, name)
                text = _read(path)
                th.assert_true("@PROJ_PATH@" not in text
                               and "@APP_USER@" not in text
                               and "@WEB_USER@" not in text
                               and "@WORKERS@" not in text,
                               f"{sub}/{name} still carries a placeholder")
                mode = stat.S_IMODE(os.stat(path).st_mode)
                th.assert_eq(mode, 0o644,
                             f"{sub}/{name} must land 0644, got {oct(mode)}")

        unit = _read(os.path.join(dest, "systemd", "mojo-asgi.service"))
        th.assert_in("--workers 7", unit, "--workers must render the flag value")
        th.assert_in("User=webu", unit, "--web-user must render into the unit")
        cron = _read(os.path.join(dest, "cron.d", "3_mojo_jobs"))
        th.assert_in("appu", cron, "--app-user must render into 3_mojo_jobs")
        th.assert_in(proj, cron, "--project-path must render into the job line")
        certbot = _read(os.path.join(dest, "cron.d", "1_certbot"))
        th.assert_in("python3 -m mojo.deploy.certbot_sync", certbot,
                     "the certbot cron must invoke the packaged module")
        th.assert_in(f"--config {proj}/var/django.conf", certbot,
                     "the certbot cron must pass --config explicitly — cron's "
                     "environment carries no PROJ_PATH")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_render_collision_policy(opts):
    """Framework wins an undeclared collision (loudly); a declared override
    replaces the framework copy; non-colliding extras copy through."""
    root = tempfile.mkdtemp(prefix="testit_render.")
    try:
        proj = os.path.join(root, "proj")
        _write(os.path.join(proj, "aws", "cron.d", "3_mojo_jobs"),
               "* * * * * root /custom/fork.sh\n")
        _write(os.path.join(proj, "aws", "cron.d", "9_extra"),
               f"* * * * * root {proj}/bin/extra.sh\n")

        dest = os.path.join(root, "out_undeclared")
        done = _render(dest, proj)
        th.assert_eq(done.returncode, 0,
                     "an undeclared collision must not fail the render — "
                     f"fixes propagate by default: {done.stderr}")
        th.assert_in("collides with a framework template", done.stderr,
                     "the undeclared collision must be logged loudly")
        installed = _read(os.path.join(dest, "cron.d", "3_mojo_jobs"))
        th.assert_true("/custom/fork.sh" not in installed,
                       "the undeclared project copy must be inert")
        th.assert_in("jobman start", installed,
                     "the framework template must win the collision")
        th.assert_in("extra.sh",
                     _read(os.path.join(dest, "cron.d", "9_extra")),
                     "a non-colliding project extra must copy through")

        _write(os.path.join(proj, "aws", "node_overrides.conf"),
               "# project forks it deliberately\n3_mojo_jobs\n")
        dest2 = os.path.join(root, "out_declared")
        done = _render(dest2, proj)
        th.assert_eq(done.returncode, 0, f"declared render failed: {done.stderr}")
        th.assert_in("/custom/fork.sh",
                     _read(os.path.join(dest2, "cron.d", "3_mojo_jobs")),
                     "a declared override must install the project copy")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_render_fails_loudly_on_unwritable_dest(opts):
    root = tempfile.mkdtemp(prefix="testit_render.")
    try:
        blocked = os.path.join(root, "blocked")
        _write(blocked, "a plain file where the dest dir should go\n")
        done = _render(os.path.join(blocked, "sub"), os.path.join(root, "proj"))
        th.assert_true(done.returncode != 0,
                       "an uncreatable dest must fail the render — "
                       "post_deploy's die() gate depends on the exit code")
        th.assert_true(done.stderr.strip() != "",
                       "the failure must say why on stderr")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# shipped inventory
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_every_shipped_file_resolves_from_the_package(opts):
    """The exact inventory the shim/downstream contract depends on, resolved
    via mojo.deploy.__file__ — how a wheel install would find them."""
    import mojo.deploy
    base = os.path.dirname(os.path.abspath(mojo.deploy.__file__))

    expected = {
        "scripts": {"update.sh", "post_deploy.sh"},
        os.path.join("templates", "cron.d"): {
            "1_certbot", "2_mojo_cron", "3_mojo_jobs", "4_certbot_sync"},
        os.path.join("templates", "systemd"): {
            "mojo-asgi.service", "config-sync.service", "config-sync.timer"},
    }
    for sub, names in expected.items():
        for name in sorted(names):
            path = os.path.join(base, sub, name)
            th.assert_true(os.path.isfile(path),
                           f"{sub}/{name} must ship inside mojo.deploy "
                           f"(missing: {path})")
        actual = {n for n in os.listdir(os.path.join(base, sub))
                  if not n.startswith(".") and not n.endswith(".pyc")}
        th.assert_eq(actual, names,
                     f"{sub}/ must ship exactly the contracted set — a stray "
                     "file would be rendered/located into production")
