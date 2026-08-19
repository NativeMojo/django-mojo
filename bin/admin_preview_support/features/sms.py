from copy import deepcopy


NAME = "sms"

SUMMARY_PATH = "/api/account/admin/messaging-sms/summary"
MUTATE_PATH = "/api/account/admin/messaging-sms"

_NOT_INSTALLED = {
    "schema_version": 1, "installed": False,
    "error": "Text messaging is not installed on this platform",
    "error_code": "not_installed",
}


def describe(capabilities):
    values = {
        "view": capabilities["messaging_sms"],
        "system_write": capabilities["messaging_sms_system_write"],
    }
    return {"id": NAME, "enabled": values["view"], "capabilities": values}


def _summary(handler):
    state = handler.sms_state
    if state == "not_installed":
        return deepcopy(_NOT_INSTALLED)
    report = {
        "schema_version": 1,
        "installed": True,
        "system": None,
        "group_overrides": {"items": [], "count": 0, "truncated": False},
        "verify_state": None,
        "settings_fallback": {
            "twilio_number_configured": True,
            "twilio_credentials_configured": False,
        },
        "capabilities": {"view": True, "system_write": True},
        "configuration_revision": "fedcba9876543210fedcba9876543210",
    }
    if state == "unset":
        return report
    report["system"] = {
        "id": 7, "name": "Mojo Remote SMS", "provider": "mojo",
        "is_active": True, "test_mode": state == "test_mode",
        "twilio_from_number": "", "aws_region": "", "aws_sender_id": "",
        "mojo_remote_url": "https://sms.example.com",
        "secrets": {
            "twilio_account_sid": False, "twilio_auth_token": False,
            "aws_access_key_id": False, "aws_secret_access_key": False,
            "mojo_api_key": True,
        },
    }
    report["group_overrides"] = {
        "items": [{
            "id": 12, "group": {"id": 2, "name": "Acme West"},
            "name": "Acme Twilio", "provider": "twilio", "test_mode": False,
        }],
        "count": 1, "truncated": False,
    }
    failed = state == "verify_failed"
    report["verify_state"] = {
        "ok": not failed,
        "code": "invalid_credentials" if failed else None,
        "message": ("sms.example.com rejected the API key" if failed
                    else "Connection verified"),
        "at": "2026-08-18T09:14:00+00:00",
    }
    return report


def get(handler, parsed):
    if parsed.path != SUMMARY_PATH:
        return None
    return 200, _summary(handler)


def post(handler, path, payload):
    if path != MUTATE_PATH:
        return None
    if handler.sms_state == "not_installed":
        return 200, deepcopy(_NOT_INSTALLED)
    action = payload.get("action")
    if action == "test_connection":
        if handler.sms_state == "test_mode":
            return 200, {"schema_version": 1, "installed": True,
                         "success": True, "state": "test_mode", "error": None,
                         "message": "Test mode — provider not contacted",
                         "config": {"id": 7, "provider": "mojo",
                                    "name": "Mojo Remote SMS"}}
        failed = handler.sms_state == "verify_failed"
        return 200, {
            "schema_version": 1, "installed": True,
            "success": not failed,
            "state": "failed" if failed else "ok",
            "error": "invalid_credentials" if failed else None,
            "message": ("The provider rejected the API key." if failed
                        else "Mojo provider reachable and API key valid"),
            "config": {"id": 7, "provider": "mojo", "name": "Mojo Remote SMS"},
        }
    if action == "send_test":
        to_number = str(payload.get("to_number") or "")
        is_test = to_number.startswith("+1555")
        return 200, {
            "schema_version": 1, "installed": True,
            "sent": True, "test_number": is_test,
            "message": ("Test number — nothing was sent" if is_test
                        else "Test message handed to the provider"),
            "sms": {"id": 3101, "status": "sent", "provider": "mojo",
                    "from_number": "+18005550100", "to_number": to_number,
                    "is_test": is_test, "error_code": None,
                    "error_message": None},
        }
    if action == "save":
        if handler.sms_state == "verify_failed":
            return 400, {"error": "Provider verification failed: the provider "
                                  "rejected the API key."}
        handler.sms_state = "configured"
        return 200, {
            "schema_version": 1, "installed": True, "saved": True,
            "topic": "sms", "provider": payload.get("provider") or "mojo",
            "config_id": 7,
            "revision": "abcdef0123456789abcdef0123456789",
            "results": {"sms": {"success": True, "code": None,
                                "message": "Connection verified"}},
        }
    return 400, {"error": "action must be save, test_connection, or send_test"}


def reset(handler, fixtures, **options):
    handler.sms_state = options.get("sms_state", "configured")
