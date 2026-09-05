"""Secret-only serialization and bounded Admin Security detail transport."""

import base64
import hashlib
import json
import re

from mojo.helpers.crypto.sign import generate_signature, verify_signature
from mojo.helpers.settings import settings


CURSOR_VERSION = 1
DEFAULT_CHUNK_BYTES = 16384
MIN_CHUNK_BYTES = 1024
MAX_CHUNK_BYTES = 65536
REDACTED = "[redacted secret]"

_SECRET_KEYS = frozenset({
    "password", "passwd", "passphrase", "secret", "token", "client_secret",
    "provider_secret", "webhook_secret", "signing_secret", "api_key", "apikey",
    "auth_key", "encryption_key", "access_token",
    "refresh_token", "auth_token", "authorization", "bearer",
    "private_key", "private_key_pem", "signing_key", "source_key",
    "secret_key", "aws_secret_access_key", "otp", "otp_code", "totp",
    "totp_code", "mfa", "mfa_code", "one_time_password", "session",
    "sessionid", "session_id", "session_token", "csrf_token",
    "csrfmiddlewaretoken",
})
_SECRET_CONTAINERS = frozenset({"credential", "credentials", "headers", "cookies"})
_ASSIGNMENT_RE = re.compile(
    r"(?i)(\b(?:password|passwd|passphrase|client_secret|provider_secret|"
    r"webhook_secret|signing_secret|secret[_-]?key|aws_secret_access_key|"
    r"api[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token|"
    r"bearer[_-]?token|auth[_-]?key|encryption[_-]?key|"
    r"private[_-]?key|otp(?:[_-]?code)?|totp(?:[_-]?code)?|"
    r"mfa(?:[_-]?code)?|one[_-]?time[_-]?password|session(?:id|[_-]?id|[_-]?token)?|"
    r"csrf(?:middleware)?token)\b\s*[:=]\s*)"
    r"([^\s,;&]+)")
_AUTH_HEADER_RE = re.compile(
    r"(?i)(\b(?:proxy-)?authorization\s*:\s*"
    r"(?:basic|bearer|digest)\s+)[^\s,;]+")
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN(?: [A-Z0-9]+)? PRIVATE KEY-----.*?"
    r"-----END(?: [A-Z0-9]+)? PRIVATE KEY-----", re.DOTALL)
_URL_USERINFO_RE = re.compile(
    r"(?i)(\b[a-z][a-z0-9+.-]*://[^/@:\s]*:)([^/@\s]+)(@)")
_AUTH_QUERY_RE = re.compile(
    r"(?i)([?&](?:access_token|refresh_token|api[_-]?key|auth(?:orization)?|"
    r"token|signature|x-amz-signature)=)([^&#\s]*)")
_COOKIE_SECRET_RE = re.compile(
    r"(?i)(\b(?:sessionid|session_id|session|session_token|"
    r"csrftoken|csrfmiddlewaretoken|otp|totp|mfa)\s*=\s*)"
    r"([^;,&\s]+)")


class TransportError(ValueError):
    def __init__(self, message, code="invalid_cursor", status=400):
        self.code = code
        self.status = status
        super().__init__(message)


def _secret_key(name):
    normalized = str(name).strip().lower().replace("-", "_")
    return (normalized in _SECRET_KEYS or normalized.endswith("_password") or
            normalized.endswith("_secret") or normalized.endswith("_token") or
            normalized.endswith("_private_key") or
            normalized.endswith("_secret_key") or
            normalized.endswith("_api_key") or
            normalized.endswith("_otp") or normalized.endswith("_mfa"))


