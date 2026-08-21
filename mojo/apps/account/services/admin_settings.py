"""Curated, server-owned settings catalog for the built-in Admin.

The registry is deliberately import-light: descriptors contain data, not
model classes, and optional applications register their own rows from
``AppConfig.ready``.  The existing :class:`account.Setting` table remains the
only database-backed store.
"""

from dataclasses import asdict, dataclass
import json
import ipaddress
import re
from urllib.parse import urlsplit, urlunsplit

from django.db import transaction
from django.utils import timezone

from mojo import errors as merrors
from mojo.helpers.settings import settings


SCHEMA_VERSION = 1
MAX_VALUE_LENGTH = 2048
SECTION_ORDER = (
    "General", "Sign-in & registration", "Users", "Email",
    "Domains & DNS", "Edge & Web Apps", "Security & operations",
)
MUTABLE_KEYS = frozenset({
    "ALLOW_EMAIL_CHANGE", "ALLOW_PHONE_CHANGE", "ALLOW_USERNAME_CHANGE",
    "ALLOW_SELF_DEACTIVATION", "WEBAPP_BASE_URL",
})
FLEET_PROVIDER_KEYS = frozenset({
    "GEOIP_PRIMARY_PROVIDER", "GEOIP_FALLBACK_PROVIDER",
    "GEOIP_ADDITIONAL_PROVIDERS", "GEOIP_MOJO_PROVIDER_URL",
    "GEOIP_MOJO_SYNC_ENABLED", "GEOIP_API_KEY_MOJO",
    "ADMIN_PROVIDER_SETUP_REVISION", "ADMIN_PROVIDER_VERIFY_STATE",
})
# The Assistant's own keys.  The writable four are owned by
# ``services/assistant_setup`` and reached only through the owner-tier endpoint;
# ``LLM_HANDLER_API_KEY`` joins them read-only because a global database row
# would outrank the deployment file (``helpers/settings/helper.py``) and make a
# descriptor labelled "Deployment settings" a lie.
ASSISTANT_WRITABLE_KEYS = frozenset({
    "LLM_ADMIN_ENABLED", "LLM_ADMIN_API_KEY", "LLM_ADMIN_MODEL",
    "LLM_ADMIN_VERIFY_STATE",
})
ASSISTANT_KEYS = ASSISTANT_WRITABLE_KEYS | {"LLM_HANDLER_API_KEY"}
# Keys whose one dedicated writer may pass ``_protected_writer=<key>`` through
# ``Setting.save``.  Naming the key twice is the point: a writer proves it owns
# exactly the row it is saving, so a shared helper cannot smuggle a different
# protected key past the guard.
PROTECTED_WRITER_KEYS = frozenset({"GEOIP_API_KEY_MOJO"}) | ASSISTANT_WRITABLE_KEYS
NON_PUBLIC_HOST_SUFFIXES = frozenset({
    "alt", "arpa", "corp", "example", "home", "internal", "invalid", "lan", "local",
    "localdomain", "localhost", "onion", "test",
})
NON_PUBLIC_HOSTS = frozenset({
    "example", "example.com", "example.net", "example.org",
})


@dataclass(frozen=True)
class Descriptor:
    key: str
    label: str
    section: str
    description: str
    value_type: str
    default: object = None
    resolver: str = "static"
    raw_semantics: str = "No database override"
    effective_semantics: str = "Deployment value"
    sensitivity: str = "public"
    scope: str = "global"
    writable: str = "none"
    owner: str = "deployment"
    change_behavior: str = "deploy"
    constraints: str = ""
    owner_route: str = ""
    storage: str = "deployment"
    # The browser cannot derive either of these from a value.  ``unit`` names
    # what an integer counts; ``unset_meaning`` is the one plain sentence that
    # says what happens while the value is absent — only where absence is a
    # legitimate, understood state, never as reassurance about a missing secret.
    unit: str = ""
    unset_meaning: str = ""
    # Protected keys are database-owned, but a few are ALSO read through the
    # deployment-file plane, so they can be live with no database row at all.
    # Only those descriptors opt in: for the rest a file value is never
    # effective, and reporting one would be the same lie inverted.
    deployment_fallback: bool = False


