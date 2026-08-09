"""Strict, root-owned deployment annotations for targeted FIM events."""

import datetime
import json
import os
import re
import stat


MAX_BYTES = 256 * 1024
MAX_ENTRIES = 4096
_DIGEST_RE = re.compile(r"^[a-f0-9]{64}$")
_DEPLOYMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_OPERATION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class ExpectedChangeError(ValueError):
    pass


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ExpectedChangeError(f"expected-change manifest repeats JSON field: {key}")
        result[key] = value
    return result


def _timestamp(value):
    if not isinstance(value, str) or len(value) > 40:
        raise ExpectedChangeError("expected-change expiry must be an ISO-8601 string")
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as err:
        raise ExpectedChangeError("expected-change expiry is invalid") from err
    if parsed.tzinfo is None:
        raise ExpectedChangeError("expected-change expiry must include a timezone")
    return parsed.astimezone(datetime.timezone.utc)


def load_manifest(path, require_root=None):
    """Read one no-follow descriptor; an absent manifest means no annotations."""
    if not path:
        return []
    if require_root is None:
        require_root = os.geteuid() == 0
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return []
    except OSError as err:
        raise ExpectedChangeError(f"cannot open expected-change manifest: {err}") from err
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ExpectedChangeError("expected-change manifest must be a regular file")
        if info.st_mode & 0o077:
            raise ExpectedChangeError("expected-change manifest must be mode 0600 or stricter")
        if require_root and info.st_uid != 0:
            raise ExpectedChangeError("expected-change manifest must be owned by root")
        payload = os.read(descriptor, MAX_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(payload) > MAX_BYTES:
        raise ExpectedChangeError("expected-change manifest is too large")
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_strict_object)
    except (UnicodeError, json.JSONDecodeError) as err:
        raise ExpectedChangeError(f"expected-change manifest is invalid JSON: {err}") from err
    if not isinstance(value, dict) or set(value) != {"schema", "version", "entries"}:
        raise ExpectedChangeError("expected-change manifest has an invalid envelope")
    if (value["schema"] != "mojosec.expected_changes" or
            value["version"] not in (1, 2)):
        raise ExpectedChangeError("expected-change manifest schema/version is unsupported")
    if not isinstance(value["entries"], list) or len(value["entries"]) > MAX_ENTRIES:
        raise ExpectedChangeError("expected-change entries must be a bounded list")
    result = []
    seen = set()
    for entry in value["entries"]:
        v1_fields = {"path", "change", "sha256", "expires_at", "deployment_id"}
        v2_fields = v1_fields | {
            "operation_id", "operation_kind", "completed_at",
        }
        allowed_fields = (v1_fields,) if value["version"] == 1 else (v1_fields, v2_fields)
        if not isinstance(entry, dict) or set(entry) not in allowed_fields:
            raise ExpectedChangeError("expected-change entry fields are invalid")
        if not isinstance(entry["path"], str) or not os.path.isabs(entry["path"]):
            raise ExpectedChangeError("expected-change path must be absolute")
        if entry["change"] not in ("created", "modified", "deleted"):
            raise ExpectedChangeError("expected-change type is invalid")
        if not isinstance(entry["sha256"], str) or not _DIGEST_RE.fullmatch(entry["sha256"]):
            raise ExpectedChangeError("expected-change sha256 is invalid")
        if (not isinstance(entry["deployment_id"], str) or
                not _DEPLOYMENT_RE.fullmatch(entry["deployment_id"])):
            raise ExpectedChangeError("expected-change deployment_id is invalid")
        expiry = _timestamp(entry["expires_at"])
        if set(entry) == v2_fields:
            if (not isinstance(entry["operation_id"], str) or
                    not _OPERATION_RE.fullmatch(entry["operation_id"])):
                raise ExpectedChangeError("expected-change operation_id is invalid")
            if (not isinstance(entry["operation_kind"], str) or
                    not _OPERATION_RE.fullmatch(entry["operation_kind"])):
                raise ExpectedChangeError("expected-change operation_kind is invalid")
            completed = _timestamp(entry["completed_at"])
            if completed > expiry:
                raise ExpectedChangeError("expected-change operation completed after expiry")
        key = (entry["path"], entry["change"], entry["sha256"])
        if key in seen:
            raise ExpectedChangeError("expected-change entries must be unique")
        seen.add(key)
        result.append({**entry, "_expiry": expiry})
    return result


def annotation(entries, path, change, before, after, now=None):
    """Return a bounded annotation for an exact live entry; never suppress."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    evidence = before if change == "deleted" else after
    digest = (evidence or {}).get("sha256")
    if not digest:
        return None
    for entry in entries:
        if (entry["path"] == path and entry["change"] == change and
                entry["sha256"] == digest and entry["_expiry"] >= now):
            result = {"deployment_id": entry["deployment_id"],
                      "expires_at": entry["expires_at"]}
            if "operation_id" in entry:
                result.update({
                    "operation_id": entry["operation_id"],
                    "operation_kind": entry["operation_kind"],
                    "completed_at": entry["completed_at"],
                })
            return result
    return None
