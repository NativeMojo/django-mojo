"""Maestro item #6348 — a release uploads one checked wheel and nothing else.

`uv publish` uploads everything in dist/. The source archive used to go up
with the wheel, and it packed every agent worktree; 1.31.4 was half-uploaded
when the index refused it for size. scripts/release_wheel_only.py removes the
source archive and refuses a wheel that holds anything git does not track.
Each test builds its own dist/ in a temporary directory.
"""
import importlib.util
from pathlib import Path
import tempfile
import zipfile

from testit import helpers as th
from testit.helpers import assert_true, assert_eq

TESTIT_TIER = "framework"
ROOT = Path(__file__).resolve().parents[2]
VERSION = "9.9.9"
TRACKED = {"mojo/__init__.py", "mojo/apps/account/models/user.py", "testit/helpers.py"}


def _load():
    spec = importlib.util.spec_from_file_location(
        "release_wheel_only_under_test", ROOT / "scripts/release_wheel_only.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_wheel(dist, module, members, name=None):
    path = dist / (name or module.wheel_name(VERSION))
    with zipfile.ZipFile(path, "w") as archive:
        for member in members:
            archive.writestr(member, "x")
        archive.writestr(f"django_mojo-{VERSION}.dist-info/METADATA", "Name: django-mojo")
    return path


def _refused(module, dist, tracked=None, **kwargs):
    try:
        module.check(dist, VERSION, TRACKED if tracked is None else tracked, **kwargs)
    except module.ReleaseWheelError as error:
        return str(error)
    return None


@th.unit_test("release wheel: a wheel of tracked files passes, and the source archive is removed")
def test_clean_wheel_passes(opts):
    module = _load()
    with tempfile.TemporaryDirectory() as directory:
        dist = Path(directory)
        (dist / ".gitignore").write_text("*")
        (dist / f"django_mojo-{VERSION}.tar.gz").write_bytes(b"source archive")
        wheel = _write_wheel(dist, module, TRACKED)

        removed = module.remove_source_archives(dist)
        assert_eq(removed, [f"django_mojo-{VERSION}.tar.gz"],
                  "the source archive must be removed before upload")
        assert_eq(module.check(dist, VERSION, TRACKED), wheel,
                  "a wheel holding exactly the tracked files must pass")
        left = sorted(path.name for path in dist.iterdir())
        assert_eq(left, [".gitignore", wheel.name],
                  "only the wheel may be left for `uv publish` to upload")


@th.unit_test("release wheel: a source archive left in dist/ is refused")
def test_source_archive_left_behind_is_refused(opts):
    module = _load()
    with tempfile.TemporaryDirectory() as directory:
        dist = Path(directory)
        (dist / f"django_mojo-{VERSION}.tar.gz").write_bytes(b"source archive")
        _write_wheel(dist, module, TRACKED)
        message = _refused(module, dist)
        assert_true(message and "must hold exactly" in message,
                    f"a second file in dist/ must be refused, got {message!r}")


@th.unit_test("release wheel: another version's wheel, or none, is refused")
def test_wrong_or_missing_wheel_is_refused(opts):
    module = _load()
    with tempfile.TemporaryDirectory() as directory:
        dist = Path(directory)
        message = _refused(module, dist)
        assert_true(message and "found: nothing" in message,
                    f"an empty dist/ must be refused, got {message!r}")
        _write_wheel(dist, module, TRACKED, name="django_mojo-9.9.8-py3-none-any.whl")
        message = _refused(module, dist)
        assert_true(message and "must hold exactly" in message,
                    f"a wheel of another version must be refused, got {message!r}")


@th.unit_test("release wheel: a file git does not track inside a package is refused")
def test_untracked_file_in_package_is_refused(opts):
    module = _load()
    with tempfile.TemporaryDirectory() as directory:
        dist = Path(directory)
        _write_wheel(dist, module, TRACKED | {"mojo/local_only_notes.py"})
        message = _refused(module, dist)
        assert_true(message and "does not track" in message
                    and "mojo/local_only_notes.py" in message,
                    f"an untracked file inside a package must be refused by name, got {message!r}")


@th.unit_test("release wheel: a worktree copy or any other top-level path is refused")
def test_path_outside_packages_is_refused(opts):
    module = _load()
    with tempfile.TemporaryDirectory() as directory:
        dist = Path(directory)
        _write_wheel(dist, module, TRACKED | {".worktrees/1234-lead/mojo/__init__.py"})
        message = _refused(module, dist)
        assert_true(message and "outside its packages" in message,
                    f"a path outside the packages must be refused, got {message!r}")


@th.unit_test("release wheel: a missing tracked file is refused")
def test_missing_tracked_file_is_refused(opts):
    module = _load()
    with tempfile.TemporaryDirectory() as directory:
        dist = Path(directory)
        _write_wheel(dist, module, TRACKED - {"testit/helpers.py"})
        message = _refused(module, dist)
        assert_true(message and "missing tracked files" in message
                    and "testit/helpers.py" in message,
                    f"a wheel missing a tracked file must be refused by name, got {message!r}")


@th.unit_test("release wheel: a wheel over the index's file limit is refused")
def test_oversized_wheel_is_refused(opts):
    module = _load()
    with tempfile.TemporaryDirectory() as directory:
        dist = Path(directory)
        wheel = _write_wheel(dist, module, TRACKED)
        message = _refused(module, dist, max_bytes=wheel.stat().st_size - 1)
        assert_true(message and "over the index limit" in message,
                    f"a wheel over the limit must be refused, got {message!r}")
        assert_eq(module.MAX_FILE_BYTES, 100 * 1024 * 1024,
                  "the default limit must be the index's 100 MiB")


@th.unit_test("release wheel: publish.py runs the check after the build and before the push")
def test_publish_runs_the_check_before_push(opts):
    source = (ROOT / "publish.py").read_text(encoding="utf-8")
    build_at = source.index('run(["uv", "build"]')
    check_at = source.index('"scripts/release_wheel_only.py"')
    call_build = source.index("build(version, dry_run=args.dry_run)")
    call_push = source.index("push_source(branch, dry_run=args.dry_run)")
    call_upload = source.index("publish_to_pypi(dry_run=args.dry_run)")
    assert_true(build_at < check_at, "the wheel check must run after the build")
    assert_true(call_build < call_push < call_upload,
                "the build and its check must run before the push and the upload")