_REGISTRY = {}
_NO_CACHE = object()


def register_descriptor(descriptor):
    """Register one immutable descriptor; identical repeats are idempotent."""
    if not isinstance(descriptor, Descriptor) or not descriptor.key.strip():
        raise ValueError("Admin setting descriptor is invalid")
    existing = _REGISTRY.get(descriptor.key)
    if existing is not None and existing != descriptor:
        raise RuntimeError(f"conflicting Admin setting descriptor: {descriptor.key}")
    _REGISTRY[descriptor.key] = descriptor
    return descriptor


def descriptors():
    return tuple(sorted(_REGISTRY.values(), key=lambda row: (row.section, row.label)))


def _section_names(descriptor_rows):
    present = {row.section for row in descriptor_rows}
    return [section for section in SECTION_ORDER if section in present]


def is_catalog_protected(key):
    """Return whether alternate *global* writers must refuse this key."""
    return (key in MUTABLE_KEYS or key in FLEET_PROVIDER_KEYS or
            key in ASSISTANT_KEYS)


def _bounded(value):
    if isinstance(value, str):
        return value[:MAX_VALUE_LENGTH]
    if isinstance(value, (bool, int, float, type(None))):
        return value
    if isinstance(value, list):
        return [_bounded(item) for item in value[:100]]
    if isinstance(value, dict):
        return {str(key)[:120]: _bounded(item)
                for key, item in list(value.items())[:100]}
    return str(value)[:MAX_VALUE_LENGTH]


def _global_rows(key, *, lock=False):
    from mojo.apps.account.models import Setting
    rows = Setting.objects.filter(key=key, group=None).order_by("pk")
    if lock:
        rows = rows.select_for_update()
    return list(rows)


def _parse_row(row):
    if row.is_secret:
        return {"configured": True}
    value = row.get_value()
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def _static_state(descriptor, rows):
    raw_value = settings.get_static(descriptor.key, descriptor.default)
    source = "deployment" if raw_value != descriptor.default else "default"
    value = raw_value
    if descriptor.sensitivity == "configured_only":
        value = {"configured": bool(raw_value)}
    return value, source, bool(rows)


def _protected_state(descriptor, rows):
    from mojo.apps.account.services import system_settings
    value = system_settings.get_value(descriptor.key, descriptor.default)
    source = "database" if rows else "default"
    if not rows and descriptor.deployment_fallback:
        deployed = settings.get_static(descriptor.key, descriptor.default)
        if deployed != descriptor.default:
            value, source = deployed, "deployment"
    if descriptor.sensitivity == "configured_only":
        value = {"configured": bool(value)}
    return value, source, False


def _dynamic_state(descriptor, rows, cached=_NO_CACHE):
    if rows:
        if cached is not _NO_CACHE:
            value = cached.decode("utf-8") if isinstance(cached, bytes) else cached
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except (TypeError, ValueError):
                    pass
            return value, "database_cache", False
        return _parse_row(rows[0]), "database", False
    value = settings.get_static(descriptor.key, descriptor.default)
    return value, "deployment" if value != descriptor.default else "default", False


def _auth_state(descriptor, rows):
    from mojo.apps.account.services import auth_config
    value = auth_config.public_auth_config(auth_config.resolve_auth_config())
    return value, "database+deployment+defaults" if rows else "deployment+defaults", False


def _posture_state(descriptor, rows):
    keys = (
        "SECURE_SSL_REDIRECT", "SESSION_COOKIE_SECURE", "CSRF_COOKIE_SECURE",
        "SECURE_HSTS_SECONDS",
    )
    value = {key: bool(settings.get_static(key, 0)) for key in keys}
    return value, "computed", False


