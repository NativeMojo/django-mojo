#!/usr/bin/env python3
"""Build one exact, private MojoLand django-mojo wheel.

The source tree is exported from Git into a temporary directory.  The public
version is changed only inside that archive, so running this tool never dirties
or versions the checkout used to invoke it.
"""

import argparse
import email.parser
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
import zipfile


SHA_RE = re.compile(r"^[0-9a-f]{40}$")
MIGRATION_RE = re.compile(r"(^|/)migrations/[^/]+\.py$")
PROJECT_VERSION_RE = re.compile(
    r'(?ms)^(\[project\]\s.*?^version\s*=\s*)"([^"]+)"'
)
PACKAGE_VERSION_RE = re.compile(r'(?m)^__version__\s*=\s*"([^"]+)"')


class BuildError(Exception):
    pass


def _run(argv, cwd, env=None, capture=True):
    try:
        result = subprocess.run(
            argv,
            cwd=str(cwd),
            env=env,
            text=True,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
        )
    except OSError as error:
        raise BuildError("cannot run {}: {}".format(argv[0], error))
    if result.returncode:
        detail = (result.stderr or result.stdout or "command failed").strip()
        raise BuildError("{}: {}".format(" ".join(argv), detail))
    return (result.stdout or "").strip()


def _exact_commit(repo, value, label):
    if not SHA_RE.fullmatch(value):
        raise BuildError("{} must be an exact 40-character lowercase Git SHA".format(label))
    resolved = _run(["git", "rev-parse", "--verify", "{}^{{commit}}".format(value)], repo)
    if resolved != value:
        raise BuildError("{} does not resolve to the exact requested commit".format(label))
    return resolved


def _validate_source(repo, base_sha, source_sha):
    base_sha = _exact_commit(repo, base_sha, "base SHA")
    source_sha = _exact_commit(repo, source_sha, "source SHA")
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", base_sha, source_sha],
        cwd=str(repo),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if result.returncode:
        raise BuildError("source SHA is not a descendant of the approved base SHA")
    changed = _run(
        ["git", "diff", "--name-only", "{}..{}".format(base_sha, source_sha)],
        repo,
    ).splitlines()
    migrations = sorted(path for path in changed if MIGRATION_RE.search(path))
    if migrations:
        raise BuildError(
            "private candidates may not contain migrations: {}".format(
                ", ".join(migrations)
            )
        )
    return changed


def _public_version(repo, source_sha):
    content = _run(["git", "show", "{}:pyproject.toml".format(source_sha)], repo)
    match = PROJECT_VERSION_RE.search(content)
    if not match:
        raise BuildError("could not read [project].version from the selected source")
    version = match.group(2)
    if "+" in version:
        version = version.split("+", 1)[0]
    return version


def _safe_extract(archive_path, destination):
    destination = destination.resolve()
    with tarfile.open(archive_path, "r:") as archive:
        for member in archive.getmembers():
            if member.issym() or member.islnk():
                raise BuildError("git archive contains links; refusing an ambiguous build input")
            target = (destination / member.name).resolve()
            if destination not in target.parents and target != destination:
                raise BuildError("git archive contained an unsafe path")
        archive.extractall(destination)


def _replace_once(path, pattern, replacement, label):
    content = path.read_text()
    content, count = pattern.subn(replacement, content, count=1)
    if count != 1:
        raise BuildError("could not patch {} in archived source".format(label))
    path.write_text(content)


