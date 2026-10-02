#!/usr/bin/env python
"""Leave only this version's wheel in dist/, and prove it holds tracked files only.

`uv publish` uploads everything in dist/, and an upload cannot be taken back.
The source archive is built because the packaging check compares it with the
wheel, but it is never uploaded: hatchling packs every file `.gitignore` does
not name, so it carried each agent worktree under `.worktrees/` and local
session files, and at 109.5 MiB the index refused it AFTER the wheel had gone
up (maestro #6348). A wheel names its packages, so it cannot pick those up; the
checks here cover what is left: a file inside a package that git does not
track, and a root file the build copies into the metadata as a license.

Run by publish.py after the build and before anything is pushed or uploaded.
Imports nothing from `mojo`, for the same reason publish.py does not.
"""
import argparse
from pathlib import Path
import subprocess
import sys
import zipfile

# The index's default limit for one file.
MAX_FILE_BYTES = 100 * 1024 * 1024
PACKAGES = ("mojo", "testit")
# What the build itself writes into the wheel's metadata directory. Everything
# else there is a file it copied from the checkout.
GENERATED_METADATA = ("METADATA", "WHEEL", "RECORD", "entry_points.txt")
LICENSES_DIR = "licenses/"
SOURCE_ARCHIVE_GLOB = "django_mojo-*.tar.gz"


class ReleaseWheelError(Exception):
    pass


def wheel_name(version):
    return f"django_mojo-{version}-py3-none-any.whl"


def remove_source_archives(dist):
    removed = sorted(Path(dist).glob(SOURCE_ARCHIVE_GLOB))
    for path in removed:
        path.unlink()
    return [path.name for path in removed]


def _sample(names):
    names = sorted(names)
    more = f" (+{len(names) - 10} more)" if len(names) > 10 else ""
    return ", ".join(names[:10]) + more


def check(dist, version, tracked, max_bytes=MAX_FILE_BYTES):
    """Return the wheel's path, or raise ReleaseWheelError naming what is wrong.

    `tracked` is the set of paths git tracks in the checkout: the wheel's
    packages, and the root files the build copies in as license metadata.
    """
    dist = Path(dist)
    expected = wheel_name(version)
    # uv writes a dist/.gitignore; it is not a distribution and is not uploaded.
    found = sorted(path.name for path in dist.iterdir() if not path.name.startswith("."))
    if found != [expected]:
        raise ReleaseWheelError(
            f"dist/ must hold exactly {expected}; found: {_sample(found) or 'nothing'}")
    wheel = dist / expected
    size = wheel.stat().st_size
    if size > max_bytes:
        raise ReleaseWheelError(
            f"{expected} is {size} bytes, over the index limit of {max_bytes}")

    dist_info = f"django_mojo-{version}.dist-info/"
    packaged, licenses, metadata, stray = set(), set(), set(), set()
    with zipfile.ZipFile(wheel) as archive:
        for name in archive.namelist():
            if name.endswith("/"):
                continue
            if name.split("/", 1)[0] in PACKAGES:
                packaged.add(name)
            elif not name.startswith(dist_info):
                stray.add(name)
            elif name[len(dist_info):].startswith(LICENSES_DIR):
                # The build copies every root file matching LICENSE*, NOTICE*
                # and the like, tracked or not; the path under licenses/ is
                # the file's path in the checkout.
                licenses.add(name[len(dist_info) + len(LICENSES_DIR):])
            else:
                metadata.add(name[len(dist_info):])
    if stray:
        raise ReleaseWheelError(
            f"{expected} holds files outside its packages: {_sample(stray)}")
    unexpected = metadata - set(GENERATED_METADATA)
    if unexpected:
        raise ReleaseWheelError(
            f"{expected} holds metadata the build does not generate: {_sample(unexpected)}")
    tracked = set(tracked)
    untracked = (packaged | licenses) - tracked
    if untracked:
        raise ReleaseWheelError(
            f"{expected} holds files git does not track: {_sample(untracked)}")
    tracked_packages = {name for name in tracked if name.split("/", 1)[0] in PACKAGES}
    missing = tracked_packages - packaged
    if missing:
        raise ReleaseWheelError(
            f"{expected} is missing tracked files: {_sample(missing)}")
    return wheel


def tracked_files():
    result = subprocess.run(
        ["git", "ls-files", "-z"], capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise ReleaseWheelError(f"git ls-files failed: {result.stderr.strip()}")
    return {name for name in result.stdout.split("\0") if name}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, default=Path("dist"))
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    try:
        removed = remove_source_archives(args.dist)
        wheel = check(args.dist, args.version, tracked_files())
    except (ReleaseWheelError, OSError, zipfile.BadZipFile) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    for name in removed:
        print(f"removed {name}: source archives are not uploaded")
    print(f"{wheel.name}: {wheel.stat().st_size} bytes, tracked files only")
    return 0


if __name__ == "__main__":
    sys.exit(main())