def _email_posture_state(descriptor, rows):
    # One source of truth: the admin email service owns this computation and
    # the Dashboard/summary reuse it. The dict must stay byte-identical.
    from mojo.apps.aws.services.email_admin import email_posture
    return email_posture(), "computed", False


def _resolve(descriptor, rows=None, cached=_NO_CACHE):
    rows = _global_rows(descriptor.key) if rows is None else rows
    duplicate = len(rows) > 1
    # A duplicate is ambiguous regardless of whatever Redis last cached.  A
    # legacy secret row is configuration metadata only: never decrypt it and
    # never let a cached plaintext value become the catalog's effective value.
    if duplicate:
        return None, "duplicate_override", False, True
    if rows and rows[0].is_secret:
        return {"configured": True}, "secret_override", \
            descriptor.resolver == "static", False
    resolver = {
        "static": _static_state,
        "protected": _protected_state,
        "dynamic": _dynamic_state,
        "auth": _auth_state,
        "posture": _posture_state,
        "email_posture": _email_posture_state,
    }.get(descriptor.resolver)
    if resolver is None:
        return None, "invalid", bool(rows), duplicate
    try:
        value, source, ignored = (
            resolver(descriptor, rows, cached)
            if descriptor.resolver == "dynamic" else resolver(descriptor, rows))
    except Exception:
        return None, "invalid", bool(rows), duplicate
    if descriptor.sensitivity == "configured_only" and not (
            isinstance(value, dict) and set(value) == {"configured"}):
        value = {"configured": bool(value)}
    return _bounded(value), source, ignored, duplicate


def catalog(*, capabilities=None):
    capabilities = capabilities or {}
    descriptor_rows = descriptors()
    from mojo.apps.account.models import Setting
    rows_by_key = {descriptor.key: [] for descriptor in descriptor_rows}
    for row in Setting.objects.filter(
            key__in=rows_by_key, group=None).order_by("key", "pk"):
        rows_by_key[row.key].append(row)
    cached = {}
    dynamic_keys = [
        descriptor.key for descriptor in descriptor_rows
        if descriptor.resolver == "dynamic"
        and len(rows_by_key[descriptor.key]) <= 1
        and not any(row.is_secret for row in rows_by_key[descriptor.key])
    ]
    redis = Setting._redis()
    if redis and dynamic_keys:
        try:
            cached = {key: value for key, value in zip(
                dynamic_keys, redis.hmget(Setting._redis_key(), dynamic_keys))
                      if value is not None}
        except Exception:
            cached = {}
    entries = []
    for descriptor in descriptor_rows:
        if descriptor.writable == "owner" and not capabilities.get("owner_display"):
            continue
        rows = rows_by_key[descriptor.key]
        value, source, ignored, duplicate = _resolve(
            descriptor, rows, cached.get(descriptor.key, _NO_CACHE))
        row = asdict(descriptor)
        row.update({
            "effective_value": value,
            "source": "duplicate_override" if duplicate else source,
            "duplicate_override": duplicate,
            "ignored_database_override": bool(ignored),
            "can_write": bool(
                descriptor.writable == "catalog" and
                capabilities.get("catalog_write") and not duplicate),
            "can_clear": bool(
                descriptor.writable == "catalog" and
                capabilities.get("catalog_write") and bool(rows)),
            "can_owner_edit": bool(
                descriptor.writable == "owner" and capabilities.get("owner_edit")),
        })
        entries.append(row)
    base_url_rows = rows_by_key.get("BASE_URL", [])
    setup_incomplete = (
        len(base_url_rows) != 1 or base_url_rows[0].is_secret or
        not bool(_parse_row(base_url_rows[0])))
    return {
        "schema_version": SCHEMA_VERSION,
        "capabilities": {
            "catalog_write": bool(capabilities.get("catalog_write")),
            "owner_display": bool(capabilities.get("owner_display")),
            "owner_edit": bool(capabilities.get("owner_edit")),
        },
        "setup_incomplete": setup_incomplete,
        # Optional applications own their descriptors, so their category must
        # disappear with them rather than advertising an empty destination.
        "sections": _section_names(descriptor_rows),
        "entries": entries,
    }


