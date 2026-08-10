"""Protected installation settings used by the System Setup control plane.

These values deliberately do not share the generic ``Setting`` write path.
They influence platform ownership or global routing and may only be changed by
the dedicated service after proving the actor is a live, literal User
superuser.
"""

import json
import re
import uuid
from urllib.parse import urlsplit, urlunsplit

from django.db import transaction
from django.utils import timezone

from mojo import errors as merrors
from mojo.helpers.settings import settings


BASE_URL = "BASE_URL"
INSTALLATION_UUID = "MOJO_INSTALLATION_UUID"
INSTALLATION_SLUG = "MOJO_INSTALLATION_SLUG"
MONITORING_TOPICS = "AWS_CLOUDWATCH_ALARM_TOPIC_ARNS"
EXPECTED_EDGE_TOPOLOGY = "EDGE_EXPECTED_TOPOLOGY"

_PROTECTED = {}
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,62}$")


def register_protected_setting(key, validator=None):
    """Register a global Setting key that generic writes must refuse."""
    if not isinstance(key, str) or not key.strip():
        raise ValueError("protected setting key must be a non-empty string")
    _PROTECTED[key] = validator


def protected_keys():
    return tuple(sorted(_PROTECTED))


def is_protected_setting(key):
    return key in _PROTECTED


def require_system_admin(actor):
    """Return a fresh User row or fail closed for non-person/admin actors."""
    from mojo.apps.account.models import User

    if not isinstance(actor, User) or not actor.pk:
        raise merrors.PermissionDeniedException("System Setup requires a superuser")
    current = User.objects.filter(
        pk=actor.pk, is_active=True, is_superuser=True).first()
    if current is None:
        raise merrors.PermissionDeniedException("System Setup requires an active superuser")
    return current


def validate_base_url(value):
    """Return one canonical public HTTPS origin suitable for BASE_URL."""
    if not isinstance(value, str):
        raise ValueError("BASE_URL must be an HTTPS origin")
    value = value.strip()
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("BASE_URL contains an invalid hostname or port")
    if parsed.scheme.lower() != "https" or not parsed.netloc:
        raise ValueError("BASE_URL must use https and include a hostname")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("BASE_URL cannot contain credentials, a query, or a fragment")
    if parsed.path not in ("", "/"):
        raise ValueError("BASE_URL must not include a path")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host or host == "localhost" or host.endswith(".localhost"):
        raise ValueError("BASE_URL must use a public hostname")
    try:
        import ipaddress
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("BASE_URL cannot use a private or local IP address")
    if address is None:
        try:
            ascii_host = host.encode("idna").decode("ascii")
        except UnicodeError:
            raise ValueError("BASE_URL contains an invalid hostname")
        labels = ascii_host.split(".")
        if len(labels) < 2 or any(
                not label or len(label) > 63 or
                not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label)
                for label in labels):
            raise ValueError("BASE_URL must use a valid public hostname")
        host = ascii_host
    display_host = f"[{host}]" if ":" in host else host
    netloc = display_host if port in (None, 443) else f"{display_host}:{port}"
    return urlunsplit(("https", netloc, "", "", ""))


def validate_browser_origin(value):
    """Return a canonical HTTP(S) browser origin, including local bootstrap."""
    if not isinstance(value, str):
        raise ValueError("Origin header is required")
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError:
        raise ValueError("Origin contains an invalid hostname or port")
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        raise ValueError("Origin must be an HTTP(S) origin")
    if (parsed.username or parsed.password or parsed.query or parsed.fragment or
            parsed.path not in ("", "/")):
        raise ValueError("Origin must not contain credentials, a path, query, or fragment")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise ValueError("Origin must include a hostname")
    try:
        import ipaddress
        ipaddress.ip_address(host)
    except ValueError:
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            raise ValueError("Origin contains an invalid hostname")
        if any(
                not label or len(label) > 63 or
                not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label)
                for label in host.split(".")):
            raise ValueError("Origin contains an invalid hostname")
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    display_host = f"[{host}]" if ":" in host else host
    netloc = display_host if port in (None, default_port) else f"{display_host}:{port}"
    return urlunsplit((parsed.scheme.lower(), netloc, "", "", ""))


