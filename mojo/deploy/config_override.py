"""Typed django.conf overrides published by the Admin plane."""

import copy
import importlib
import math
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


_REGISTRY = {}
_MODULE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
_RESERVED_KEYS = frozenset({
    "SECRET_KEY", "SECRET_KEY_FALLBACKS", "DATABASES", "CACHES",
    "INSTALLED_APPS", "MIDDLEWARE", "AUTHENTICATION_BACKENDS", "ROOT_URLCONF",
    "WSGI_APPLICATION", "ASGI_APPLICATION", "TEMPLATES", "LOGGING", "DEBUG",
    "SETTINGS_MODULE", "ALLOWED_HOSTS", "KMS_KEY_ID", "AWS_KEY", "AWS_SECRET",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
    "AWS_REGION", "AWS_PROFILE", "AUTH_CONFIG", "EDGE_EXPECTED_TOPOLOGY",
    "EDGE_FRAMEWORK_VERSION", "ASSISTANT_MCP_ENABLED", REVISION_KEY,
    # Existing dedicated writers retain ownership. This registry is also loaded
    # before Django starts, so keep these names explicit rather than importing
    # the application services here.
    "ALLOW_EMAIL_CHANGE", "ALLOW_PHONE_CHANGE", "ALLOW_USERNAME_CHANGE",
    "ALLOW_SELF_DEACTIVATION", "WEBAPP_BASE_URL", "GEOIP_API_KEY_MOJO",
    "BASE_URL", "AWS_CLOUDWATCH_ALARM_TOPIC_ARNS",
})
_RESERVED_PREFIXES = (
    "ADMIN_FLEET_CONFIG_", "CONFIG_SYNC_", "AWS_CONFIG_", "FLEET_CONFIG_",
    "SYSTEM_SETUP_", "MOJO_INSTALLATION_", "ADMIN_PROVIDER_", "LLM_", "FRESH_AUTH_",
)


def _reserved(key):
    return key in _RESERVED_KEYS or key.startswith(_RESERVED_PREFIXES)


def _json_value(value, depth=0):
    if depth > 8:
        return False
    if value is None or type(value) in (bool, int):
        return True
    if type(value) is float:
        return math.isfinite(value)
    if type(value) is str:
        return len(value) <= 8192
    if type(value) is list:
        return len(value) <= 100 and all(_json_value(v, depth + 1) for v in value)
    if type(value) is dict:
        return len(value) <= 100 and all(
            type(k) is str and len(k) <= 128 and _json_value(v, depth + 1)
            for k, v in value.items())
    return False


def _valid_value(definition, value):
    kind = definition["value_type"]
    types = {"boolean": bool, "integer": int, "string": str,
             "list": list, "object": dict}
    if type(value) is not types[kind] or not _json_value(value):
        return False
    if kind == "integer":
        if definition["min_value"] is not None and value < definition["min_value"]:
            return False
        if definition["max_value"] is not None and value > definition["max_value"]:
            return False
    if kind == "string" and len(value) > definition["max_length"]:
        return False
    if kind in ("list", "object") and len(value) > definition["max_items"]:
        return False
    validator = definition["validator"]
    if validator is not None:
        try:
            return validator(value) is True
        except Exception:
            # A validator exception may contain a submitted secret.
            return False
    return True


def register_setting(key, *, label, section, description, value_type,
                     default=None, sensitive=False, restart_required=True,
                     validator=None, max_length=2048, min_value=None,
                     max_value=None, max_items=100):
    """Register trusted application schema; delegation remains independent."""
    if type(key) is not str or not KEY_RE.fullmatch(key) or _reserved(key):
        raise ValueError("fleet setting name is invalid or reserved")
    if value_type not in ("boolean", "integer", "string", "list", "object"):
        raise ValueError("fleet setting type is unsupported")
    if not all(type(v) is str and 0 < len(v) <= 2048
               for v in (label, section, description)):
        raise ValueError("fleet setting metadata is invalid")
    if type(sensitive) is not bool or type(restart_required) is not bool:
        raise ValueError("fleet setting flags must be boolean")
    if (type(max_length) is not int or not 1 <= max_length <= 8192 or
            type(max_items) is not int or not 1 <= max_items <= 100 or
            any(v is not None and type(v) is not int for v in (min_value, max_value)) or
            (min_value is not None and max_value is not None and min_value > max_value) or
            (validator is not None and not callable(validator))):
        raise ValueError("fleet setting constraints are invalid")
    definition = dict(key=key, label=label, section=section,
                      description=description, value_type=value_type,
                      default=copy.deepcopy(default), sensitive=sensitive,
                      restart_required=restart_required, validator=validator,
                      max_length=max_length, min_value=min_value,
                      max_value=max_value, max_items=max_items)
    if default is not None and not _valid_value(definition, default):
        raise ValueError("fleet setting default is invalid")
    existing = _REGISTRY.get(key)
    if existing is not None and existing != definition:
        raise ValueError("conflicting fleet setting definition")
    _REGISTRY[key] = definition
    return get_definition(key)


def get_definition(key):
    """Browser-safe metadata, without callable code or secret defaults."""
    definition = _REGISTRY.get(key)
    if definition is None:
        return None
    return copy.deepcopy({name: value for name, value in definition.items()
                          if name != "validator" and
                          not (name == "default" and definition["sensitive"])})


def definitions():
    return tuple(get_definition(key) for key in sorted(_REGISTRY))


def load_schema_modules(module_names):
    """Load deployment-owned modules without booting Django.

    Never pass module names read from a published document or REST input.
    """
    if module_names in (None, ""):
        return
    if isinstance(module_names, str):
        module_names = [name.strip() for name in module_names.split(",")]
    if (not isinstance(module_names, (list, tuple)) or len(module_names) > 32 or
            any(type(name) is not str or len(name) > 256 or
                not _MODULE_RE.fullmatch(name) for name in module_names)):
        raise ValueError("fleet schema modules must be a bounded list of module names")
    for name in module_names:
        try:
            importlib.import_module(name)
        except Exception:
            raise ValueError("fleet schema module could not be loaded") from None


for _key, _default in DEFAULTS.items():
    register_setting(
        _key, label=_key.replace("_", " ").title(), section="Security & operations",
        description="Fleet GeoIP provider configuration.",
        value_type=("boolean" if isinstance(_default, bool) else
                    "list" if isinstance(_default, list) else "string"),
        default=_default, validator=VALIDATORS[_key])


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
        definition = _REGISTRY.get(key)
        if definition is None or _reserved(key):
            raise ValueError(f"fleet setting has no typed validator: {key}")
        if not _valid_value(definition, value):
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