def normalize_webapp_origin(value):
    if not isinstance(value, str):
        raise merrors.ValueException("WEBAPP_BASE_URL must be a public HTTPS origin")
    raw = value.strip()
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        raise merrors.ValueException("WEBAPP_BASE_URL must be a public HTTPS origin") from None
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or
            parsed.password or parsed.path not in ("", "/") or parsed.query or
            parsed.fragment or port not in (None, 443)):
        raise merrors.ValueException("WEBAPP_BASE_URL must be a public HTTPS origin")
    try:
        hostname = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise merrors.ValueException("WEBAPP_BASE_URL must be a public HTTPS origin") from None
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    labels = hostname.split(".")
    valid_labels = all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                       for label in labels)
    reserved = (
        hostname in NON_PUBLIC_HOSTS or
        any(hostname == suffix or hostname.endswith(f".{suffix}")
            for suffix in NON_PUBLIC_HOST_SUFFIXES) or
        any(hostname.endswith(f".{suffix}") for suffix in (
            "example.com", "example.net", "example.org"))
    )
    # Browser and libc URL parsers accept abbreviated/octal/hex IPv4 spellings
    # such as 127.1, 0177.0.0.1, and 0x7f.0.0.1.  Requiring a real public DNS
    # suffix keeps those forms from bypassing ipaddress's canonical parser.
    public_suffix = bool(re.fullmatch(
        r"(?:[a-z]{2,63}|xn--[a-z0-9-]{1,59})", labels[-1]))
    if (address is not None or reserved or len(labels) < 2 or
            not valid_labels or not public_suffix):
        raise merrors.ValueException("WEBAPP_BASE_URL must be a public HTTPS origin")
    return urlunsplit(("https", hostname, "", "", ""))


def _normalize(key, value):
    if key.startswith("ALLOW_"):
        if not isinstance(value, bool):
            raise merrors.ValueException(f"{key} must be true or false")
        return value
    if key == "WEBAPP_BASE_URL":
        return normalize_webapp_origin(value)
    raise merrors.ValueException("Setting is not writable from the catalog")


def require_catalog_writer(actor, *, lock=False):
    from mojo.apps.account.models import User
    if not isinstance(actor, User) or not actor.pk:
        raise merrors.PermissionDeniedException("A literal active User is required")
    rows = User.objects.filter(pk=actor.pk, is_active=True)
    if lock:
        rows = rows.select_for_update()
    current = rows.first()
    if current is None:
        raise merrors.PermissionDeniedException("The user is no longer active")
    permissions = current.permissions if isinstance(current.permissions, dict) else {}
    if not (current.is_superuser or permissions.get("manage_settings") is True or
            permissions.get("admin") is True):
        raise merrors.PermissionDeniedException("manage_settings is required")
    return current


def _cache_delete(key):
    from mojo.apps.account.models import Setting
    redis = Setting._redis()
    if redis:
        redis.hdel(Setting._redis_key(), key)


def _audit(actor, key, action, source="admin_settings"):
    actor_id = actor.pk
    def write():
        from mojo.apps.incident import report_event_suppressed
        report_event_suppressed(
            f"Admin setting actor={actor_id} key={key} action={action} source={source}",
            title="Admin setting changed", category="admin_settings", level=5,
            key=f"admin-settings:{actor_id}:{key}:{action}")
    transaction.on_commit(write)