def _validate_string(key, value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def _validate_slug(key, value):
    value = _validate_string(key, value).lower()
    if not _SLUG_RE.fullmatch(value):
        raise ValueError(f"{key} must be 3-63 lowercase letters, digits, or hyphens")
    return value


def _validate_list(key, value):
    if not isinstance(value, list) or not all(
            isinstance(item, str) and item.strip() for item in value):
        raise ValueError(f"{key} must be a JSON list of non-empty strings")
    return [item.strip() for item in value]


def _validate_topology(key, value):
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be a JSON object")
    nodes = value.get("nodes", [])
    pools = value.get("pools", [])
    if not isinstance(nodes, list) or not isinstance(pools, list):
        raise ValueError(f"{key} nodes and pools must be lists")
    clean_nodes = _validate_list(key, nodes) if nodes else []
    clean_pools = _validate_list(key, pools) if pools else []
    return {"nodes": sorted(set(clean_nodes)), "pools": sorted(set(clean_pools))}


def _normalize_value(key, value):
    validator = _PROTECTED.get(key)
    if validator is None and key not in _PROTECTED:
        raise merrors.ValueException(f"{key} is not an allowlisted system setting")
    if validator is None:
        return value
    try:
        return validator(key, value)
    except ValueError as exc:
        raise merrors.ValueException(str(exc))


def get_value(key, default=None):
    from mojo.apps.account.models import Setting
    value, found = Setting.get_from_db(key)
    if not found:
        return default
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return value
        if isinstance(parsed, (dict, list, bool, int, float, type(None))):
            return parsed
    return value


def set_value(actor, key, value):
    """Persist one protected global setting through the only allowed writer."""
    actor = require_system_admin(actor)
    if key in (INSTALLATION_UUID, INSTALLATION_SLUG):
        raise merrors.PermissionDeniedException(
            "Installation identity can only be created by its initializer")
    normalized = _normalize_value(key, value)
    from mojo.apps.account.models import Setting

    with transaction.atomic():
        # A protected row may not exist yet, so lock one stable row shared by
        # every administrator before looking it up. PostgreSQL does not treat
        # NULLs as equal in Setting's legacy unique_together constraint.
        from mojo.apps.account.models import User
        User.objects.select_for_update().order_by("pk").first()
        row = Setting.objects.select_for_update().filter(key=key, group=None).first()
        if row is None:
            row = Setting(key=key, group=None, is_secret=False)
            row.set_value(normalized)
            # Protected rows intentionally bypass Setting.save(): that method
            # refuses every protected write. This bulk primitive is private to
            # the dedicated allowlisted service and exposes no model flag a
            # generic caller can forge.
            Setting.objects.bulk_create([row])
        else:
            serialized = normalized if isinstance(normalized, str) else json.dumps(normalized)
            Setting.objects.filter(pk=row.pk).update(
                value=serialized, is_secret=False, mojo_secrets=None,
                modified=timezone.now())
            row.value = serialized
            row.is_secret = False
            row.mojo_secrets = None
        transaction.on_commit(row.push_to_cache)
    return normalized


def _validate_identity_pair(current_uuid, current_slug):
    if bool(current_uuid) != bool(current_slug):
        raise merrors.ValueException("Stored installation identity is incomplete")
    if not current_uuid:
        return None
    try:
        identity = str(uuid.UUID(str(current_uuid)))
    except (ValueError, TypeError, AttributeError):
        raise merrors.ValueException("Stored installation UUID is invalid")
    try:
        slug = _validate_slug(INSTALLATION_SLUG, current_slug)
    except ValueError as exc:
        raise merrors.ValueException(str(exc))
    return {"uuid": identity, "slug": slug}


def _initialize_identity(actor):
    """Create the immutable identity under the shared protected-setting lock."""
    actor = require_system_admin(actor)
    with transaction.atomic():
        from mojo.apps.account.models import User
        from mojo.apps.account.models import Setting
        User.objects.select_for_update().order_by("pk").first()
        current_uuid = get_value(INSTALLATION_UUID)
        current_slug = get_value(INSTALLATION_SLUG)
        existing = _validate_identity_pair(current_uuid, current_slug)
        if existing:
            return existing

        identity = str(uuid.uuid4())
        configured = str(settings.get_static("AWS_MONITORING_NAME", "")).strip().lower()
        slug = configured or f"mojo-{identity.replace('-', '')[:12]}"
        slug = _validate_slug(INSTALLATION_SLUG, slug)
        rows = []
        for key, value in ((INSTALLATION_UUID, identity), (INSTALLATION_SLUG, slug)):
            row = Setting(key=key, group=None, is_secret=False)
            row.set_value(value)
            rows.append(row)
        Setting.objects.bulk_create(rows)
        for row in rows:
            transaction.on_commit(row.push_to_cache)
        return _validate_identity_pair(identity, slug)


def installation_identity(actor):
    """Validate or create the immutable ownership identity."""
    actor = require_system_admin(actor)
    current = _validate_identity_pair(
        get_value(INSTALLATION_UUID), get_value(INSTALLATION_SLUG))
    return current or _initialize_identity(actor)


def _register_defaults():
    register_protected_setting(BASE_URL, lambda key, value: validate_base_url(value))
    register_protected_setting(INSTALLATION_UUID, _validate_string)
    register_protected_setting(INSTALLATION_SLUG, _validate_slug)
    register_protected_setting(MONITORING_TOPICS, _validate_list)
    register_protected_setting(EXPECTED_EDGE_TOPOLOGY, _validate_topology)


_register_defaults()
