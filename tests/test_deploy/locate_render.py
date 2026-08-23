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
    # The role filter reads this: an inherited value would make the
    # no-role compatibility assertions lie.
    env.pop("MOJO_NODE_ROLE", None)
    env["PYTHONPATH"] = _repo_root()
    return env


def _run(args, env=None):
    return subprocess.run(
        [sys.executable] + args,
        env=env or _settings_free_env(), capture_output=True, text=True,
        timeout=120)


def _render(dest, proj, extra=None, role=None):
    env = None
    if role is not None:
        env = _settings_free_env()
        env["MOJO_NODE_ROLE"] = role
    return _run(["-m", "mojo.deploy", "render", "--dest", dest,
                 "--project-path", proj] + (extra or []), env=env)


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
# role filtering and pruning
# ---------------------------------------------------------------------------

ROLE_MANIFEST = """\
# which converged files each role owns; unlisted names stay shared
api      cron.d/9_api_only
api      cron.d/9_shared_extra
worker   cron.d/9_worker_only
worker   cron.d/9_shared_extra
worker   cron.d/4_certbot_sync
worker   systemd/worker-drain.timer
"""


def _role_project(root):
    """A project whose repo describes two kinds of box."""
    proj = os.path.join(root, "proj")
    _write(os.path.join(proj, "aws", "node_roles.conf"), ROLE_MANIFEST)
    _write(os.path.join(proj, "aws", "cron.d", "9_api_only"),
           f"* * * * * root {proj}/bin/api_reports.sh\n")
    _write(os.path.join(proj, "aws", "cron.d", "9_worker_only"),
           f"* * * * * root {proj}/bin/drain.sh\n")
    _write(os.path.join(proj, "aws", "cron.d", "9_shared_extra"),
           f"* * * * * root {proj}/bin/shared.sh\n")
    _write(os.path.join(proj, "aws", "nginx", "systemd", "worker-drain.timer"),
           "[Unit]\nDescription=drain\n[Timer]\nOnBootSec=1min\n")
    return proj


def _names(dest, sub):
    return sorted(os.listdir(os.path.join(dest, sub)))