def set_override(actor, key, value):
    actor = require_catalog_writer(actor)
    if key not in MUTABLE_KEYS:
        raise merrors.ValueException("Setting is not writable from the catalog")
    value = _normalize(key, value)
    from mojo.apps.account.models import Setting, User
    with transaction.atomic():
        # One installation-wide lock also serializes the absent-row case;
        # locking the actor would not contend when two different admins write.
        User.objects.select_for_update().order_by("pk").first()
        actor = require_catalog_writer(actor, lock=True)
        rows = _global_rows(key, lock=True)
        if len(rows) > 1:
            raise merrors.ValueException(
                "Conflicting global overrides must be cleared before setting a value")
        serialized = value if isinstance(value, str) else json.dumps(value)
        if rows:
            Setting.objects.filter(pk=rows[0].pk).update(
                value=serialized, is_secret=False, mojo_secrets=None,
                modified=timezone.now())
            row = rows[0]
            row.value = serialized
            row.is_secret = False
            row.mojo_secrets = None
        else:
            row = Setting(key=key, group=None, is_secret=False, value=serialized)
            Setting.objects.bulk_create([row])
        transaction.on_commit(row.push_to_cache)
        _audit(actor, key, "set")
    return value


def clear_override(actor, key):
    actor = require_catalog_writer(actor)
    if key not in MUTABLE_KEYS:
        raise merrors.ValueException("Setting is not writable from the catalog")
    from mojo.apps.account.models import User
    with transaction.atomic():
        User.objects.select_for_update().order_by("pk").first()
        actor = require_catalog_writer(actor, lock=True)
        rows = _global_rows(key, lock=True)
        if rows:
            type(rows[0]).objects.filter(pk__in=[row.pk for row in rows]).delete()
        transaction.on_commit(lambda: _cache_delete(key))
        _audit(actor, key, "clear")
    return len(rows)


