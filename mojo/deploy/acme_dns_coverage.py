"""Read-only ProducerCoverageV1 contract. Enrollment belongs to the operator.

No Django/settings import or writer exists here. A source install, new journal,
old inventory observation, UI acknowledgement, or missing receipt is not coverage.
"""
import hashlib
import json
import os
import re
import stat
import uuid
from pathlib import Path

PATH = Path("/etc/mojo/acme-dns-producer-coverage.json")
MAX_BYTES = 65536
FIELDS = {"schema_version", "producer_epoch", "installation_uuid", "enrollment_uuid",
          "source_sha", "critical_manifest_sha256", "profile_sha256", "roster_sha256",
          "ledger_cutover_sequence", "prior_outcomes_sha256", "scopes"}
HEX40 = re.compile(r"[0-9a-f]{40}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
CLASSES = {"inline_acme_txt", "hub_target_txt"}


class CoverageError(ValueError):
    pass


def validate(value, identity):
    try:
        if not isinstance(value, dict) or set(value) != FIELDS:
            raise ValueError()
        if type(value["schema_version"]) is not int or value["schema_version"] != 1 or type(value["producer_epoch"]) is not int or value["producer_epoch"] != 1:
            raise ValueError()
        for key in ("installation_uuid", "enrollment_uuid"):
            if str(uuid.UUID(value[key])) != value[key]:
                raise ValueError()
        if not HEX40.fullmatch(value["source_sha"]):
            raise ValueError()
        for key in ("critical_manifest_sha256", "profile_sha256", "roster_sha256", "prior_outcomes_sha256"):
            if not HEX64.fullmatch(value[key]):
                raise ValueError()
        if (value["source_sha"] != identity["source_sha"]
                or value["critical_manifest_sha256"] != identity["manifest_sha256"]
                or type(value["ledger_cutover_sequence"]) is not int or value["ledger_cutover_sequence"] < 0
                or not isinstance(value["scopes"], list) or not 1 <= len(value["scopes"]) <= 128):
            raise ValueError()
        seen = set()
        for scope in value["scopes"]:
            if (not isinstance(scope, dict) or set(scope) != {"provider", "account_id", "zone_id", "classes"}
                    or scope["provider"] != "route53" or not re.fullmatch(r"[0-9]{12}", scope["account_id"])
                    or not re.fullmatch(r"Z[A-Z0-9]{1,63}", scope["zone_id"])
                    or not isinstance(scope["classes"], list) or not scope["classes"]
                    or len(scope["classes"]) != len(set(scope["classes"])) or not set(scope["classes"]) <= CLASSES):
                raise ValueError()
            key = scope["account_id"], scope["zone_id"]
            if key in seen:
                raise ValueError()
            seen.add(key)
        return value
    except (ValueError, TypeError, KeyError, AttributeError):
        raise CoverageError("producer_coverage_unavailable") from None


def _read(path, uid, identity):
    """Private disposable-file seam; public load always uses the fixed path."""
    path = Path(path)
    flags = os.O_RDONLY | os.O_NOFOLLOW
    directory = os.open("/", flags | os.O_DIRECTORY)
    try:
        for component in path.parts[1:-1]:
            child = os.open(component, flags | os.O_DIRECTORY, dir_fd=directory)
            os.close(directory)
            directory = child
            info = os.fstat(directory)
            if info.st_uid != uid or info.st_mode & 0o022:
                raise CoverageError("producer_coverage_unavailable")
        fd = os.open(path.name, flags, dir_fd=directory)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_nlink != 1
                    or stat.S_IMODE(info.st_mode) != 0o644 or not 0 < info.st_size <= MAX_BYTES):
                raise CoverageError("producer_coverage_unavailable")
            raw = os.read(fd, MAX_BYTES + 1)
            after = os.fstat(fd)
            if len(raw) != info.st_size or len(raw) > MAX_BYTES or (after.st_size, after.st_mtime_ns, after.st_ino) != (info.st_size, info.st_mtime_ns, info.st_ino):
                raise CoverageError("producer_coverage_unavailable")
        finally:
            os.close(fd)
    finally:
        os.close(directory)
    value = validate(json.loads(raw), identity)
    return value, hashlib.sha256(raw).hexdigest()


def load(identity):
    try:
        return _read(PATH, 0, identity)
    except (OSError, ValueError, TypeError, KeyError):
        raise CoverageError("producer_coverage_unavailable") from None


def qualifies(receipt, scope, origin):
    if not origin or origin["kind"] != "origin" or origin["request"].get("legacy_history") is not False:
        return "legacy_history_unknown"
    if origin["id"] <= receipt["ledger_cutover_sequence"]:
        return "legacy_history_unknown"
    for approved in receipt["scopes"]:
        if (approved["provider"] == scope.get("provider")
                and approved["account_id"] == scope.get("account_id")
                and approved["zone_id"] == scope.get("zone_id")
                and scope.get("scope_class") in approved["classes"]):
            return None
    return "producer_coverage_unavailable"
