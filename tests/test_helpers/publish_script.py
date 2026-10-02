"""Maestro item #6348 — what publish.py runs, in what order, and what stops it.

publish.py is loaded as this test's own module instance. On that instance
`run` records commands instead of running them, `say` records the lines the
script would print, and the checks that reach git, the package index, maestro
or the process environment are stubs. Nothing is built, pushed or uploaded.
"""
import importlib.util
from pathlib import Path
import tempfile

from testit import helpers as th
from testit.helpers import assert_true, assert_eq

TESTIT_TIER = "framework"
ROOT = Path(__file__).resolve().parents[2]
PROJECT = 42
WHEEL_CHECK = "scripts/release_wheel_only.py"
CLEAN_TREE = "<clean-tree check>"


class Harness:
    """One loaded publish.py, with what it ran and what it said."""

    def __init__(self, fail_on=None, dirty_after_build=False):
        spec = importlib.util.spec_from_file_location(
            "publish_script_under_test", ROOT / "publish.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.module = module
        self.calls = []
        self.lines = []
        self.fail_on = fail_on
        self.dirty_after_build = dirty_after_build
        self.requests = []

        module.PYPROJECT_FILE = ROOT / "pyproject.toml"
        module.INIT_FILE = ROOT / "mojo/__init__.py"
        module.LOCK_FILE = ROOT / "uv.lock"
        module.run = self.run
        module.say = self.say
        module.load_env = lambda path, environ: None
        module.validate_environment = lambda args: None
        module.require_clean_tree = self.require_clean_tree
        module.require_unreleased = lambda version: None
        module.maestro_credentials = lambda: (None, None, None, PROJECT)
        module.maestro_request = self.maestro_request
        module.wait_for_pypi_visibility = lambda version: True

    def run(self, argv, dry_run=False, capture=True):
        self.calls.append((list(argv), dry_run))
        if dry_run:
            return ""
        if self.fail_on and self.fail_on in argv:
            raise self.module.PublishError(f"command failed: {self.fail_on}")
        if argv[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            return "main"
        return ""

    def say(self, message, prefix="==> "):
        self.lines.append(f"{prefix}{message}")

    def require_clean_tree(self):
        self.calls.append(([CLEAN_TREE], False))
        if self.dirty_after_build and self.executed("uv", "build"):
            raise self.module.PublishError("the working tree has uncommitted changes")

    def maestro_request(self, *args, **kwargs):
        self.requests.append(args)
        raise self.module.PublishError("the test allows no maestro request")

    def main(self, *argv):
        """Exit code, or None when the script ran to its end."""
        try:
            self.module.main(list(argv))
        except SystemExit as stop:
            return stop.code
        return None

    def position(self, *prefix, dry_run=False):
        """Where a command starting with `prefix` was run, or -1."""
        for index, (argv, was_dry) in enumerate(self.calls):
            if argv[:len(prefix)] == list(prefix) and was_dry == dry_run:
                return index
        return -1

    def executed(self, *prefix):
        return self.position(*prefix) >= 0

    def find(self, *prefix, dry_run=False):
        index = self.position(*prefix, dry_run=dry_run)
        return self.calls[index][0] if index >= 0 else None

    def version(self):
        return self.module.get_current_version()


def _nothing_left_the_machine(harness, when):
    for prefix in (("git", "push"), ("uv", "publish"), ("git", "tag")):
        for dry_run in (False, True):
            assert_eq(harness.position(*prefix, dry_run=dry_run), -1,
                      f"{' '.join(prefix)} must not be reached {when}")


@th.unit_test("publish: with every check passing it builds, checks, pushes, uploads and tags, in that order")
def test_release_order(opts):
    harness = Harness()
    assert_eq(harness.main("--skip-notes"), None, "a clean release must run to its end")
    order = [
        harness.position("uv", "build", "--wheel"),
        harness.position(harness.module.sys.executable, WHEEL_CHECK),
        max(index for index, (argv, _) in enumerate(harness.calls) if argv == [CLEAN_TREE]),
        harness.position("git", "push", "origin", "main"),
        harness.position("uv", "publish"),
        harness.position("git", "tag", "-a"),
    ]
    assert_true(-1 not in order, f"a step of the release was never run: {order}")
    assert_eq(order, sorted(order),
              "build, wheel check, clean-tree re-check, push, upload and tag must run in that order")
    assert_eq(harness.lines[-1], f"==> released {harness.version()}",
              "a release with no agent step must end on the released line")


@th.unit_test("publish: a refused wheel leaves nothing pushed, uploaded or tagged")
def test_refused_wheel_stops_before_push(opts):
    for flags in (("--skip-notes",), ("--skip-notes", "--nopypi")):
        harness = Harness(fail_on=WHEEL_CHECK)
        assert_eq(harness.main(*flags), 1, f"a refused wheel must fail the run ({flags})")
        assert_true(harness.executed("uv", "build", "--wheel"),
                    f"the build must have been reached, or this test proves nothing ({flags})")
        _nothing_left_the_machine(harness, f"after a refused wheel ({flags})")


@th.unit_test("publish: a tree that changed during the build stops the release before the push")
def test_dirty_tree_after_build_stops_before_push(opts):
    harness = Harness(dirty_after_build=True)
    assert_eq(harness.main("--skip-notes"), 1,
              "a tree changed during the build must fail the run")
    assert_true(harness.executed(harness.module.sys.executable, WHEEL_CHECK),
                "the build and its checks must have finished first")
    _nothing_left_the_machine(harness, "when the tree changed during the build")


@th.unit_test("publish: the upload names the one checked wheel by its absolute path in a private folder")
def test_upload_names_the_wheel(opts):
    harness = Harness()
    assert_eq(harness.main("--skip-notes"), None, "a clean release must run to its end")
    build = harness.find("uv", "build")
    assert_eq(build[:4], ["uv", "build", "--wheel", "--out-dir"],
              "the build must make the wheel alone, into a folder it names")
    out_dir = Path(build[4])
    assert_true(out_dir.is_absolute(), f"the build folder must be absolute, got {out_dir}")
    assert_true(ROOT not in out_dir.parents and out_dir != ROOT,
                f"the build folder must be outside the checkout, got {out_dir}")
    assert_true(not out_dir.exists(), "the build folder must be removed when the run ends")
    module = harness.module
    for script in ("scripts/verify_admin_portal_package.py", WHEEL_CHECK):
        argv = harness.find(module.sys.executable, script)
        assert_eq(argv[argv.index("--dist") + 1], str(out_dir),
                  f"{script} must check the folder the build wrote")
    wheel = out_dir / f"django_mojo-{harness.version()}-py3-none-any.whl"
    assert_eq(harness.find("uv", "publish"), ["uv", "publish", str(wheel)],
              "the upload must name the checked wheel and nothing else")


@th.unit_test("publish: a dry run builds and checks for real, and pushes, uploads and tags nothing")
def test_dry_run_builds_for_real(opts):
    harness = Harness()
    assert_eq(harness.main("--dry-run", "--skip-notes"), None, "a clean dry run must run to its end")
    module = harness.module
    for prefix in ((module.sys.executable, "scripts/vendor_admin_portal.py"),
                   ("uv", "build", "--wheel"),
                   (module.sys.executable, "scripts/verify_admin_portal_package.py"),
                   (module.sys.executable, WHEEL_CHECK)):
        assert_true(harness.executed(*prefix),
                    f"a dry run must really run {' '.join(prefix[-2:])}")
    for prefix in (("git", "push"), ("uv", "publish"), ("git", "tag")):
        assert_true(not harness.executed(*prefix),
                    f"a dry run must not run {' '.join(prefix)}")
        assert_true(harness.position(*prefix, dry_run=True) >= 0,
                    f"a dry run must still show {' '.join(prefix)}")


@th.unit_test("publish: a failing check fails a dry run where it would fail the release")
def test_dry_run_fails_where_the_release_would(opts):
    harness = Harness(fail_on=WHEEL_CHECK)
    assert_eq(harness.main("--dry-run", "--skip-notes"), 1,
              "a dry run must fail on a refused wheel")


@th.unit_test("publish: --note-by-agent asks maestro nothing and ends a real run on the agent's next step")
def test_note_by_agent_real_run(opts):
    harness = Harness()
    assert_eq(harness.main("--note-by-agent"), None, "a clean release must run to its end")
    assert_eq(harness.requests, [], "--note-by-agent must make no maestro request")
    assert_true(any("not checked by this script" in line for line in harness.lines),
                "the script must say it did not check the note")
    assert_true(harness.executed("git", "tag", "-a"), "the release must have been tagged")
    assert_eq(harness.lines[-1],
              f'NEXT: publish_release(project={PROJECT}, version="{harness.version()}")',
              "the agent's next step must be the last line of a real run")


@th.unit_test("publish: a dry run with --note-by-agent never tells the agent to publish the note")
def test_note_by_agent_dry_run(opts):
    harness = Harness()
    assert_eq(harness.main("--dry-run", "--note-by-agent"), None,
              "a clean dry run must run to its end")
    assert_eq(harness.requests, [], "--note-by-agent must make no maestro request")
    assert_true(not any("NEXT" in line or "publish_release" in line for line in harness.lines),
                f"a dry run must not print the publish step, got {harness.lines[-3:]}")
    assert_true(any("release note step skipped" in line for line in harness.lines),
                "a dry run must say the note step was skipped")


@th.unit_test("publish: a failed real run with --note-by-agent prints no next step")
def test_note_by_agent_failed_run(opts):
    harness = Harness(fail_on="publish")
    assert_eq(harness.main("--note-by-agent"), 1, "a failed upload must fail the run")
    assert_true(not any("NEXT" in line for line in harness.lines),
                "a run that failed must not tell the agent to publish the note")


@th.unit_test("publish: --note-by-agent together with --skip-notes is refused before anything runs")
def test_note_flags_are_exclusive(opts):
    harness = Harness()
    assert_eq(harness.main("--note-by-agent", "--skip-notes"), 2,
              "the two note flags together must be refused")
    assert_eq(harness.calls, [], "nothing may run when the flags are refused")


@th.unit_test("publish: without a note flag the note is still required from maestro")
def test_note_is_required_by_default(opts):
    harness = Harness()
    assert_eq(harness.main(), 1, "a release with no reachable note must be refused")
    assert_eq(len(harness.requests), 1, "the default path must ask maestro for the note")
    assert_true(not harness.executed("uv", "build"), "the note gate must come before the build")


@th.unit_test("publish: load_env fills a mapping from a file and keeps what is already set")
def test_load_env(opts):
    spec = importlib.util.spec_from_file_location(
        "publish_script_env_under_test", ROOT / "publish.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / ".env"
        path.write_text("# a comment\nUV_PUBLISH_TOKEN=from-file\n\n KEPT = spaced \nno_equals_sign\nSET=new\n")
        environ = {"SET": "already"}
        module.load_env(Path(directory) / "missing.env", environ)
        assert_eq(environ, {"SET": "already"}, "a missing file must change nothing")
        module.load_env(path, environ)
        assert_eq(environ, {"SET": "already", "UV_PUBLISH_TOKEN": "from-file", "KEPT": "spaced"},
                  "load_env must fill the mapping and keep what was already set")


@th.unit_test("publish: the wheel name agrees with the wheel check")
def test_wheel_name_agrees_with_the_check(opts):
    harness = Harness()
    spec = importlib.util.spec_from_file_location(
        "release_wheel_only_for_publish_test", ROOT / WHEEL_CHECK)
    check = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(check)
    assert_eq(harness.module.WHEEL_NAME.format(version="9.9.9"), check.wheel_name("9.9.9"),
              "publish.py must upload the file name the wheel check requires")
