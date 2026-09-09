#!/usr/bin/env python3
"""Prove the Admin artifact survives wheel/sdist packaging, without Django."""

import argparse
import importlib.util
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile

REPO = Path(__file__).resolve().parents[1]
PREFIX = "mojo/apps/account/admin_portal_v2/"
_spec = importlib.util.spec_from_file_location("admin_artifact", REPO / "mojo/apps/account/services/admin_artifact.py")
artifact = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(artifact)


def inspect_archive(path, expected, extract_to):
    """Validate every member before extracting any regular files."""
    path = Path(path)
    seen = set()
    records = []
    wheel = path.suffix == ".whl"
    archive = zipfile.ZipFile(path) if wheel else tarfile.open(path, "r:gz")
    with archive:
        entries = archive.infolist() if wheel else archive.getmembers()
        for entry in entries:
            name = entry.filename if wheel else entry.name
            canonical = name[:-1] if name.endswith("/") else name
            artifact.safe_path(canonical)
            if canonical in seen:
                raise artifact.ArtifactError(f"duplicate archive member: {name}")
            seen.add(canonical)
            if ".admin-portal-v2" in canonical:
                raise artifact.ArtifactError(f"transient vendor tree in archive: {name}")
            is_dir = entry.is_dir() if wheel else entry.isdir()
            mode = entry.external_attr >> 16 if wheel else entry.mode
            if wheel:
                kind = stat.S_IFMT(mode)
                regular = kind in (0, stat.S_IFREG)
                if is_dir and kind not in (0, stat.S_IFDIR):
                    raise artifact.ArtifactError(f"nonregular archive directory: {name}")
            else:
                regular = entry.isfile()
            if not is_dir and not regular:
                raise artifact.ArtifactError(f"nonregular archive member: {name}")
            if is_dir:
                continue
            data = archive.read(entry) if wheel else archive.extractfile(entry).read()
            records.append((canonical, data))
    roots = {PurePosixPath(name).parts[0] for name, _ in records}
    if not wheel and len(roots) != 1:
        raise artifact.ArtifactError("sdist must have exactly one root")
    root_name = next(iter(roots)) if not wheel else ""
    for name, data in records:
        target = Path(extract_to) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    package_root = Path(extract_to) / root_name
    result = artifact.validate(package_root / PREFIX, expected)
    return result, package_root


def run(argv, cwd):
    result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=300,
                            env={k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")})
    if result.returncode:
        raise artifact.ArtifactError(f"package smoke failed: {' '.join(map(str, argv))}\n{result.stderr[-3000:]}")


def verify(dist, expected, build_smoke=False):
    tree = artifact.validate(REPO / PREFIX, expected)
    wheels = sorted(Path(dist).glob("django_mojo-*.whl"))
    sources = sorted(Path(dist).glob("django_mojo-*.tar.gz"))
    if len(wheels) != 1 or len(sources) != 1:
        raise artifact.ArtifactError("dist must contain exactly one django-mojo wheel and sdist")
    with tempfile.TemporaryDirectory(prefix="mojo-admin-package-") as directory:
        # macOS may return /var, an alias of /private/var. Only this freshly
        # created, verifier-owned root is canonicalized; artifact/source paths
        # continue through the validator's strict symlink-ancestry checks.
        root = Path(directory).resolve()
        for name, path in (("wheel", wheels[0]), ("sdist", sources[0])):
            result, package_root = inspect_archive(path, expected, root / name)
            if result["inventory"] != tree["inventory"]:
                raise artifact.ArtifactError(f"{name} inventory differs from committed tree")
            if name == "sdist":
                sdist_root = package_root
        if build_smoke:
            output = root / "rebuilt"
            run(["uv", "build", "--wheel", "--out-dir", str(output)], sdist_root)
            rebuilt = list(output.glob("*.whl"))
            if len(rebuilt) != 1:
                raise artifact.ArtifactError("sdist did not build exactly one wheel")
            inspect_archive(rebuilt[0], expected, root / "rebuilt-proof")
            environment = root / "venv"
            run(["uv", "venv", str(environment)], root)
            python = environment / "bin/python"
            run(["uv", "pip", "install", "--python", str(python), "--no-deps", str(rebuilt[0])], root)
            code = ("import importlib.util,pathlib,sysconfig;"
                    "p=pathlib.Path(sysconfig.get_path('purelib'))/'mojo/apps/account';"
                    "s=importlib.util.spec_from_file_location('artifact',p/'services/admin_artifact.py');"
                    "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
                    f"m.validate(p/'admin_portal_v2',{expected!r})")
            run([str(python), "-I", "-c", code], root)
    return tree


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, default=REPO / "dist")
    parser.add_argument("--expected-manifest-sha256", default=artifact.PINNED_MANIFEST_SHA256)
    parser.add_argument("--build-smoke", action="store_true")
    args = parser.parse_args()
    try:
        result = verify(args.dist, args.expected_manifest_sha256, args.build_smoke)
        print(f"Tree, wheel and sdist: {result['manifest_sha256']} ({len(result['inventory'])} files)")
    except (artifact.ArtifactError, OSError, ValueError, tarfile.TarError, zipfile.BadZipFile) as error:
        print(f"Admin package proof: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