def register_core_descriptors():
    core = (
        Descriptor("BASE_URL", "Public API address", "General",
                   "Canonical public address used by this installation.", "origin",
                   resolver="protected", raw_semantics="Protected database value",
                   effective_semantics="Durable Setup-owned value", writable="owner",
                   owner="System Setup", change_behavior="setup",
                   owner_route="setup?focus=django.base_url"),
        Descriptor("MOJO_INSTALLATION_UUID", "Installation UUID", "General",
                   "Frozen installation identity.", "string", resolver="protected",
                   writable="none", owner="System Setup", change_behavior="immutable"),
        Descriptor("MOJO_INSTALLATION_SLUG", "Installation slug", "General",
                   "Frozen human-readable installation identity.", "string",
                   resolver="protected", writable="none", owner="System Setup",
                   change_behavior="immutable"),
        Descriptor("AUTH_CONFIG", "Brand & authentication", "Sign-in & registration",
                   "Hosted authentication appearance and safe sign-in methods.", "object",
                   resolver="auth", raw_semantics="Protected AUTH_CONFIG override",
                   effective_semantics="Defaults merged with the safe override",
                   writable="owner", owner="Settings authentication editor",
                   change_behavior="typed_owner"),
        Descriptor("ALLOW_EMAIL_CHANGE", "Allow email changes", "Users",
                   "Let users change their own email address.", "boolean", True,
                   resolver="dynamic", writable="catalog", owner="Settings",
                   change_behavior="immediate"),
        Descriptor("ALLOW_PHONE_CHANGE", "Allow phone changes", "Users",
                   "Let users change their own phone number.", "boolean", True,
                   resolver="dynamic", writable="catalog", owner="Settings",
                   change_behavior="immediate"),
        Descriptor("ALLOW_USERNAME_CHANGE", "Allow username changes", "Users",
                   "Let users change their own username.", "boolean", True,
                   resolver="dynamic", writable="catalog", owner="Settings",
                   change_behavior="immediate"),
        Descriptor("ALLOW_SELF_DEACTIVATION", "Allow self-deactivation", "Users",
                   "Let users deactivate their own account.", "boolean", True,
                   resolver="dynamic", writable="catalog", owner="Settings",
                   change_behavior="immediate"),
        Descriptor("INCIDENT_EMAIL_FROM", "Incident email sender", "Email",
                   "Mailbox used for incident notification email.", "configured",
                   resolver="static", sensitivity="configured_only", writable="owner",
                   owner="System Setup", change_behavior="setup", owner_route="setup?focus=aws_email",
                   unset_meaning="incident email is not sent"),
        Descriptor("AWS_MONITORING_NAME", "Monitoring identity", "Security & operations",
                   "Stable deployment name used for monitoring resources.", "configured",
                   resolver="static", sensitivity="configured_only", writable="owner", owner="System Setup",
                   change_behavior="setup", owner_route="setup?focus=aws_monitoring",
                   unset_meaning="monitoring resources are not named for this deployment"),
        Descriptor("AWS_CLOUDWATCH_ALARM_TOPIC_ARNS", "Monitoring delivery", "Security & operations",
                   "CloudWatch alarm delivery configuration.", "configured",
                   resolver="protected", sensitivity="configured_only", writable="owner",
                   owner="System Setup", change_behavior="setup", owner_route="setup?focus=aws_monitoring",
                   unset_meaning="CloudWatch alarms are not delivered anywhere",
                   deployment_fallback=True),
        Descriptor("SECURE_POSTURE", "Django HTTPS posture", "Security & operations",
                   "Secure redirect, cookie, and HSTS deployment controls.", "object",
                   resolver="posture", writable="none", owner="Deployment settings",
                   change_behavior="deploy"),
        Descriptor("KMS_KEY_ID", "KMS encryption", "Security & operations",
                   "Encryption key configuration state.", "configured", resolver="static",
                   sensitivity="configured_only", writable="none", owner="Deployment settings",
                   change_behavior="deploy"),
        Descriptor("SYSTEM_SETUP_LOCAL_API_URL", "Local API probe", "Security & operations",
                   "Optional loopback target for Setup probes.", "configured", resolver="static",
                   sensitivity="configured_only", writable="none", owner="Deployment settings",
                   change_behavior="deploy",
                   unset_meaning="Setup probes the public address instead"),
        Descriptor("GEOIP_PRIMARY_PROVIDER", "Primary GeoIP provider", "Security & operations",
                   "Provider queried first for IP intelligence.", "string", "mojo",
                   resolver="static", writable="fleet_config", owner="Mojo providers",
                   change_behavior="restart", constraints="Provider identifier",
                   storage="fleet_config"),
        Descriptor("GEOIP_FALLBACK_PROVIDER", "Fallback GeoIP provider", "Security & operations",
                   "Provider used when the primary cannot answer.", "string", "ipinfo",
                   resolver="static", writable="fleet_config", owner="Mojo providers",
                   change_behavior="restart", constraints="Provider identifier",
                   storage="fleet_config"),
        Descriptor("GEOIP_ADDITIONAL_PROVIDERS", "Additional GeoIP providers", "Security & operations",
                   "Extra providers available after primary and fallback.", "list", [],
                   resolver="static", writable="fleet_config", owner="Mojo providers",
                   change_behavior="restart", constraints="Unique provider identifiers",
                   storage="fleet_config"),
        Descriptor("GEOIP_MOJO_PROVIDER_URL", "Mojo GeoIP URL", "Security & operations",
                   "Canonical HTTPS origin of the upstream django-mojo provider.", "origin",
                   "https://api.mojoverify.com", resolver="static", writable="fleet_config",
                   owner="Mojo providers", change_behavior="restart",
                   constraints="One HTTPS origin", storage="fleet_config"),
        Descriptor("GEOIP_MOJO_SYNC_ENABLED", "Mojo GeoIP sync", "Security & operations",
                   "Push observed abuse signals back to the upstream provider.", "boolean", False,
                   resolver="static", writable="fleet_config", owner="Mojo providers",
                   change_behavior="restart", storage="fleet_config"),
        Descriptor("GEOIP_API_KEY_MOJO", "Mojo GeoIP API key", "Security & operations",
                   "Encrypted credential used for Mojo GeoIP federation.", "configured",
                   resolver="dynamic", sensitivity="configured_only", writable="provider_setup",
                   owner="Mojo providers", change_behavior="immediate", storage="database"),
    )
    for descriptor in core:
        register_descriptor(descriptor)
