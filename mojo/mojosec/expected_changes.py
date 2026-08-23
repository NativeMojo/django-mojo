"""Strict, root-owned deployment annotations for targeted FIM events."""

import datetime
import hashlib
import json
import os
import re
import stat


MAX_BYTES = 20 * 1024 * 1024
MAX_ENTRIES = 65536
_DIGEST_RE = re.compile(r"^[a-f0-9]{64}$")
_DEPLOYMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_OPERATION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_EVIDENCE_KINDS = {"file", "symlink", "directory", "other"}
MAX_OPERATION_CORRELATION_SECONDS = 300
# The widest post-completion window any tier may configure
# (collectors.fim.tiers.*.correlation_seconds, validated in config.py). It is
# the ONE ceiling: annotation() clamps to it, and store.py's
# ANNOTATION_MAX_CORRELATION_SECONDS is this value, so the window an event is
# HELD for and the window it is MATCHED in can never be bounded differently.
MAX_TIER_CORRELATION_SECONDS = 1800
# Domain separator for the relaxed content-directory digest below. Its presence
# in the digest material makes a relaxed digest unable to collide with a full
# one, so relaxation can never be smuggled onto an ordinary host annotation.
_RELAXED_DIRECTORY_SCOPE = "directory_metadata_relaxed"


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


def relaxed_directory_digest(value):
    """Digest one content-root directory by ownership and mode alone.

    Publishing into a tenant tree moves the containing directory's mtime, size
    and (on a rename-based publish) its inode, none of which the producer can
    predict when it snapshots `after`. Ownership and mode are exactly the
    properties a publish must NOT change, so they are what stays in the digest.
    This narrows nothing else: every file created, modified or deleted inside
    the directory is still its own FIM change needing its own annotation.
    """
    if not isinstance(value, dict) or value.get("kind") != "directory":
        return None, None
    material = {field: value.get(field) for field in ("kind", "mode", "uid", "gid")}
    material["scope"] = _RELAXED_DIRECTORY_SCOPE
    payload = json.dumps(
        material, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "directory", hashlib.sha256(payload.encode("utf-8")).hexdigest()


def contained(path, root):
    try:
        return os.path.commonpath((path, root)) == root
    except ValueError:
        return False


def evidence_digest(value):
    """Return the type and digest used by both FIM and trusted producers."""
    if not isinstance(value, dict) or value.get("kind") not in _EVIDENCE_KINDS:
        return None, None
    kind = value["kind"]
    fields = (
        "kind", "mode", "uid", "gid", "size", "mtime_ns", "ctime_ns",
        "device", "inode",
    )
    material = {field: value.get(field) for field in fields}
    if kind == "file":
        digest = value.get("sha256")
        if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
            return None, None
        material["content_sha256"] = digest
    elif kind == "symlink":
        digest = value.get("target_sha256")
        if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
            return None, None
        material["target_sha256"] = digest
    payload = json.dumps(
        material, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return kind, hashlib.sha256(payload.encode("utf-8")).hexdigest()


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
            "operation_id", "operation_kind", "started_at", "completed_at",
            "evidence_kind",
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
            if entry["evidence_kind"] not in _EVIDENCE_KINDS:
                raise ExpectedChangeError("expected-change evidence_kind is invalid")
            if (not isinstance(entry["operation_id"], str) or
                    not _OPERATION_RE.fullmatch(entry["operation_id"])):
                raise ExpectedChangeError("expected-change operation_id is invalid")
            if (not isinstance(entry["operation_kind"], str) or
                    not _OPERATION_RE.fullmatch(entry["operation_kind"])):
                raise ExpectedChangeError("expected-change operation_kind is invalid")
            started = _timestamp(entry["started_at"])
            completed = _timestamp(entry["completed_at"])
            if started > completed:
                raise ExpectedChangeError("expected-change operation completed before it started")
            if completed > expiry:
                raise ExpectedChangeError("expected-change operation completed after expiry")
        key = (entry["path"], entry["change"], entry["sha256"])
        if key in seen:
            raise ExpectedChangeError("expected-change entries must be unique")
        seen.add(key)
        internal = {"_expiry": expiry}
        if set(entry) == v2_fields:
            internal["_started"] = started
            internal["_completed"] = completed
        result.append({**entry, **internal})
    return result


def annotation(entries, path, change, before, after, now=None, observed_at=None,
               correlation_seconds=None, directory_metadata_roots=()):
    """Return a bounded annotation for an exact live entry; never suppress.

    correlation_seconds is the tier's post-completion match window; None — and
    anything outside 0 < value <= MAX_TIER_CORRELATION_SECONDS, which is what a
    durable event attribute can carry — keeps the shared deploy window. That is
    the same test store._event_grace applies before deciding how long to hold
    the evidence, so no attribute can widen one bound without the other.
    directory_metadata_roots names the content roots
    inside which a DIRECTORY annotation matches on ownership and mode alone
    (see relaxed_directory_digest) — outside them nothing is relaxed.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if isinstance(now, (int, float)):
        now = datetime.datetime.fromtimestamp(now, datetime.timezone.utc)
    if observed_at is None:
        observed_at = now
    elif isinstance(observed_at, (int, float)):
        observed_at = datetime.datetime.fromtimestamp(observed_at, datetime.timezone.utc)
    elif isinstance(observed_at, str):
        observed_at = _timestamp(observed_at)
    evidence = before if change == "deleted" else after
    correlation = correlation_seconds
    if (not isinstance(correlation, int) or isinstance(correlation, bool) or
            not 0 < correlation <= MAX_TIER_CORRELATION_SECONDS):
        correlation = MAX_OPERATION_CORRELATION_SECONDS
    relaxable = any(contained(path, root) for root in directory_metadata_roots or ())
    for entry in entries:
        if entry["path"] != path or entry["change"] != change or entry["_expiry"] < now:
            continue
        if "evidence_kind" in entry:
            if relaxable and entry["evidence_kind"] == "directory":
                kind, digest = relaxed_directory_digest(evidence)
            else:
                kind, digest = evidence_digest(evidence)
            if kind != entry["evidence_kind"] or digest != entry["sha256"]:
                continue
            if (observed_at < entry["_started"] or
                    observed_at > entry["_completed"] + datetime.timedelta(
                        seconds=correlation)):
                continue
        else:
            digest = (evidence or {}).get("sha256")
            if not digest or entry["sha256"] != digest:
                continue
        if entry["path"] == path:
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
