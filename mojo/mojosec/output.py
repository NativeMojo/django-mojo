"""Small settings-free output and status-file helpers."""

import json
import os
import sys
import tempfile

from .protocol import canonical_json, utc_now


def emit(level, message, stream=None, **fields):
    stream = sys.stderr if stream is None else stream
    record = {"at": utc_now(), "level": level, "message": str(message)[:1024]}
    record.update(fields)
    stream.write(canonical_json(record) + "\n")
    stream.flush()


def emit_error(message, error, stream=None, **fields):
    """Best-effort local error record with a bounded public classification."""
    fields["error"] = str(error)[:256]
    try:
        emit("error", message, stream=stream, **fields)
    except Exception:
        return False
    return True


def write_status(path, status):
    """Atomically publish a non-secret, world-readable health snapshot."""
    parent = os.path.dirname(path)
    os.makedirs(parent, mode=0o755, exist_ok=True)
    payload = dict(status)
    payload["updated_at"] = utc_now()
    descriptor, temp_path = tempfile.mkstemp(prefix=".mojosec-status-", dir=parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, 0o640)
        os.replace(temp_path, path)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise
    return payload


def read_status(path):
    with open(path, "r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("status file must contain a JSON object")
    return value
