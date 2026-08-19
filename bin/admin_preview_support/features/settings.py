from copy import deepcopy
import time


NAME = "settings"


def describe(capabilities):
    values = {key: capabilities[key] for key in (
        "settings", "catalog_write", "settings_owner_display", "settings_owner_edit")}
    return {"id": NAME, "enabled": values["settings"], "capabilities": {
        "view": values["settings"], "catalog_write": values["catalog_write"],
        "owner_display": values["settings_owner_display"],
        "owner_edit": values["settings_owner_edit"],
    }}


def _entry(key, label, section, value, *, source="database", value_type="boolean",
           writable="catalog", owner="Settings", behavior="immediate", route="",
           unit="", unset_meaning="", sensitivity="public"):
    return {
        "key": key, "label": label, "section": section,
        "description": f"Deterministic preview for {label.lower()}.",
        "value_type": value_type, "default": True if value_type == "boolean" else None,
        "resolver": "dynamic", "raw_semantics": "Global database override",
        "effective_semantics": "Database, deployment, then default",
        "sensitivity": sensitivity, "scope": "global", "writable": writable,
        "owner": owner, "change_behavior": behavior, "constraints": "",
        "owner_route": route, "unit": unit, "unset_meaning": unset_meaning,
        "effective_value": value, "source": source,
        "duplicate_override": False, "ignored_database_override": False,
        "can_write": writable == "catalog", "can_clear": writable == "catalog" and source == "database",
        "can_owner_edit": writable == "owner" and behavior == "typed_owner",
    }


# A four-character hint is exactly what the browser is allowed to show; a
# fixture that emitted more would let a leak ship looking correct.
GEOIP_HINT = "9f2c"


def _provider_setup(handler):
    unset = handler.settings_state == "unset"
    failed = handler.settings_state == "provider_failed"
    return {
        "available": True,
        "bucket_configured": True,
        "restart_configured": True,
        "object_key": "config/prod/django.override.json",
        "delegated_keys": [
            "GEOIP_ADDITIONAL_PROVIDERS", "GEOIP_FALLBACK_PROVIDER",
            "GEOIP_MOJO_PROVIDER_URL", "GEOIP_MOJO_SYNC_ENABLED",
            "GEOIP_PRIMARY_PROVIDER",
        ],
        "fleet_fields": [
            {"key": "GEOIP_PRIMARY_PROVIDER", "label": "Primary GeoIP provider",
             "description": "Provider queried first for IP intelligence.",
             "value_type": "string", "constraints": "Provider identifier"},
            {"key": "GEOIP_FALLBACK_PROVIDER", "label": "Fallback GeoIP provider",
             "description": "Provider used when the primary cannot answer.",
             "value_type": "string", "constraints": "Provider identifier"},
            {"key": "GEOIP_ADDITIONAL_PROVIDERS", "label": "Additional GeoIP providers",
             "description": "Extra providers available after primary and fallback.",
             "value_type": "list", "constraints": "Unique provider identifiers"},
            {"key": "GEOIP_MOJO_PROVIDER_URL", "label": "Mojo GeoIP URL",
             "description": "Canonical HTTPS origin of the upstream provider.",
             "value_type": "origin", "constraints": "One HTTPS origin"},
            {"key": "GEOIP_MOJO_SYNC_ENABLED", "label": "Mojo GeoIP sync",
             "description": "Push observed abuse signals back upstream.",
             "value_type": "boolean", "constraints": ""},
        ],
        "loaded_revision": "0123456789abcdef0123456789abcdef",
        "published_revision": "0123456789abcdef0123456789abcdef",
        "configuration_revision": "fedcba9876543210fedcba9876543210",
        "published_version": "s3-version-id",
        "pending_restart": False,
        "remote_error": None,
        "geoip": {
            "GEOIP_PRIMARY_PROVIDER": "mojo",
            "GEOIP_FALLBACK_PROVIDER": "ipinfo",
            "GEOIP_ADDITIONAL_PROVIDERS": [],
            "GEOIP_MOJO_PROVIDER_URL": "https://api.mojoverify.com",
            "GEOIP_MOJO_SYNC_ENABLED": False,
            "GEOIP_API_KEY_MOJO_CONFIGURED": not unset,
            "GEOIP_API_KEY_MOJO_HINT": "" if unset else GEOIP_HINT,
        },
        "geoip_providers": ["ip-api", "ipinfo", "ipstack", "maxmind", "mojo"],
        "sms": {
            "configured": not unset,
            "remote_url": "" if unset else "https://sms.example.com",
            "api_key_configured": not unset,
            "api_key_hint": "" if unset else "4d81",
            "test_mode": False,
        },
        "verify_state": {} if unset else {
            "geoip": ({"ok": False, "code": "http_401",
                       "message": "api.mojoverify.com rejected the API key",
                       "at": "2026-08-18T09:14:00+00:00"} if failed else
                      {"ok": True, "code": None, "message": "Connection verified",
                       "at": "2026-08-18T09:14:00+00:00"}),
            "sms": {"ok": True, "code": None, "message": "Connection verified",
                    "at": "2026-08-18T09:14:00+00:00"},
        },
    }