@th.django_unit_test()
def test_render_installs_only_the_names_this_role_owns(opts):
    """A role renders its own names plus every unlisted (shared) one, and none
    of the names another role owns — framework templates included."""
    root = tempfile.mkdtemp(prefix="testit_render.")
    try:
        proj = _role_project(root)
        dest = os.path.join(root, "out_api")
        done = _render(dest, proj, role="api")
        th.assert_eq(done.returncode, 0,
                     f"a role render must exit 0: {done.stderr}")

        crons = _names(dest, "cron.d")
        th.assert_in("9_api_only", crons,
                     f"the role's own extra must render: {crons}")
        th.assert_in("9_shared_extra", crons,
                     f"a name listed under BOTH roles is owned by both: {crons}")
        th.assert_in("1_certbot", crons,
                     f"an unlisted framework template stays shared: {crons}")
        th.assert_true("9_worker_only" not in crons,
                       f"another role's cron must not render: {crons}")
        th.assert_true("4_certbot_sync" not in crons,
                       f"a FRAMEWORK template another role owns must be "
                       f"filtered too — the manifest outranks the shipped "
                       f"set: {crons}")

        units = _names(dest, "systemd")
        th.assert_true("worker-drain.timer" not in units,
                       f"the systemd set is filtered by the same rule: {units}")
        th.assert_in("mojo-asgi.service", units,
                     f"the shared ASGI unit is installed on every node: {units}")
        th.assert_in(proj,
                     _read(os.path.join(dest, "cron.d", "9_api_only")),
                     "an owned project extra still copies through verbatim")

        dest = os.path.join(root, "out_worker")
        done = _render(dest, proj, role="worker")
        th.assert_eq(done.returncode, 0,
                     f"the other role must render too: {done.stderr}")
        crons = _names(dest, "cron.d")
        th.assert_in("9_worker_only", crons,
                     f"the worker's own extra must render: {crons}")
        th.assert_in("4_certbot_sync", crons,
                     f"the framework template the worker owns must render for "
                     f"it: {crons}")
        th.assert_true("9_api_only" not in crons,
                       f"filtering is symmetric across roles: {crons}")
        th.assert_in("worker-drain.timer", _names(dest, "systemd"),
                     "the worker's own timer must render for the worker")

        done = _render(os.path.join(root, "out_ghost"), proj, role="ghost")
        th.assert_true(done.returncode != 0,
                       "a role the manifest never declares must fail the "
                       "render closed, not silently strip the node")
        th.assert_in("not declared", done.stderr,
                     f"the refusal must name the cause: {done.stderr!r}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_render_prunes_names_it_did_not_write_but_not_after_a_failure(opts):
    """var/deploy IS the contract: a copy this render did not produce would be
    reinstalled into /etc by post_deploy and shield the structural sweep."""
    root = tempfile.mkdtemp(prefix="testit_render.")
    try:
        proj = _role_project(root)
        dest = os.path.join(root, "out")
        _write(os.path.join(dest, "cron.d", "9_worker_only"),
               "* * * * * root /an/older/render/left/this\n")
        _write(os.path.join(dest, "systemd", "worker-drain.timer"),
               "[Unit]\nDescription=stale\n")
        os.mkdir(os.path.join(dest, "cron.d", "a-directory"))

        done = _render(dest, proj, role="api")
        th.assert_eq(done.returncode, 0, f"render must exit 0: {done.stderr}")
        crons = _names(dest, "cron.d")
        th.assert_true("9_worker_only" not in crons,
                       f"a stale copy of another role's cron must be pruned — "
                       f"post_deploy would otherwise reinstall it: {crons}")
        th.assert_true("worker-drain.timer" not in _names(dest, "systemd"),
                       "the systemd set is pruned by the same rule")
        th.assert_in("1_certbot", crons,
                     f"pruning must not touch what this render wrote: {crons}")
        th.assert_in("a-directory", crons,
                     f"pruning removes regular files only, never directories: "
                     f"{crons}")
        th.assert_in("removed stale cron.d/9_worker_only", done.stdout,
                     f"a removal is logged, never silent: {done.stdout!r}")

        # A render that dies partway must leave the previous contract intact:
        # a half-pruned var/deploy is a contract nobody wrote.
        broken = os.path.join(root, "broken")
        _write(os.path.join(broken, "cron.d", "9_worker_only"), "stale\n")
        _write(os.path.join(broken, "systemd"), "not a directory\n")
        done = _render(broken, proj, role="api")
        th.assert_true(done.returncode != 0,
                       "a render that cannot create its dest must fail")
        th.assert_in("9_worker_only", _names(broken, "cron.d"),
                     "nothing may be pruned when the render failed partway")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_render_without_a_role_is_byte_identical_to_the_old_behavior(opts):
    """The compatibility assertion: with MOJO_NODE_ROLE unset, a manifest in
    the tree changes nothing at all."""
    root = tempfile.mkdtemp(prefix="testit_render.")
    try:
        proj = _role_project(root)
        with_manifest = os.path.join(root, "with_manifest")
        done = _render(with_manifest, proj)
        th.assert_eq(done.returncode, 0, f"render must exit 0: {done.stderr}")

        os.unlink(os.path.join(proj, "aws", "node_roles.conf"))
        without = os.path.join(root, "without_manifest")
        done = _render(without, proj)
        th.assert_eq(done.returncode, 0, f"render must exit 0: {done.stderr}")

        for sub in ("cron.d", "systemd"):
            th.assert_eq(_names(with_manifest, sub), _names(without, sub),
                         f"an unread manifest must not change the {sub} "
                         f"inventory")
            for name in _names(without, sub):
                th.assert_eq(_read(os.path.join(with_manifest, sub, name)),
                             _read(os.path.join(without, sub, name)),
                             f"{sub}/{name} must render byte-identically with "
                             f"the role filter unused")
        th.assert_in("9_worker_only", _names(with_manifest, "cron.d"),
                     "an unlabeled node still converges every name — that is "
                     "exactly the behavior every existing project has")
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
