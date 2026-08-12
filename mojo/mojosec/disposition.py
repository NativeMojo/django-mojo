"""Server-owned disposition for one exact, trusted local PAM lifecycle."""

import datetime
import ipaddress
import json
import os
import re
import stat


LOCAL_ONLY_DIAGNOSTIC_PATH = "/etc/mojosec/local_only_diagnostic.json"
LOCAL_ONLY_DIAGNOSTIC_SCHEMA = "mojosec.local_only_diagnostic"
LOCAL_ONLY_DIAGNOSTIC_VERSION = 1
MAX_DIAGNOSTIC_BYTES = 512
MAX_DIAGNOSTIC_SECONDS = 60 * 60
MAX_MTIME_FUTURE_SECONDS = 5

_OBSERVATION_FIELDS = {
    "kind", "severity", "summary", "attributes", "fingerprint",
    "aggregate", "recommendation", "observed_at",
}
_EVENT_FIELDS = {
    "id", "kind", "observed_at", "first_seen", "last_seen", "severity",
    "summary", "count", "attributes", "recommendation",
}
_ATTRIBUTE_FIELDS = {
    "attribution_provenance", "service", "target_user", "target_uid",
    "opener_uid", "producer_uid", "producer_pid", "producer_comm",
    "producer_exe", "systemd_unit", "boot_id", "audit_session",
    "audit_loginuid",
}
_OPTIONAL_ATTRIBUTE_FIELDS = {"source_ip", "tty"}
_USER = re.compile(r"^[A-Za-z0-9_.@-]{1,128}$")
_BOOT_ID = re.compile(r"^[a-f0-9]{32}$")
_TTY = re.compile(r"^[A-Za-z0-9_.\-/]{1,96}$")
_DIGEST = re.compile(r"^[a-f0-9]{64}$")


def _timestamp(value):
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _canonical_uid(value):
    return bool(
        isinstance(value, int) and not isinstance(value, bool) and
        0 <= value < 4294967295
    )


def _canonical_ip(value):
    if not isinstance(value, str):
        return False
    try:
        return str(ipaddress.ip_address(value)) == value
    except ValueError:
        return False


def is_local_only(value, wire=None):
    """Return True only for the complete canonical lifecycle tuple."""
    if not isinstance(value, dict):
        return False
    if wire is None:
        wire = "count" in value
    expected = _EVENT_FIELDS if wire else _OBSERVATION_FIELDS
    if set(value) != expected:
        return False
    if (value.get("kind") != "auth.session_open" or
            value.get("severity") != "info" or
            value.get("summary") != "PAM service session opened" or
            value.get("recommendation") != "none"):
        return False
    identity = value.get("id") if wire else value.get("fingerprint")
    if not isinstance(identity, str) or not _DIGEST.fullmatch(identity):
        return False
    observed = _timestamp(value.get("observed_at"))
    if observed is None:
        return False
    if wire:
        if (not isinstance(value.get("count"), int) or
                isinstance(value.get("count"), bool) or value.get("count") != 1 or
                _timestamp(value.get("first_seen")) != observed or
                _timestamp(value.get("last_seen")) != observed):
            return False
    elif value.get("aggregate") is not False:
        return False
    attributes = value.get("attributes")
    if (not isinstance(attributes, dict) or
            not _ATTRIBUTE_FIELDS.issubset(attributes) or
            set(attributes) - _ATTRIBUTE_FIELDS - _OPTIONAL_ATTRIBUTE_FIELDS):
        return False
    target_uid = attributes.get("target_uid")
    target_user = attributes.get("target_user")
    opener_uid = attributes.get("opener_uid")
    producer_uid = attributes.get("producer_uid")
    audit_loginuid = attributes.get("audit_loginuid")
    if (not _canonical_uid(target_uid) or not isinstance(target_user, str) or
            not _USER.fullmatch(target_user) or
            attributes.get("service") != "systemd-user" or
            not _canonical_uid(opener_uid) or opener_uid != 0 or
            not _canonical_uid(producer_uid) or producer_uid != 0 or
            not _canonical_uid(attributes.get("producer_pid")) or
            attributes.get("producer_pid") == 0 or
            attributes.get("producer_comm") != "(systemd)" or
            attributes.get("producer_exe") != "/usr/lib/systemd/systemd" or
            attributes.get("systemd_unit") != f"user@{target_uid}.service" or
            not _canonical_uid(audit_loginuid) or audit_loginuid != target_uid or
            not isinstance(attributes.get("boot_id"), str) or
            not _BOOT_ID.fullmatch(attributes["boot_id"]) or
            not _canonical_uid(attributes.get("audit_session"))):
        return False
    if "tty" in attributes:
        tty = attributes["tty"]
        if not isinstance(tty, str) or not _TTY.fullmatch(tty) or ".." in tty:
            return False
    provenance = attributes.get("attribution_provenance")
    source_ip = attributes.get("source_ip")
    if provenance == "none":
        return "source_ip" not in attributes
    if provenance == "audit_session":
        return "source_ip" in attributes and _canonical_ip(source_ip)
    return False