def reset(handler, fixtures, **options):
    handler.settings_state = options.get("settings_state", "normal")
    handler.setting_entries = [
        _entry("BASE_URL", "Public API address", "General", "https://api.nativemojo.com", value_type="origin", writable="owner", owner="System Setup", behavior="setup", route="setup?focus=django.base_url"),
        _entry("MOJO_INSTALLATION_UUID", "Installation UUID", "General", "11111111-1111-4111-8111-111111111111", value_type="string", writable="none", owner="System Setup", behavior="immutable"),
        _entry("AUTH_CONFIG", "Brand & authentication", "Sign-in & registration", {"theme": {"app_title": "DJANGO MOJO", "accent_color": "#6384ff"}, "login": {"methods": ["password", "passkey", "github"]}, "registration": {"enabled": True, "methods": ["password", "github"], "passkey_prompt": "optional"}}, source="database+deployment+defaults", value_type="object", writable="owner", owner="Settings authentication editor", behavior="typed_owner"),
        _entry("ALLOW_EMAIL_CHANGE", "Allow email changes", "Users", True),
        _entry("ALLOW_PHONE_CHANGE", "Allow phone changes", "Users", False),
        _entry("DNSMAN_ACME_CONTACT_EMAIL", "ACME contact", "Domains & DNS", "ops@example.com", source="deployment", value_type="email", writable="owner", owner="Domains & DNS", behavior="owner_review", route="domains"),
        _entry("EDGE_EXPECTED_TOPOLOGY", "Expected fleet", "Edge & Web Apps", {"nodes": ["edge-a", "edge-b"], "pools": ["public-web"]}, value_type="topology", writable="owner", owner="Settings fleet editor", behavior="typed_owner"),
        _entry("WEBAPP_BASE_URL", "Default WebApp address", "Edge & Web Apps", "https://apps.nativemojo.com", value_type="origin"),
        _entry("SYSTEM_SETUP_LOCAL_API_URL", "Local API probe", "Security & operations", {"configured": True}, source="deployment", value_type="configured", writable="none", owner="Deployment settings", behavior="deploy", sensitivity="configured_only", unset_meaning="Setup probes the public address instead"),
        _entry("DNSMAN_CERT_RENEW_DAYS", "Certificate renewal window", "Domains & DNS", 30, source="default", value_type="integer", writable="owner", owner="Domains & DNS", behavior="owner_review", route="domains", unit="days"),
        _entry("EDGE_FRAMEWORK_VERSION", "Framework version hold", "Edge & Web Apps", None, source="default", value_type="string", writable="owner", owner="Settings platform editor", behavior="typed_owner", unset_meaning="installs the newest published release"),
        _entry("EMAIL_DELIVERY_POSTURE", "Email delivery", "Email", {"default_sender_configured": True, "default_sender_conflict": False, "templates_installed": False, "missing_template_count": 3}, source="computed", value_type="object", writable="owner", owner="System Setup", behavior="setup", route="setup?focus=aws_email"),
        _entry("INCIDENT_EMAIL_FROM", "Incident email sender", "Email", {"configured": True}, source="deployment", value_type="configured", writable="owner", owner="System Setup", behavior="setup", route="setup?focus=aws_email", sensitivity="configured_only", unset_meaning="incident email is not sent"),
        _entry("SECURE_POSTURE", "Django HTTPS posture", "Security & operations", {"SECURE_SSL_REDIRECT": True, "SESSION_COOKIE_SECURE": True, "CSRF_COOKIE_SECURE": True, "SECURE_HSTS_SECONDS": False}, source="computed", value_type="object", writable="none", owner="Deployment settings", behavior="deploy"),
        _entry("GEOIP_API_KEY_MOJO", "Mojo GeoIP API key", "Security & operations", {"configured": True}, source="database", value_type="configured", writable="provider_setup", owner="Mojo providers", behavior="immediate", sensitivity="configured_only"),
    ]
    if handler.settings_state == "unset":
        for key in ("EDGE_FRAMEWORK_VERSION", "INCIDENT_EMAIL_FROM", "GEOIP_API_KEY_MOJO"):
            row = next(item for item in handler.setting_entries if item["key"] == key)
            row.update(source="default", effective_value=(
                {"configured": False} if row["sensitivity"] == "configured_only" else None))
    if handler.settings_state == "duplicate":
        row = next(item for item in handler.setting_entries if item["key"] == "WEBAPP_BASE_URL")
        row.update(source="duplicate_override", duplicate_override=True, can_write=False, can_clear=True)
    if handler.settings_state == "invalid":
        row = next(item for item in handler.setting_entries if item["key"] == "ALLOW_PHONE_CHANGE")
        row.update(source="invalid", effective_value=None)


