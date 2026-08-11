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
           writable="catalog", owner="Settings", behavior="immediate", route=""):
    return {
        "key": key, "label": label, "section": section,
        "description": f"Deterministic preview for {label.lower()}.",
        "value_type": value_type, "default": True if value_type == "boolean" else None,
        "resolver": "dynamic", "raw_semantics": "Global database override",
        "effective_semantics": "Database, deployment, then default",
        "sensitivity": "public", "scope": "global", "writable": writable,
        "owner": owner, "change_behavior": behavior, "constraints": "",
        "owner_route": route, "effective_value": value, "source": source,
        "duplicate_override": False, "ignored_database_override": False,
        "can_write": writable == "catalog", "can_clear": writable == "catalog" and source == "database",
        "can_owner_edit": writable == "owner" and behavior == "typed_owner",
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
        _entry("SYSTEM_SETUP_LOCAL_API_URL", "Local API probe", "Security & operations", {"configured": True}, source="deployment", value_type="configured", writable="none", owner="Deployment settings", behavior="deploy"),
    ]
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
    return 200, {"schema_version": 1, "capabilities": {
                     "catalog_write": True, "owner_display": True, "owner_edit": True},
                 "setup_incomplete": False,
                 "sections": ["General", "Sign-in & registration", "Users", "Email", "Domains & DNS", "Edge & Web Apps", "Security & operations"],
                 "entries": deepcopy(handler.setting_entries)}


def post(handler, path, payload):
    if path != "/api/account/admin/settings":
        return None
    if handler.settings_state == "delay":
        time.sleep(1.25)
    if handler.settings_state == "error":
        return 503, {"error": "Deterministic Settings save failure"}
    if handler.settings_state == "fresh":
        return 440, {"error": "Fresh authentication required"}
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
