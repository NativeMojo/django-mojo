"""Exact private-asset manifest for the built-in Admin portal."""

import json
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1] / "admin_portal"
FEATURES = (
    "dashboard", "people", "webapps", "activity", "platform", "advanced",
    "settings", "sms", "email")


def _read_json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        raise RuntimeError(f"invalid Admin asset manifest: {path.name}") from err
    if not isinstance(value, dict):
        raise RuntimeError(f"invalid Admin asset manifest envelope: {path.name}")
    return value


def _safe_relative(value):
    if not isinstance(value, str) or not value or "\\" in value:
        raise RuntimeError("Admin asset manifest contains an invalid path")
    if any(part in ("", ".", "..") for part in value.split("/")):
        raise RuntimeError("Admin asset manifest contains a non-canonical path")
    path = PurePosixPath(value)
    if path.is_absolute():
        raise RuntimeError("Admin asset manifest contains an unsafe path")
    return path.as_posix()


def load_manifest(root=ROOT):
    """Load, validate, and prove every exactly deliverable package asset."""
    root = Path(root)
    base = _read_json(root / "manifest.json")
    if base.get("version") != 1 or base.get("owner") != "foundation":
        raise RuntimeError("Admin base manifest has an unsupported contract")
    assets = set()

    def add(relative):
        relative = _safe_relative(relative)
        if relative in assets:
            raise RuntimeError(f"Admin asset is declared twice: {relative}")
        path = root / relative
        if not path.is_file():
            raise RuntimeError(f"Admin package is missing declared asset: {relative}")
        assets.add(relative)

    entries = base.get("assets")
    if not isinstance(entries, list):
        raise RuntimeError("Admin base manifest assets must be a list")
    for relative in entries:
        add(relative)

    for feature in FEATURES:
        feature_root = root / "assets" / "features" / feature
        manifest = _read_json(feature_root / "manifest.json")
        if manifest.get("version") != 1 or manifest.get("owner") != feature:
            raise RuntimeError(f"Admin feature manifest ownership mismatch: {feature}")
        entries = manifest.get("assets")
        if not isinstance(entries, list):
            raise RuntimeError(f"Admin feature assets must be a list: {feature}")
        for local in entries:
            local = _safe_relative(local)
            if "/" in local:
                raise RuntimeError(
                    f"Admin feature asset escaped its owner directory: {feature}")
            add(f"assets/features/{feature}/{local}")
    return frozenset(assets)


PRIVATE_ASSETS = load_manifest()


def asset_path(asset):
    """Return an allowed real file path, or None for unknown/traversal input."""
    try:
        relative = _safe_relative(asset)
    except RuntimeError:
        return None
    if relative not in PRIVATE_ASSETS:
        return None
    path = ROOT / relative
    try:
        path.resolve().relative_to(ROOT.resolve())
    except (OSError, ValueError):
        return None
    return path if path.is_file() else None