def _inspect_wheel(wheel_path, expected_version):
    with zipfile.ZipFile(wheel_path) as wheel:
        metadata_names = [name for name in wheel.namelist() if name.endswith(".dist-info/METADATA")]
        if len(metadata_names) != 1:
            raise BuildError("wheel must contain exactly one dist-info/METADATA file")
        metadata = email.parser.Parser().parsestr(
            wheel.read(metadata_names[0]).decode("utf-8")
        )
        package_names = [name for name in wheel.namelist() if name == "mojo/__init__.py"]
        if package_names != ["mojo/__init__.py"]:
            raise BuildError("wheel does not contain mojo/__init__.py")
        package_init = wheel.read("mojo/__init__.py").decode("utf-8")
    if metadata.get("Name") != "django-mojo":
        raise BuildError("wheel distribution is not django-mojo")
    if metadata.get("Version") != expected_version:
        raise BuildError("wheel metadata version does not match the private version")
    match = PACKAGE_VERSION_RE.search(package_init)
    if not match or match.group(1) != expected_version:
        raise BuildError("wheel package version does not match its metadata")
    return metadata.get("Name"), metadata.get("Version")


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build(repo, source_sha, base_sha, output_dir, builder_identity=None):
    repo = Path(repo).resolve()
    output_dir = Path(output_dir).resolve()
    _validate_source(repo, base_sha, source_sha)
    public_version = _public_version(repo, source_sha)
    private_version = "{}+mojoland.g{}".format(public_version, source_sha)
    source_epoch = _run(["git", "show", "-s", "--format=%ct", source_sha], repo)

    with tempfile.TemporaryDirectory(prefix="django-mojo-lab-") as temp_name:
        temp = Path(temp_name)
        archive_path = temp / "source.tar"
        source_dir = temp / "source"
        wheel_dir = temp / "wheel"
        source_dir.mkdir()
        wheel_dir.mkdir()
        _run(
            ["git", "archive", "--format=tar", "--output", str(archive_path), source_sha],
            repo,
        )
        _safe_extract(archive_path, source_dir)
        _replace_once(
            source_dir / "pyproject.toml",
            PROJECT_VERSION_RE,
            lambda match: '{}"{}"'.format(match.group(1), private_version),
            "pyproject.toml version",
        )
        _replace_once(
            source_dir / "mojo" / "__init__.py",
            PACKAGE_VERSION_RE,
            '__version__ = "{}"'.format(private_version),
            "mojo package version",
        )
        env = dict(os.environ)
        env["SOURCE_DATE_EPOCH"] = source_epoch
        _run(
            ["uv", "build", "--wheel", "--out-dir", str(wheel_dir), str(source_dir)],
            repo,
            env=env,
            capture=False,
        )
        wheels = sorted(wheel_dir.glob("*.whl"))
        if len(wheels) != 1:
            raise BuildError("build produced {} wheels; expected exactly one".format(len(wheels)))
        distribution, inspected_version = _inspect_wheel(wheels[0], private_version)
        output_dir.mkdir(parents=True, exist_ok=True)
        wheel_path = output_dir / wheels[0].name
        shutil.copyfile(wheels[0], wheel_path)

    wheel_hash = _sha256(wheel_path)
    built_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    manifest = {
        "schema_version": 1,
        "distribution": distribution,
        "version": inspected_version,
        "public_version": public_version,
        "wheel": {
            "filename": wheel_path.name,
            "sha256": wheel_hash,
            "size": wheel_path.stat().st_size,
        },
        "source": {
            "base_commit": base_sha,
            "commit": source_sha,
            "migration_check": "passed",
        },
        "builder": {
            "identity": builder_identity or "{}@{}".format(getpass.getuser(), socket.gethostname()),
            "utc": built_at,
        },
    }
    manifest_path = output_dir / "{}.manifest.json".format(wheel_path.name)
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n")
    return wheel_path, manifest_path, manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--repo", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--builder-identity")
    args = parser.parse_args()
    try:
        wheel_path, manifest_path, manifest = build(
            args.repo,
            args.source_sha,
            args.base_sha,
            args.output_dir,
            builder_identity=args.builder_identity,
        )
    except BuildError as error:
        print("ERROR: {}".format(error), file=sys.stderr)
        return 1
    print(json.dumps({
        "manifest": str(manifest_path),
        "version": manifest["version"],
        "wheel": str(wheel_path),
        "wheel_sha256": manifest["wheel"]["sha256"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
