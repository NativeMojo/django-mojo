"""Bounded redaction for every System Setup trust boundary."""

import json
import math
import re
from urllib.parse import urlsplit, urlunsplit


MAX_DEPTH = 5
MAX_ITEMS = 256
MAX_STRING_BYTES = 1000
MAX_INPUT_CHARACTERS = 8192
MAX_SERIALIZED_BYTES = 65536
REDACTED = "[redacted]"
TRUNCATED = "[truncated]"

_SENSITIVE_KEY = re.compile(
    r"secret|token|password|credential|authorization|presign|access[_-]?key|"
    r"private[_-]?key|session[_-]?key", re.I)
_AWS_KEY = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_PEM = re.compile(r"-----BEGIN [^-]*(?:PRIVATE KEY|CREDENTIAL)[^-]*-----", re.I)
_LABELED_SECRET = re.compile(
    r"(?i)\b(?:password|secret|token|credential|authorization|private[_ -]?key|"
    r"access[_ -]?key)\s*[:=]\s*[^\s,;]+")
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{8,}=*")
_OPAQUE_TOKEN = re.compile(r"(?<![A-Za-z0-9/+=_-])[A-Za-z0-9/+=_-]{32,8192}(?![A-Za-z0-9/+=_-])")


def is_sensitive_name(value):
    return bool(_SENSITIVE_KEY.search(str(value)))


def _safe_string(value):
    if isinstance(value, (bytes, bytearray)) and len(value) > MAX_INPUT_CHARACTERS:
        return TRUNCATED
    text = str(value)
    # Bound attacker-controlled input before any regex or URL parser sees it.
    if len(text) > MAX_INPUT_CHARACTERS:
        return TRUNCATED
    if (_AWS_KEY.search(text) or _JWT.search(text) or _PEM.search(text) or
            _LABELED_SECRET.search(text) or _BEARER.search(text)):
        return REDACTED
    for match in _OPAQUE_TOKEN.finditer(text):
        token = match.group(0)
        counts = {char: token.count(char) for char in set(token)}
        entropy = -sum(
            (count / len(token)) * math.log2(count / len(token))
            for count in counts.values())
        if entropy >= 4.2:
            return REDACTED
    try:
        parsed = urlsplit(text)
    except ValueError:
        parsed = None
    if parsed and parsed.scheme.lower() in ("http", "https") and parsed.netloc:
        host = parsed.hostname or ""
        display_host = f"[{host}]" if ":" in host else host
        try:
            port = parsed.port
        except ValueError:
            return REDACTED
        default_port = 443 if parsed.scheme.lower() == "https" else 80
        netloc = display_host if port in (None, default_port) else f"{display_host}:{port}"
        query = "redacted" if parsed.query else ""
        text = urlunsplit((parsed.scheme.lower(), netloc, parsed.path, query, ""))
    raw = text.encode("utf-8", errors="replace")
    if len(raw) > MAX_STRING_BYTES:
        text = raw[:MAX_STRING_BYTES].decode("utf-8", errors="ignore") + TRUNCATED
    return text


def sanitize(value, max_bytes=MAX_SERIALIZED_BYTES):
    """Return JSON-safe bounded data with secrets removed independent of key."""
    budget = {"items": MAX_ITEMS, "bytes": max(256, int(max_bytes) - 4096)}

    def clean(item, depth=0, sensitive=False):
        if sensitive:
            return REDACTED
        if depth > MAX_DEPTH or budget["items"] <= 0 or budget["bytes"] <= 0:
            return TRUNCATED
        budget["items"] -= 1
        if isinstance(item, dict):
            output = {}
            for key, child in item.items():
                if budget["items"] <= 0 or budget["bytes"] <= 0:
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
                    output.append(TRUNCATED)
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
    encoded = json.dumps(output, separators=(",", ":"), default=str).encode("utf-8")
    if len(encoded) <= max_bytes:
        return output
    # This should only be reachable for unusually expensive JSON escaping.
    if isinstance(output, dict):
        return {"truncated": True}
    return TRUNCATED