def scrub_text(value):
    """Remove embedded authentication material but preserve surrounding text."""
    sentinel = "\x00MOJO_ADMIN_SECRET_REDACTED\x00"
    value = value.replace(REDACTED, sentinel)
    value = _PRIVATE_KEY_RE.sub(sentinel, value)
    value = _URL_USERINFO_RE.sub(
        lambda match: match.group(1) + sentinel + match.group(3), value)
    value = _AUTH_QUERY_RE.sub(
        lambda match: match.group(1) + sentinel, value)
    value = _AUTH_HEADER_RE.sub(
        lambda match: match.group(1) + sentinel, value)
    value = _COOKIE_SECRET_RE.sub(
        lambda match: match.group(1) + sentinel, value)
    value = _ASSIGNMENT_RE.sub(lambda match: match.group(1) + sentinel, value)
    return value.replace(sentinel, REDACTED)


def scrub(value, key=None):
    """Recursively scrub only authentication-secret values."""
    normalized = (str(key).strip().lower().replace("-", "_")
                  if key is not None else None)
    if normalized in {"credential", "credentials"}:
        if not isinstance(value, (dict, list, tuple)):
            return REDACTED
        if isinstance(value, (list, tuple)):
            return [scrub(item) if isinstance(item, (dict, list, tuple))
                    else REDACTED for item in value]
    if (normalized in _SECRET_CONTAINERS and
            isinstance(value, (dict, list, tuple))):
        # A credentials/headers/cookies object mixes secrets with operational
        # siblings. Walk it instead of erasing usernames, hosts, paths, or
        # harmless cookie preferences with the container label.
        key = None
    if key is not None and _secret_key(key):
        return REDACTED
    if isinstance(value, dict):
        return {str(name): scrub(item, key=name) for name, item in value.items()}
    if isinstance(value, list):
        return [scrub(item) for item in value]
    if isinstance(value, tuple):
        return [scrub(item) for item in value]
    if isinstance(value, str):
        return scrub_text(value)
    return value


def _chunk_size():
    value = settings.get_static(
        "ADMIN_SECURITY_CHUNK_BYTES", DEFAULT_CHUNK_BYTES, kind="int")
    return max(MIN_CHUNK_BYTES, min(MAX_CHUNK_BYTES, int(value)))


def _serialize(value):
    safe = scrub(value)
    if isinstance(safe, str):
        return "text", safe.encode("utf-8")
    return "json", json.dumps(
        safe, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str).encode("utf-8")


def _b64encode(value):
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def issue_cursor(payload):
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    encoded = _b64encode(raw)
    return f"{encoded}.{generate_signature(encoded)}"


def read_cursor(cursor):
    if not isinstance(cursor, str) or len(cursor) > 4096 or cursor.count(".") != 1:
        raise TransportError("Admin Security cursor is invalid")
    encoded, signature = cursor.split(".", 1)
    if not verify_signature(encoded, signature):
        raise TransportError("Admin Security cursor is invalid")
    try:
        value = json.loads(_b64decode(encoded).decode("utf-8"))
    except Exception as error:
        raise TransportError("Admin Security cursor is invalid") from error
    required = {"v", "scope", "kind", "id", "field", "revision", "digest", "offset"}
    if (not isinstance(value, dict) or set(value) != required or
            value.get("v") != CURSOR_VERSION or
            not isinstance(value.get("scope"), str) or
            not isinstance(value.get("kind"), str) or
            isinstance(value.get("id"), bool) or not isinstance(value.get("id"), int) or
            not isinstance(value.get("field"), str) or
            not isinstance(value.get("revision"), str) or
            not isinstance(value.get("digest"), str) or
            not re.fullmatch(r"[0-9a-f]{64}", value["digest"]) or
            isinstance(value.get("offset"), bool) or
            not isinstance(value.get("offset"), int) or value["offset"] < 0):
        raise TransportError("Admin Security cursor is invalid")
    return value


def issue_page_cursor(authority, section, window, position, limit, snapshot):
    """Issue an opaque discovery cursor bound to scope and one snapshot."""
    return issue_cursor({
        "v": CURSOR_VERSION, "purpose": "rows",
        "scope": authority.cursor_scope, "section": section,
        "start": window["start"], "end": window["end"],
        "hours": window["hours"],
        "position": list(position), "limit": limit, "snapshot": snapshot,
    })


