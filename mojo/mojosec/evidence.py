"""Deterministic, byte-bounded raw evidence for MojoSec observations."""

import hashlib
import json
import math

from .protocol import MAX_ATTRIBUTE_BYTES, canonical_json


# Leave room for future protocol-v1 envelope fields and aggregation metadata.
EVIDENCE_BYTES = MAX_ATTRIBUTE_BYTES - 512

FIELD_POLICIES = {
    "auth.ssh_login": (
        ("source_ip", 64), ("user", 128), ("auth_method", 64),
        ("boot_id", 64), ("audit_session", 32), ("tty", 96),
    ),
    "auth.ssh_failure": (
        ("source_ip", 64), ("user", 128), ("auth_method", 64),
        ("boot_id", 64), ("audit_session", 32), ("tty", 96),
    ),
    "auth.sudo_command": (
        ("source_ip", 64), ("actor", 128), ("target_user", 128),
        ("tty", 96), ("boot_id", 64), ("audit_session", 32),
        ("attribution_provenance", 32), ("cwd", 512),
        ("command_path", 512), ("command_sha256", 64), ("command", 2048),
    ),
    "auth.sudo_failure": (
        ("source_ip", 64), ("actor", 128), ("tty", 96),
        ("boot_id", 64), ("audit_session", 32),
        ("attribution_provenance", 32),
    ),
    "auth.session_open": (
        ("source_ip", 64), ("attribution_provenance", 32),
        ("service", 128), ("target_user", 128), ("target_uid", None),
        ("opener_uid", None), ("producer_uid", None), ("producer_pid", None),
        ("producer_comm", 96), ("producer_exe", 256), ("systemd_unit", 256),
        ("boot_id", 64), ("audit_session", None), ("audit_loginuid", None),
        ("tty", 96),
    ),
    "web.probe": (
        ("source_ip", 64), ("peer_ip", 64), ("method", 32),
        ("status", None), ("host", 512), ("upstream_status", 256),
        ("request_id", 64), ("scheme", 16), ("protocol", 32),
        ("tls_protocol", 96), ("tls_cipher", 96),
        ("remote_port", None), ("peer_port", None), ("server_port", None),
        ("request_length", None), ("response_bytes", None),
        ("response_body_bytes", None), ("request_time", 64),
        ("upstream_connect_time", 256), ("upstream_header_time", 256),
        ("upstream_response_time", 256), ("upstream_response_length", 256),
        ("upstream_bytes_received", 256), ("upstream_bytes_sent", 256),
        ("path", 1024), ("request_uri", 2048), ("referrer", 1536),
        ("user_agent", 1536),
    ),
    "web.error": (),
    "web.denied": (),
    "system.oom": (("unit", 256), ("message", 1024)),
    "system.service_error": (
        ("unit", 256), ("priority", None), ("message", 1024),
        ("failure_kind", 64),
    ),
}
FIELD_POLICIES["web.error"] = FIELD_POLICIES["web.probe"]
FIELD_POLICIES["web.denied"] = FIELD_POLICIES["web.probe"]


def _safe_text(value):
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    # JSON may decode escaped lone surrogates. Normalize them before any UTF-8
    # accounting so one poisoned record cannot wedge a durable cursor.
    return value.replace("\x00", "").encode("utf-8", errors="replace").decode("utf-8")


def digest_text(value):
    return hashlib.sha256(_safe_text(value).encode("utf-8")).hexdigest()


def _prefix(value, byte_limit):
    raw = value.encode("utf-8")
    if len(raw) <= byte_limit:
        return value
    return raw[:byte_limit].decode("utf-8", errors="ignore")


def _fits(value):
    return len(canonical_json(value).encode("utf-8")) <= EVIDENCE_BYTES


def _add_text(result, key, value, byte_limit):
    value = _safe_text(value)
    raw = value.encode("utf-8")
    truncated = len(raw) > byte_limit
    prefix = _prefix(value, byte_limit)
    digest = hashlib.sha256(raw).hexdigest()

    def candidate(limit):
        trial = dict(result)
        trial[key] = _prefix(prefix, limit)
        if truncated or limit < len(prefix.encode("utf-8")):
            trial[key + "_truncated"] = True
            trial[key + "_sha256"] = digest
        return trial

    trial = candidate(len(prefix.encode("utf-8")))
    if _fits(trial):
        result.clear()
        result.update(trial)
        return
    low = 0
    high = len(prefix.encode("utf-8"))
    best = None
    while low <= high:
        middle = (low + high) // 2
        trial = candidate(middle)
        if _fits(trial):
            best = trial
            low = middle + 1
        else:
            high = middle - 1
    if best is not None:
        result.clear()
        result.update(best)
        return
    marker = dict(result)
    marker[key + "_truncated"] = True
    marker[key + "_sha256"] = digest
    if _fits(marker):
        result.clear()
        result.update(marker)


def build_evidence(kind, values):
    """Apply one kind's allowlist, priority, and encoded byte budget."""
    policies = FIELD_POLICIES.get(kind)
    if policies is None:
        raise ValueError(f"unsupported MojoSec evidence kind: {kind}")
    result = {}
    for key, byte_limit in policies:
        if key not in values or values[key] is None or values[key] == "":
            continue
        value = values[key]
        if byte_limit is not None:
            _add_text(result, key, value, byte_limit)
            continue
        if (value is None or isinstance(value, (bool, int)) or
                isinstance(value, float) and math.isfinite(value)):
            trial = dict(result)
            trial[key] = value
            if _fits(trial):
                result = trial
    # Assert the invariant here, before SQLite persistence rather than at send.
    if len(canonical_json(result).encode("utf-8")) > EVIDENCE_BYTES:
        raise ValueError("MojoSec evidence budget invariant failed")
    return result


def fingerprint_values(evidence, fields):
    """Stable values that distinguish every selected projected scalar."""
    values = []
    for field in fields:
        value = evidence.get(field)
        digest = evidence.get(field + "_sha256")
        values.append(digest or value or "")
    return tuple(values)


def encoded_size(evidence):
    return len(json.dumps(
        evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8"))
