"""Typed, non-secret django.conf overrides published by the Admin plane."""

import hashlib
import json
import re
from urllib.parse import urlsplit


SCHEMA_VERSION = 1
REVISION_KEY = "MOJO_FLEET_CONFIG_REVISION"
KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,127}$")
PROVIDER_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
REVISION_RE = re.compile(r"^[a-f0-9]{32,64}$")
MAX_DOCUMENT_BYTES = 32768
MAX_SETTINGS = 64


def _provider(value):
    return isinstance(value, str) and bool(PROVIDER_RE.fullmatch(value))


def _providers(value):
    return (isinstance(value, list) and len(value) <= 16 and
            all(_provider(item) for item in value) and len(value) == len(set(value)))


def https_origin(value):
    if not isinstance(value, str) or len(value) > 512:
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme == "https" and parsed.hostname and not parsed.username and
        not parsed.password and parsed.path in ("", "/") and not parsed.query and
        not parsed.fragment and port in (None, 443))


VALIDATORS = {
    "GEOIP_PRIMARY_PROVIDER": _provider,
    "GEOIP_FALLBACK_PROVIDER": _provider,
    "GEOIP_ADDITIONAL_PROVIDERS": _providers,
    "GEOIP_MOJO_PROVIDER_URL": https_origin,
    "GEOIP_MOJO_SYNC_ENABLED": lambda value: isinstance(value, bool),
}


DEFAULTS = {
    "GEOIP_PRIMARY_PROVIDER": "mojo",
    "GEOIP_FALLBACK_PROVIDER": "ipinfo",
    "GEOIP_ADDITIONAL_PROVIDERS": [],
    "GEOIP_MOJO_PROVIDER_URL": "https://api.mojoverify.com",
    "GEOIP_MOJO_SYNC_ENABLED": False,
}


def normalize_allowed(value):
    if isinstance(value, str):
        value = value.split(",")
    if isinstance(value, dict):
        value = list(value)
    if not isinstance(value, (list, tuple, set, frozenset)):
        return frozenset()
    return frozenset(
        str(key).strip() for key in value
        if KEY_RE.fullmatch(str(key).strip()))


def validate_settings(values, allowed):
    if not isinstance(values, dict) or len(values) > MAX_SETTINGS:
        raise ValueError("fleet settings must be a bounded object")
    allowed = normalize_allowed(allowed)
    clean = {}
    for key, value in values.items():
        if key not in allowed:
            raise ValueError(f"fleet setting is not delegated: {key}")
        validator = VALIDATORS.get(key)
        if validator is None:
            raise ValueError(f"fleet setting has no typed validator: {key}")
        if not validator(value):
            raise ValueError(f"fleet setting has an invalid value: {key}")
        clean[key] = value.rstrip("/") if key == "GEOIP_MOJO_PROVIDER_URL" else value
    return clean


def encode_document(settings_values, revision, published_at, allowed):
    clean = validate_settings(settings_values, allowed)
    document = {
        "schema_version": SCHEMA_VERSION,
        "revision": str(revision),
        "published_at": str(published_at),
        "settings": clean,
    }
    payload = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_DOCUMENT_BYTES:
        raise ValueError("fleet settings document is too large")
    return payload


def decode_document(payload, allowed):
    if not isinstance(payload, bytes) or len(payload) > MAX_DOCUMENT_BYTES:
        raise ValueError("fleet settings document is invalid")
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ValueError("fleet settings document is not valid JSON") from None
    if not isinstance(document, dict) or set(document) != {
            "schema_version", "revision", "published_at", "settings"}:
        raise ValueError("fleet settings document has an invalid shape")
    if document["schema_version"] != SCHEMA_VERSION:
        raise ValueError("fleet settings document version is unsupported")
    revision = document["revision"]
    if not isinstance(revision, str) or not REVISION_RE.fullmatch(revision):
        raise ValueError("fleet settings revision is invalid")
    document["settings"] = validate_settings(document["settings"], allowed)
    return document


def compose(base_payload, document):
    if not isinstance(base_payload, bytes) or not base_payload.strip():
        raise ValueError("canonical django.conf is empty")
    revision = document["revision"]
    lines = [base_payload.rstrip(b"\n"), b"", b"# django-mojo managed fleet overrides"]
    for key in sorted(document["settings"]):
        value = document["settings"][key]
        lines.append(f"{key} = {value!r}".encode("utf-8"))
    lines.append(f"{REVISION_KEY} = {revision!r}".encode("utf-8"))
    lines.append(b"")
    return b"\n".join(lines)


def sha256(payload):
    return hashlib.sha256(payload).hexdigest()
