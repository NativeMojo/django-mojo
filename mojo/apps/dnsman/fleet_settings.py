"""Installed shared-validation schema and approved identities, before Django.

Registration is deployment-owned Python code. Neither database Settings nor a
published value can install profiles, publication policies or delegations.
"""
import copy
import hashlib
import ipaddress
import json
import re
import uuid
from urllib.parse import urlsplit, urlunsplit

from mojo.deploy import config_override

KEYS = ("DNSMAN_ACME_HUB_PROFILE_ID", "DNSMAN_ACME_HUB_URL", "DNSMAN_ACME_HUB_API_KEY")
PROFILE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_PROFILES = {}


def normalize_origin(value):
    if (not isinstance(value, str) or not value or len(value) > 512
            or value != value.strip() or "\\" in value
            or any(c.isspace() or ord(c) < 32 for c in value)):
        raise ValueError("invalid hub origin")
    try:
        parsed = urlsplit(value)
        host, port = parsed.hostname, parsed.port
        if (not host or not parsed.netloc or parsed.username is not None
                or parsed.password is not None or parsed.path not in ("", "/")
                or parsed.query or parsed.fragment or port == 0 or "%" in host):
            raise ValueError()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            host = host.encode("idna").decode("ascii").lower().rstrip(".")
            if host != "localhost":
                labels = host.split(".")
                if len(labels) < 2 or len(host) > 253 or any(not re.fullmatch(
                        r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part) for part in labels):
                    raise ValueError()
            loopback = host == "localhost"
        else:
            loopback = address.is_loopback
            host = address.compressed
            if address.version == 6:
                host = f"[{host}]"
        if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
            raise ValueError()
        return urlunsplit((parsed.scheme, host + (f":{port}" if port is not None else ""), "", "", ""))
    except (ValueError, UnicodeError):
        raise ValueError("invalid hub origin") from None


def project_uuid(value):
    if not isinstance(value, str) or len(value) not in (32, 36):
        raise ValueError("invalid project UUID")
    try:
        parsed = uuid.UUID(value)
        if value not in (parsed.hex, str(parsed)):
            raise ValueError()
        return parsed.hex
    except (ValueError, AttributeError):
        raise ValueError("invalid project UUID") from None


def valid_key(value):
    return (isinstance(value, str) and 0 < len(value) <= 4096 and value == value.strip()
            and not any(c.isspace() or ord(c) < 32 for c in value))


def _origin(value):
    if value == "":
        return True
    try:
        return normalize_origin(value) == value
    except ValueError:
        return False


def register_hub_profile(profile_id, *, label, origin, consumer_project_uuid, consumer_label):
    if (not isinstance(profile_id, str) or not PROFILE_RE.fullmatch(profile_id)
            or any(not isinstance(v, str) or not 0 < len(v) <= 200
                   or any(ord(c) < 32 for c in v) for v in (label, consumer_label))):
        raise ValueError("invalid hub profile")
    row = dict(id=profile_id, label=label, origin=normalize_origin(origin),
               consumer_project_uuid=project_uuid(consumer_project_uuid), consumer_label=consumer_label)
    if len(_PROFILES) >= 32 and profile_id not in _PROFILES:
        raise ValueError("hub profile catalog is full")
    if profile_id in _PROFILES and _PROFILES[profile_id] != row:
        raise ValueError("conflicting hub profile")
    _PROFILES[profile_id] = row
    return copy.deepcopy(row)


def profiles():
    return [copy.deepcopy(_PROFILES[key]) for key in sorted(_PROFILES)]


def profile(profile_id):
    return copy.deepcopy(_PROFILES.get(profile_id))


def catalog_revision():
    return hashlib.sha256(json.dumps(profiles(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _publication_policy(old, candidate):
    # Startup/decode must never import Django services or probe a network.
    from mojo.apps.dnsman.services.connection_config import publication_policy
    publication_policy(old, candidate)


for _name in KEYS:
    config_override.register_setting(
        _name, label={KEYS[0]: "Approved hub profile", KEYS[1]: "Approved hub origin",
                      KEYS[2]: "Protected recipient credential"}[_name],
        section="Shared validation", description="Managed shared-validation connection; use its protected topic.",
        value_type="string", default="", sensitive=_name == KEYS[2], restart_required=True,
        max_length=4096 if _name == KEYS[2] else 512 if _name == KEYS[1] else 64,
        validator=(lambda value: value == "" or bool(PROFILE_RE.fullmatch(value))) if _name == KEYS[0]
        else _origin if _name == KEYS[1] else (lambda value: value == "" or valid_key(value)))
config_override.register_publication_policy("dnsman_shared_validation", KEYS, _publication_policy)