def observed_timestamp(value):
    if not is_local_only(value):
        return None
    return _timestamp(value["observed_at"]).astimezone(
        datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _strict_object(pairs):
    value = {}
    for key, child in pairs:
        if key in value:
            raise ValueError("duplicate field")
        value[key] = child
    return value


def diagnostic_override(path=LOCAL_ONLY_DIAGNOSTIC_PATH, now=None):
    """Read the bounded root-owned diagnostic sidecar from one descriptor."""
    result = {"active": False, "until": "", "error": ""}
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return result
    except OSError:
        result["error"] = "sidecar_unreadable"
        return result
    try:
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or
                    info.st_uid != 0 or info.st_gid != 0):
                result["error"] = "sidecar_unsafe"
                return result
            if info.st_size > MAX_DIAGNOSTIC_BYTES:
                result["error"] = "sidecar_too_large"
                return result
            raw = b""
            while len(raw) <= MAX_DIAGNOSTIC_BYTES:
                block = os.read(descriptor, MAX_DIAGNOSTIC_BYTES + 1 - len(raw))
                if not block:
                    break
                raw += block
        except OSError:
            result["error"] = "sidecar_unreadable"
            return result
    finally:
        os.close(descriptor)
    if len(raw) > MAX_DIAGNOSTIC_BYTES:
        result["error"] = "sidecar_too_large"
        return result
    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_strict_object,
            parse_constant=lambda child: (_ for _ in ()).throw(ValueError("constant")),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        result["error"] = "sidecar_malformed"
        return result
    if (not isinstance(value, dict) or
            set(value) != {"schema", "version", "until"} or
            value.get("schema") != LOCAL_ONLY_DIAGNOSTIC_SCHEMA):
        result["error"] = "sidecar_malformed"
        return result
    version = value.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        result["error"] = "sidecar_malformed"
        return result
    if version != LOCAL_ONLY_DIAGNOSTIC_VERSION:
        return result
    until = _timestamp(value.get("until"))
    if until is None:
        result["error"] = "sidecar_malformed"
        return result
    current = now
    if current is None:
        current = datetime.datetime.now(datetime.timezone.utc)
    elif isinstance(current, (int, float)):
        current = datetime.datetime.fromtimestamp(current, datetime.timezone.utc)
    if current.tzinfo is None:
        result["error"] = "sidecar_malformed"
        return result
    try:
        mtime = datetime.datetime.fromtimestamp(info.st_mtime, datetime.timezone.utc)
        mtime_limit = datetime.datetime.fromtimestamp(
            info.st_mtime + MAX_DIAGNOSTIC_SECONDS, datetime.timezone.utc)
    except (OSError, OverflowError, ValueError):
        result["error"] = "sidecar_unsafe"
        return result
    current = current.astimezone(datetime.timezone.utc)
    if mtime > current + datetime.timedelta(seconds=MAX_MTIME_FUTURE_SECONDS):
        result["error"] = "sidecar_unsafe"
        return result
    if (until > mtime_limit or
            until > current + datetime.timedelta(seconds=MAX_DIAGNOSTIC_SECONDS)):
        result["error"] = "sidecar_until_too_late"
        return result
    result["until"] = until.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    result["active"] = current < until
    return result
