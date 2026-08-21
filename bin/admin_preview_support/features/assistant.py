"""Deterministic Assistant fixtures for the Admin preview.

The preview server has no WebSocket bridge, so the panel renders in its
"cannot reach the realtime service" state. That is itself a state worth being
able to look at; the transport's live behaviour is covered by the static
contracts and the tests under tests/test_assistant*.
"""

from copy import deepcopy


NAME = "assistant"

PATH = "/api/account/admin/assistant"

# Exactly four characters, following the GEOIP_HINT precedent: a fixture that
# emitted more would let a real leak ship looking correct.
KEY_HINT = "4d1a"

CHOICES = [
    {"id": "claude-sonnet-5", "label": "Claude Sonnet 5"},
    {"id": "claude-opus-4-8", "label": "Claude Opus 4.8"},
    {"id": "claude-haiku-4-5", "label": "Claude Haiku 4.5"},
]


def describe(capabilities):
    values = {
        "view": capabilities["assistant"],
        "ready": capabilities["assistant_ready"],
        "setup": capabilities["assistant_setup"],
    }
    return {"id": NAME, "enabled": values["view"], "capabilities": values}


def _state(handler):
    scenario = getattr(handler, "assistant_state", "configured")
    key = {"configured": True, "hint": KEY_HINT, "source": "admin"}
    verify = {"ok": True, "code": "verified",
              "message": "Anthropic accepted this key.",
              "at": "2026-08-19T11:02:00+00:00"}
    if scenario == "unset":
        key = {"configured": False, "hint": "", "source": "none"}
        verify = {"ok": None, "code": "", "message": "", "at": ""}
    elif scenario == "fallback":
        key = {"configured": True, "hint": KEY_HINT, "source": "fallback"}
        verify = {"ok": None, "code": "", "message": "", "at": ""}
    elif scenario == "verify_failed":
        verify = {"ok": False, "code": "invalid_key",
                  "message": "Anthropic rejected this key.",
                  "at": "2026-08-19T11:02:00+00:00"}
    return {
        "schema_version": 1,
        "enabled": scenario != "disabled",
        "key": key,
        "model": {
            "selected": "" if scenario == "unset" else "claude-sonnet-5",
            "effective": "claude-sonnet-5",
            "source": "automatic" if scenario == "unset" else "admin",
            "choices": deepcopy(CHOICES),
        },
        "verify": verify,
        "assistant_installed": True,
        "realtime_installed": True,
    }


def get(handler, parsed):
    if parsed.path != PATH:
        return None
    return 200, _state(handler)


def post(handler, path, payload):
    if path != PATH:
        return None
    action = payload.get("action")
    if action == "verify":
        state = _state(handler)
        return 200, {"schema_version": 1, "verified": True,
                     "result": state["verify"] if state["verify"]["code"]
                     else {"ok": True, "code": "verified",
                           "message": "Anthropic accepted this key."},
                     "state": state}
    if action == "save":
        if handler.assistant_state == "verify_failed" and payload.get("api_key"):
            return 400, {"error": "The API key was not accepted. "
                                  "Anthropic rejected this key."}
        handler.assistant_state = "configured" if payload.get("enabled") else "disabled"
        return 200, {"schema_version": 1, "saved": True, "state": _state(handler)}
    return 400, {"error": "action must be verify or save"}


def reset(handler, fixtures, **options):
    handler.assistant_state = options.get("assistant_state", "configured")