def read_page_cursor(cursor):
    """Validate and return a signed discovery-list cursor."""
    if not isinstance(cursor, str) or len(cursor) > 4096 or cursor.count(".") != 1:
        raise TransportError("Admin Security page cursor is invalid")
    encoded, signature = cursor.split(".", 1)
    if not verify_signature(encoded, signature):
        raise TransportError("Admin Security page cursor is invalid")
    try:
        value = json.loads(_b64decode(encoded).decode("utf-8"))
    except Exception as error:
        raise TransportError("Admin Security page cursor is invalid") from error
    required = {
        "v", "purpose", "scope", "section", "start", "end", "hours",
        "position", "limit", "snapshot"}
    position = value.get("position") if isinstance(value, dict) else None
    if (not isinstance(value, dict) or set(value) != required or
            value.get("v") != CURSOR_VERSION or
            value.get("purpose") != "rows" or
            not isinstance(value.get("scope"), str) or
            not isinstance(value.get("section"), str) or
            not 1 <= len(value["section"]) <= 32 or
            not isinstance(value.get("start"), str) or
            not 1 <= len(value["start"]) <= 64 or
            not isinstance(value.get("end"), str) or
            not 1 <= len(value["end"]) <= 64 or
            isinstance(value.get("hours"), bool) or
            not isinstance(value.get("hours"), int) or
            not 1 <= value["hours"] <= 2160 or
            not isinstance(position, list) or len(position) != 2 or
            isinstance(position[1], bool) or not isinstance(position[1], int) or
            position[1] < 1 or
            isinstance(value.get("limit"), bool) or
            not isinstance(value.get("limit"), int) or
            not 1 <= value["limit"] <= 100 or
            not isinstance(value.get("snapshot"), str) or
            not re.fullmatch(r"[0-9a-f]{64}", value["snapshot"]) or
            not isinstance(position[0], (str, int)) or
            isinstance(position[0], bool)):
        raise TransportError("Admin Security page cursor is invalid")
    return value


def _utf8_slice(raw, offset, maximum):
    end = min(len(raw), offset + maximum)
    while end > offset:
        try:
            return raw[offset:end].decode("utf-8"), end
        except UnicodeDecodeError:
            end -= 1
    return "", offset


def chunk(value, authority, object_kind, object_id, field, revision,
          cursor=None):
    """Return one complete-value chunk with a scope/revision-bound cursor."""
    encoding, raw = _serialize(value)
    digest = hashlib.sha256(raw).hexdigest()
    offset = 0
    if cursor is not None:
        token = read_cursor(cursor)
        expected = {
            "scope": authority.cursor_scope, "kind": object_kind,
            "id": object_id, "field": field, "revision": str(revision),
            "digest": digest,
        }
        if any(token.get(name) != expected[name] for name in expected):
            raise TransportError(
                "Admin Security evidence changed; restart detail retrieval",
                code="stale_cursor", status=409)
        offset = token["offset"]
    if offset > len(raw):
        raise TransportError("Admin Security cursor is invalid")
    text, next_offset = _utf8_slice(raw, offset, _chunk_size())
    complete = next_offset >= len(raw)
    next_cursor = None
    if not complete:
        next_cursor = issue_cursor({
            "v": CURSOR_VERSION, "scope": authority.cursor_scope,
            "kind": object_kind, "id": object_id, "field": field,
            "revision": str(revision), "digest": digest,
            "offset": next_offset,
        })
    return {
        "encoding": encoding, "chunk": text, "offset": offset,
        "next_offset": next_offset, "byte_length": len(raw),
        "complete": complete, "next_cursor": next_cursor,
        "digest": digest,
    }


def bounded_detail(values, authority, object_kind, object_id, revision,
                   chunk_fields):
    """Scrub a detail mapping and chunk every named large/evidence field."""
    safe = scrub(values)
    for field in chunk_fields:
        if field in values:
            safe[field] = chunk(
                values[field], authority, object_kind, object_id, field,
                revision)
    return safe
