"""Bounded redaction for every System Setup trust boundary."""

import json
import re

from mojo.helpers.safe_text import sanitize_scalar


# A serialized readiness report reaches depth six at ordinary scalar evidence
# and a serialized operation reaches depth seven at choice enum values. Keep
# both framework-owned envelopes usable while still cutting off deeper provider
# structures.
MAX_DEPTH = 7
MAX_ITEMS = 256
MAX_STRING_BYTES = 1000
MAX_INPUT_CHARACTERS = 8192
MAX_SERIALIZED_BYTES = 65536
REDACTED = "[redacted]"
TRUNCATED = "[truncated]"

_SENSITIVE_KEY = re.compile(
    r"secret|token|password|credential|authorization|presign|access[_-]?key|"
    r"private[_-]?key|session[_-]?key", re.I)
def is_sensitive_name(value):
    return bool(_SENSITIVE_KEY.search(str(value)))


def _safe_string(value):
    # MAX_STRING_BYTES historically bounded the retained prefix, with the
    # truncation marker appended. Preserve that exact Setup contract while the
    # shared helper itself exposes a hard output-byte limit.
    return sanitize_scalar(
        value,
        max_input_characters=MAX_INPUT_CHARACTERS,
        max_bytes=MAX_STRING_BYTES + len(TRUNCATED.encode("utf-8")),
        redacted=REDACTED,
        truncated=TRUNCATED,
    )


def sanitize(value, max_bytes=MAX_SERIALIZED_BYTES):
    """Return JSON-safe bounded data with secrets removed independent of key."""
    budget = {
        "items": MAX_ITEMS,
        "bytes": max(256, int(max_bytes) - 4096),
        "truncated": False,
    }

    def clean(item, depth=0, sensitive=False):
        if sensitive:
            return REDACTED
        if depth > MAX_DEPTH or budget["items"] <= 0 or budget["bytes"] <= 0:
            budget["truncated"] = True
            return TRUNCATED
        budget["items"] -= 1
        if isinstance(item, dict):
            output = {}
            for key, child in item.items():
                if budget["items"] <= 0 or budget["bytes"] <= 0:
                    budget["truncated"] = True
                    output["truncated"] = True
                    break
                name = _safe_string(key)[:80]
                budget["bytes"] -= len(name.encode("utf-8")) + 8
                output[name] = clean(child, depth + 1, is_sensitive_name(name))
            return output
        if isinstance(item, (list, tuple)):
            output = []
            for child in item:
                if budget["items"] <= 0 or budget["bytes"] <= 0:
                    # A scalar sentinel corrupts typed collections such as
                    # readiness sections/checks. Omit the remaining entries
                    # and publish truncation metadata on the root envelope.
                    budget["truncated"] = True
                    break
                output.append(clean(child, depth + 1))
            return output
        if isinstance(item, str):
            output = _safe_string(item)
            budget["bytes"] -= len(output.encode("utf-8")) + 4
            return output
        if isinstance(item, (int, float, bool, type(None))):
            budget["bytes"] -= 32
            return item
        return clean(str(item), depth + 1)

    output = clean(value)
    if budget["truncated"] and isinstance(output, dict):
        output["truncated"] = True
    encoded = json.dumps(output, separators=(",", ":"), default=str).encode("utf-8")
    if len(encoded) <= max_bytes:
        return output
    # This should only be reachable for unusually expensive JSON escaping.
    if isinstance(output, dict):
        return {"truncated": True}
    return TRUNCATED


def failure_metadata(error, action):
    """Bounded non-secret exception identity for durable state and logs.

    Exception messages are provider-controlled and can echo request values.
    Callers that need an audit marker persist/log only this action and class;
    full traceback detail remains in the protected exception path.
    """
    metadata = sanitize({
        "action": str(action or "operation")[:80],
        "exception_class": type(error).__name__[:80],
    }, max_bytes=512)
    return {
        "action": metadata.get("action") or "operation",
        "exception_class": metadata.get("exception_class") or "Exception",
    }
