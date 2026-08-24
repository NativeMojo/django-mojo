"""Pure, settings-free redaction and UTF-8 bounding for scalar text."""

import math
import re
from urllib.parse import urlsplit, urlunsplit


REDACTED = "[redacted]"
TRUNCATED = "[truncated]"

_AWS_KEY = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_PEM = re.compile(r"-----BEGIN [^-]*(?:PRIVATE KEY|CREDENTIAL)[^-]*-----", re.I)
_LABELED_SECRET = re.compile(
    r"(?i)\b(?:password|secret|token|credential|authorization|private[_ -]?key|"
    r"access[_ -]?key)\s*[:=]\s*[^\s,;]+")
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{8,}=*")
_OPAQUE_TOKEN = re.compile(
    r"(?<![A-Za-z0-9/+=_-])[A-Za-z0-9/+=_-]{32,8192}(?![A-Za-z0-9/+=_-])")
_URL = re.compile(r"https?://[^\s<>'\"]+", re.I)


def bound_utf8(value, max_bytes, marker=TRUNCATED):
    """Return valid UTF-8 text no larger than max_bytes, marker included."""
    text = str(value)
    maximum = max(0, int(max_bytes))
    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= maximum:
        return text
    marker_raw = str(marker).encode("utf-8", errors="replace")
    if len(marker_raw) >= maximum:
        return marker_raw[:maximum].decode("utf-8", errors="ignore")
    prefix = raw[:maximum - len(marker_raw)].decode("utf-8", errors="ignore")
    return prefix + marker_raw.decode("utf-8", errors="ignore")


def sanitize_scalar(value, max_input_characters=8192, max_bytes=1000,
                    redacted=REDACTED, truncated=TRUNCATED):
    """Redact secret-shaped scalar text and apply one hard UTF-8 byte cap."""
    if (isinstance(value, (bytes, bytearray)) and
            len(value) > max_input_characters):
        return truncated
    text = str(value)
    if len(text) > max_input_characters:
        return truncated
    if (_AWS_KEY.search(text) or _JWT.search(text) or _PEM.search(text) or
            _LABELED_SECRET.search(text) or _BEARER.search(text)):
        return redacted
    for match in _OPAQUE_TOKEN.finditer(text):
        token = match.group(0)
        counts = {character: token.count(character) for character in set(token)}
        entropy = -sum(
            (count / len(token)) * math.log2(count / len(token))
            for count in counts.values())
        if entropy >= 4.2:
            return redacted
    for match in _URL.finditer(text):
        if match.start() == 0 and match.end() == len(text):
            continue
        try:
            embedded = urlsplit(match.group(0))
        except ValueError:
            return redacted
        if (embedded.netloc and
                (embedded.username is not None or embedded.password is not None or
                 embedded.query or embedded.fragment)):
            # A prose-wrapped URL cannot be safely reconstructed without
            # guessing where punctuation stops. Redact the complete scalar.
            return redacted
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
            return redacted
        default_port = 443 if parsed.scheme.lower() == "https" else 80
        netloc = display_host if port in (None, default_port) else f"{display_host}:{port}"
        query = "redacted" if parsed.query else ""
        text = urlunsplit((parsed.scheme.lower(), netloc, parsed.path, query, ""))
    return bound_utf8(text, max_bytes, marker=truncated)