def get(handler, parsed):
    if parsed.path != "/api/account/admin/settings":
        return None
    if handler.settings_state == "error":
        return 503, {"error": "Deterministic Settings failure"}
    report = {"schema_version": 1, "capabilities": {
                  "catalog_write": True, "owner_display": True, "owner_edit": True},
              "setup_incomplete": False,
              "sections": ["General", "Sign-in & registration", "Users", "Email", "Domains & DNS", "Edge & Web Apps", "Security & operations"],
              "entries": deepcopy(handler.setting_entries)}
    # "restricted" is a non-superuser: no provider payload at all.  It is the
    # only way to see the fallback where the six GeoIP descriptors render as
    # their own read-only rows instead of one collapsed integration.
    if handler.settings_state != "restricted":
        report["provider_setup"] = _provider_setup(handler)
    return 200, report


def post(handler, path, payload):
    if path != "/api/account/admin/settings":
        return None
    if handler.settings_state == "delay":
        time.sleep(1.25)
    if handler.settings_state == "error":
        return 503, {"error": "Deterministic Settings save failure"}
    if handler.settings_state == "fresh":
        return 440, {"error": "Fresh authentication required"}
    action = payload.get("action")
    if action in ("configure_providers", "test_providers"):
        return _provider_post(handler, action, payload)
    key = payload.get("key")
    row = next((item for item in handler.setting_entries if item["key"] == key), None)
    if row is None:
        return 400, {"error": "Setting is not writable from the catalog"}
    if payload.get("action") == "clear":
        row.update(source="default", effective_value=row.get("default"), duplicate_override=False, can_write=True, can_clear=False)
        return 200, {"schema_version": 1, "cleared": True, "key": key, "removed": 2 if handler.settings_state == "duplicate" else 1}
    if payload.get("action") == "set":
        row.update(source="database", effective_value=payload.get("value"), duplicate_override=False, can_write=True, can_clear=True)
        return 200, {"schema_version": 1, "saved": True, "key": key, "effective_value": payload.get("value")}
    return 400, {"error": "action must be set or clear"}


def _provider_post(handler, action, payload):
    """One topic per call, deterministically — including the failure."""
    topic = payload.get("topic")
    if topic not in ("geoip", "sms"):
        return 400, {"error": "Provider setup requires topic geoip or sms"}
    providers = payload.get("providers") or {}
    if set(providers) - {topic, "expected_revision"}:
        return 400, {"error": f"Provider setup accepts only {topic} and expected_revision"}
    failing = handler.settings_state == "provider_failed" and topic == "geoip"
    result = ({"success": False, "code": "http_401",
               "message": "api.mojoverify.com rejected the API key"} if failing
              else {"success": True, "code": None, "message": "Connection verified"})
    if action == "test_providers":
        return 200, {"tested": True, "topic": topic, "results": {topic: result},
                     "success": result["success"]}
    if not result["success"]:
        return 400, {"error": "Provider verification failed for " + topic}
    if handler.settings_state == "unset":
        handler.settings_state = "normal"
    return 200, {"published": topic == "geoip", "unchanged": topic != "geoip",
                 "topic": topic,
                 "revision": "abcdef0123456789abcdef0123456789",
                 "published_revision": "abcdef0123456789abcdef0123456789",
                 "version_id": "s3-version-id",
                 "pending_restart": topic == "geoip",
                 "results": {topic: result}}
