#!/usr/bin/env python3
"""Offline, locked, recoverable replacement of the owned Admin v2 tree."""

import argparse
import fcntl
import importlib.util
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile

REPO = Path(__file__).resolve().parents[1]
DESTINATION = REPO / "mojo/apps/account/admin_portal_v2"
_spec = importlib.util.spec_from_file_location("admin_artifact", REPO / "mojo/apps/account/services/admin_artifact.py")
artifact = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(artifact)


def vendor(source, expected, destination=DESTINATION, *, rename=os.rename):
    """A failed promotion restores the previous tree; a crash leaves a backup.

    The deterministic backup is recovered on the next invocation. The lock is
    outside package roots, and the stage/backup are outside the account tree.
    Callers must stop processes serving/importing this checkout first.
    """
    source = artifact.no_symlink_ancestry(source)
    destination = artifact.no_symlink_ancestry(destination)
    if source == destination or source in destination.parents or destination in source.parents:
        raise artifact.ArtifactError("source and destination overlap")
    # REPO is on the same filesystem as the package, without making transient
    # trees package data. Tests pass destinations under their private root.
    workspace = destination.parents[3] if destination == DESTINATION else destination.parent
    backup = workspace / ".admin-portal-v2.backup"
    artifact.no_symlink_ancestry(backup)
    for path in (destination, backup):
        if path.exists() and not path.is_dir():
            raise artifact.ArtifactError(f"expected a directory at {path}")
    if workspace.stat().st_dev != destination.parent.stat().st_dev:
        raise artifact.ArtifactError("vendor staging and destination must share a filesystem")
    lock_path = workspace / ".admin-portal-v2.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as lock:
        if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
            raise artifact.ArtifactError("vendor lock must be a regular file")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if backup.exists():
            if not destination.exists():
                rename(backup, destination)
            else:
                artifact.validate(destination, expected)
                shutil.rmtree(backup)
        result = artifact.validate(source, expected)
        try:
            if artifact.validate(destination, expected)["manifest_sha256"] == result["manifest_sha256"]:
                return result
        except artifact.ArtifactError:
            pass
        stage = Path(tempfile.mkdtemp(prefix=".admin-portal-v2.stage-", dir=workspace))
        try:
            for name in (*result["inventory"], artifact.MANIFEST):
                target = stage / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source / name, target)
            artifact.validate(stage, expected)
            if destination.exists():
                rename(destination, backup)
            try:
                rename(stage, destination)
            except BaseException:
                if backup.exists() and not destination.exists():
                    rename(backup, destination)
                raise
            if backup.exists():
                shutil.rmtree(backup)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--expected-manifest-sha256", default=artifact.PINNED_MANIFEST_SHA256)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        if args.check:
            result = artifact.validate(DESTINATION, args.expected_manifest_sha256)
            if args.source:
                artifact.validate(args.source, result["manifest_sha256"])
        elif args.source:
            result = vendor(args.source, args.expected_manifest_sha256)
        else:
            parser.error("--source is required unless --check is used")
        print(f"Admin artifact {result['manifest_sha256']} ({len(result['inventory'])} files)")
    except (artifact.ArtifactError, OSError) as error:
        print(f"Admin artifact: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
