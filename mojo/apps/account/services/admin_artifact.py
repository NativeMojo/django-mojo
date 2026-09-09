"""Dependency-free validation of portal-mojo's frozen Admin artifact schema 1.

Load this file directly with importlib when repairing an installation: importing
account or admin_assets is deliberately unnecessary.
"""

import hashlib
import json
import re
import stat
from pathlib import Path
from types import MappingProxyType


MANIFEST = "admin-artifact.json"
PINNED_MANIFEST_SHA256 = "934e89e2ce913583463469d7eda4c4ef3c5015ce9f3d51fb9894a75ce61b2031"
DOCUMENT_CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self'; "
                "script-src 'self'; connect-src 'self'; base-uri 'none'; "
                "form-action 'self'; frame-ancestors 'none'")
_FIELDS = frozenset(("schema_version", "artifact", "version", "source_revision",
    "source_dirty", "toolchain", "lockfile_sha256", "entrypoint", "asset_base",
    "api_mode", "source_session_contract", "vite_manifest", "files"))


class ArtifactError(RuntimeError):
    """The artifact is not a complete, canonical producer output."""


def safe_path(value):
    if (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.@/-]+", value)
            or any(p in ("", ".", "..") for p in value.split("/"))):
        raise ArtifactError(f"noncanonical artifact path: {value!r}")
    return value


def no_symlink_ancestry(path):
    path = Path(path).absolute()
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ArtifactError(f"symlinked artifact ancestry: {part}")
    return path


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ArtifactError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    return value


def validate(root, expected_manifest_sha256=None):
    """Return immutable metadata, exact manifest digest and HTTP allowlist."""
    root = no_symlink_ancestry(root)
    try:
        actual = set()
        for path in root.rglob("*"):
            mode = path.lstat().st_mode
            if stat.S_ISDIR(mode):
                continue
            if not stat.S_ISREG(mode):
                raise ArtifactError(f"nonregular artifact entry: {path}")
            actual.add(safe_path(path.relative_to(root).as_posix()))
        raw = (root / MANIFEST).read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if expected_manifest_sha256 is not None and digest != expected_manifest_sha256:
            raise ArtifactError(f"manifest digest mismatch: expected {expected_manifest_sha256}, got {digest}")
        data = json.loads(raw, object_pairs_hook=_pairs)
        if not isinstance(data, dict) or set(data) != _FIELDS:
            raise ArtifactError("unsupported artifact manifest fields")
        for key, expected in {"schema_version": 1, "artifact": "portal-mojo-admin",
                "source_dirty": False, "entrypoint": "index.html", "asset_base": "./",
                "api_mode": "same-origin", "source_session_contract": 1,
                "vite_manifest": ".vite/manifest.json",
                "toolchain": {"node": "24.21.0", "npm": "11.19.0"}}.items():
            if type(data[key]) is not type(expected) or data[key] != expected:
                raise ArtifactError(f"unsupported artifact {key}")
        for key, pattern in (("version", r"[0-9]+\.[0-9]+\.[0-9]+"),
                ("source_revision", r"[a-f0-9]{40}"), ("lockfile_sha256", r"[a-f0-9]{64}")):
            if not isinstance(data[key], str) or not re.fullmatch(pattern, data[key]):
                raise ArtifactError(f"invalid artifact {key}")
        if not isinstance(data["files"], list) or not data["files"]:
            raise ArtifactError("empty artifact inventory")
        declared = []
        for entry in data["files"]:
            if not isinstance(entry, dict) or set(entry) != {"path", "size", "sha256"}:
                raise ArtifactError("invalid inventory fields")
            name = safe_path(entry["path"])
            if (name == MANIFEST or name.endswith(".map") or "mock" in name.lower()
                    or type(entry["size"]) is not int or entry["size"] < 0
                    or not isinstance(entry["sha256"], str)
                    or not re.fullmatch(r"[a-f0-9]{64}", entry["sha256"])):
                raise ArtifactError(f"invalid inventory entry: {name}")
            declared.append(name)
            content = (root / name).read_bytes()
            if len(content) != entry["size"] or hashlib.sha256(content).hexdigest() != entry["sha256"]:
                raise ArtifactError(f"artifact bytes mismatch: {name}")
        if declared != sorted(set(declared)):
            raise ArtifactError("inventory must be sorted and unique")
        if actual != set(declared) | {MANIFEST}:
            raise ArtifactError(f"artifact inventory mismatch: {sorted(actual ^ (set(declared) | {MANIFEST}))}")
        if not {data["entrypoint"], data["vite_manifest"]}.issubset(declared):
            raise ArtifactError("entrypoint or Vite manifest missing")
        return MappingProxyType({"metadata": _freeze(data), "manifest_sha256": digest,
            "allowlist": frozenset(set(declared) - {data["vite_manifest"]}),
            "inventory": tuple(declared)})
    except (OSError, ValueError, TypeError) as error:
        raise ArtifactError(f"invalid Admin artifact at {root}: {error}") from error
