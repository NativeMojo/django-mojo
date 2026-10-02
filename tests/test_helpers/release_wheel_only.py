"""Maestro item #6348 — a release uploads one checked wheel and nothing else.

The source archive used to go up with the wheel, and it packed every agent
worktree; 1.31.4 was half-uploaded when the index refused it for size. No
source archive is built now, and scripts/release_wheel_only.py refuses a build
folder holding anything but this version's wheel, and a wheel holding anything
git does not track. Each test builds its own folder in a temporary directory.
The last tests pin the build settings that keep a hand-run build clean.
"""
import importlib.util
from pathlib import Path
import re
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


@th.unit_test("release wheel: a wheel of tracked files, alone beside uv's .gitignore, passes")
def test_clean_wheel_passes(opts):
    module = _load()
    with tempfile.TemporaryDirectory() as directory:
        dist = Path(directory)
        (dist / ".gitignore").write_text("*")
        wheel = _write_wheel(dist, module, TRACKED)
        assert_eq(module.check(dist, VERSION, TRACKED), wheel,
                  "a wheel holding exactly the tracked files must pass")


@th.unit_test("release wheel: a source archive beside the wheel is refused, and left in place")
def test_source_archive_beside_the_wheel_is_refused(opts):
    module = _load()
    with tempfile.TemporaryDirectory() as directory:
        dist = Path(directory)
        (dist / f"django_mojo-{VERSION}.tar.gz").write_bytes(b"source archive")
        _write_wheel(dist, module, TRACKED)
        message = _refused(module, dist)
        assert_true(message and "must hold exactly" in message,
                    f"a second file in the build folder must be refused, got {message!r}")
        assert_true((dist / f"django_mojo-{VERSION}.tar.gz").exists(),
                    "the check must refuse a source archive, not quietly delete it")


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


@th.unit_test("release wheel: a local file packed as license metadata is refused")
def test_untracked_license_file_is_refused(opts):
    # Found in review (Brenda, #6226 note 63596): the build copies every file
    # matching LICENSE*/NOTICE* into the wheel's metadata, tracked or not.
    module = _load()
    tracked = TRACKED | {"LICENSE", "NOTICE"}
    with tempfile.TemporaryDirectory() as directory:
        dist = Path(directory)
        licenses = f"django_mojo-{VERSION}.dist-info/licenses/"
        wheel = _write_wheel(dist, module, TRACKED | {licenses + "LICENSE", licenses + "NOTICE"})
        assert_eq(module.check(dist, VERSION, tracked), wheel,
                  "tracked license files in the metadata must pass")
        wheel.unlink()
        _write_wheel(dist, module, TRACKED | {licenses + "LICENSE", licenses + "LICENSE.local.txt"})
        message = _refused(module, dist, tracked=tracked)
        assert_true(message and "LICENSE.local.txt" in message,
                    f"an untracked file packed as a license must be refused by name, got {message!r}")


@th.unit_test("release wheel: an unexpected file in the wheel's metadata is refused")
def test_unexpected_metadata_file_is_refused(opts):
    module = _load()
    with tempfile.TemporaryDirectory() as directory:
        dist = Path(directory)
        _write_wheel(dist, module, TRACKED | {f"django_mojo-{VERSION}.dist-info/notes.txt"})
        message = _refused(module, dist)
        assert_true(message and "notes.txt" in message,
                    f"a metadata file the build does not generate must be refused, got {message!r}")


def _toml_list(table, key):
    """The strings of `key = [...]` in one table of pyproject.toml.

    A regular expression, because tomllib is not in Python 3.10.
    """
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    body = re.search(r"^\[" + re.escape(table) + r"\]\n(.*?)(?=^\[|\Z)", text,
                     re.MULTILINE | re.DOTALL)
    if not body:
        return None
    found = re.search(r"^" + re.escape(key) + r"\s*=\s*\[(.*?)\]", body.group(1),
                      re.MULTILINE | re.DOTALL)
    if not found:
        return None
    return re.findall(r'"([^"]+)"', found.group(1))


@th.unit_test("release wheel: the check and the build settings name the same packages")
def test_packages_agree_with_the_build_settings(opts):
    module = _load()
    assert_eq(_toml_list("tool.hatch.build.targets.wheel", "packages"), list(module.PACKAGES),
              "the wheel's packages must be the ones the check allows")
    assert_eq(_toml_list("tool.hatch.build.targets.sdist", "only-include"), list(module.PACKAGES),
              "a hand-built source archive must be limited to the same packages")


@th.unit_test("release wheel: license files are named, never matched by pattern")
def test_license_files_are_named(opts):
    assert_eq(_toml_list("project", "license-files"), ["LICENSE", "NOTICE"],
              "a pattern would pack any local file named like a license")


@th.unit_test("release wheel: the committed .gitignore excludes agent worktrees and local session settings")
def test_gitignore_names_worktrees(opts):
    lines = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    for entry in (".worktrees/", ".claude/settings.local.json"):
        assert_true(entry in lines,
                    f"{entry} must be in .gitignore: a build tool does not read .git/info/exclude")
