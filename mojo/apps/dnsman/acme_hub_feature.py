"""Private loaded identity; never part of the public capabilities DTO."""
import hashlib
import json
import re
from pathlib import Path

from packaging.version import Version, InvalidVersion

EPOCH = 1
LEGACY_RELEASE_CEILING = "1.37.4"
ROOT = Path(__file__).resolve().parents[2]
BUILD_FILE = ROOT / "apps/dnsman/_acme_hub_build.json"
CRITICAL_FILES = (
    "apps/dnsman/acme_hub_feature.py", "apps/dnsman/models/acme_hub_installation.py",
    "apps/dnsman/services/acme_hub.py", "apps/dnsman/services/acme_hub_upgrade.py",
    "apps/dnsman/services/acme_hub_legacy.py", "apps/edge/services/acme_hub_deploy.py",
    "apps/edge/asyncjobs.py", "apps/edge/services/deploy.py",
    "deploy/acme_hub_floor.py", "deploy/acme_hub_entry.py", "deploy/project_scripts/update.sh",
    "deploy/project_scripts/post_deploy.sh",
    "apps/dnsman/migrations/0005_acmehubparticipant_and_more.py",
    "apps/dnsman/migrations/0007_acmehubinstallation.py",
    "apps/dnsman/management/commands/acme_hub_upgrade.py",
    "apps/aws/services/capacity.py",
    "apps/dnsman/services/acme_hub_client.py",
    "apps/dnsman/services/delegation_setup.py",
)
HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")


def release(value):
    if (not isinstance(value, str) or len(value) > 64
            or not re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){1,3}", value)):
        raise ValueError("artifact_unavailable")
    try:
        parsed = Version(value)
    except InvalidVersion:
        raise ValueError("artifact_unavailable") from None
    if (str(parsed) != value or parsed.is_prerelease or parsed.is_devrelease
            or parsed.local is not None):
        raise ValueError("artifact_unavailable")
    return parsed


def manifest(root=ROOT):
    digest = hashlib.sha256()
    for name in CRITICAL_FILES:
        digest.update(name.encode("ascii") + b"\0")
        digest.update(hashlib.sha256((root / name).read_bytes()).digest())
    return digest.hexdigest()


def installed_identity():
    try:
        raw = BUILD_FILE.read_bytes()
        if len(raw) > 2048:
            raise ValueError("artifact_unavailable")
        value = json.loads(raw)
        if (set(value) != {"epoch", "source_sha", "manifest_sha256", "release"}
                or type(value["epoch"]) is not int or value["epoch"] != EPOCH
                or not HEX40.fullmatch(value["source_sha"])
                or not HEX64.fullmatch(value["manifest_sha256"])
                or value["manifest_sha256"] != manifest()):
            raise ValueError("artifact_unavailable")
        release(value["release"])
        return value
    except (OSError, ValueError, TypeError, KeyError):
        return None


# Captured once when these bytes load. Re-reading installed files is not proof
# that a process whose old modules remain in memory loaded the new artifact.
LOADED_IDENTITY = installed_identity()


def loaded_identity():
    return dict(LOADED_IDENTITY) if LOADED_IDENTITY else None


def validate_artifact(value):
    required = {"epoch", "source_sha", "manifest_sha256", "release", "wheel_sha256"}
    if (not isinstance(value, dict) or set(value) != required
            or type(value["epoch"]) is not int or value["epoch"] != EPOCH
            or not isinstance(value["source_sha"], str) or not HEX40.fullmatch(value["source_sha"])
            or any(not isinstance(value[key], str) or not HEX64.fullmatch(value[key])
                   for key in ("manifest_sha256", "wheel_sha256"))):
        raise ValueError("artifact_unavailable")
    if release(value["release"]) <= release(LEGACY_RELEASE_CEILING):
        raise ValueError("artifact_unavailable")
    return dict(value)
