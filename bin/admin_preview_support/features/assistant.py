"""Deterministic Assistant fixtures for the Admin preview.

The preview server has no WebSocket bridge, so the panel renders in its
"cannot reach the realtime service" state. That is itself a state worth being
able to look at; the transport's live behaviour is covered by the static
contracts and the tests under tests/test_assistant*.
"""

from copy import deepcopy
from urllib.parse import parse_qs


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

# --- remote agent access (MCP) ---------------------------------------------
MCP_PATH = "/api/assistant/mcp"
MCP_URL = "https://admin.example.com/api/assistant/mcp"
# The second registered resource: the REST API root, reached with the `api`
# scope. Grants at both paths are listed, counted and swept together.
API_ROOT_URL = "https://admin.example.com/api"
MCP_DISCOVERY_URL = (
    "https://admin.example.com/.well-known/oauth-protected-resource"
    "/api/assistant/mcp")
MCP_CHECKED_AT = "2026-08-21T18:30:00+00:00"
# A visibly newer stamp, so "Check now" is observably a re-check.
MCP_RECHECKED_AT = "2026-08-22T09:00:00+00:00"

MCP_UNCHECKED = {"ok": None, "code": "", "detail": "", "checked_at": ""}
MCP_DISCOVERY = {
    "ok": {"ok": True, "code": "ok",
           "detail": "The discovery document is reachable at the public address.",
           "checked_at": MCP_CHECKED_AT},
    "unreachable": {
        "ok": False, "code": "unreachable",
        "detail": "The public address answered HTTP 404 instead of the discovery "
                  "document — nginx is not forwarding /.well-known/ to the "
                  "application.",
        "checked_at": MCP_CHECKED_AT},
    "disabled": {
        "ok": False, "code": "disabled",
        "detail": "Remote agent access is switched off, so nothing is published.",
        "checked_at": MCP_CHECKED_AT},
}

# Names and dates only — the real `list_grants` carries no jti and no hash, and
# a fixture that invented one would let a real leak ship looking correct.
MCP_GRANTS = [
    {"id": 41,
     "client": {"id": 7, "client_id": "https://claude.ai/.well-known/mcp-client",
                "name": "Claude"},
     "user": {"id": 1, "email": "ian@example.com", "display_name": "Ian Smith"},
     "resource": MCP_URL, "scopes": ["mcp"], "access": "tools",
     "created": "2026-08-18T14:05:00+00:00",
     "last_used": "2026-08-21T17:40:00+00:00",
     "expires": "2026-09-17T14:05:00+00:00",
     "is_active": True, "revoked_reason": ""},
    {"id": 42,
     "client": {"id": 8, "client_id": "dcr-2f9c41a7", "name": "Claude Code"},
     "user": {"id": 2, "email": "avery@example.com", "display_name": "Avery Cole"},
     "resource": MCP_URL, "scopes": ["mcp"], "access": "tools",
     "created": "2026-08-20T09:12:00+00:00",
     "last_used": None,
     "expires": "2026-09-19T09:12:00+00:00",
     "is_active": True, "revoked_reason": ""},
    # A connection at the API ROOT, consented to with both scopes: the Access
    # column has to be visibly different from the tool-door rows, or the
    # preview would show a table that cannot tell them apart.
    {"id": 43,
     "client": {"id": 9, "client_id": "dcr-8b31e04f", "name": "Ops script"},
     "user": {"id": 2, "email": "avery@example.com", "display_name": "Avery Cole"},
     "resource": API_ROOT_URL, "scopes": ["mcp", "api"], "access": "both",
     "created": "2026-08-21T11:48:00+00:00",
     "last_used": "2026-08-22T08:05:00+00:00",
     "expires": "2026-09-20T11:48:00+00:00",
     "is_active": True, "revoked_reason": ""},
]


def describe(capabilities):
    values = {
        "view": capabilities["assistant"],
        "ready": capabilities["assistant_ready"],
        "setup": capabilities["assistant_setup"],
        "mcp": capabilities["assistant_mcp"],
    }
    return {"id": NAME, "enabled": values["view"], "capabilities": values}


def _mcp_state(handler, checked=False):
    scenario = getattr(handler, "assistant_mcp_state", "connected")
    if scenario == "off":
        discovery = dict(MCP_DISCOVERY["disabled"]) if checked else dict(MCP_UNCHECKED)
    elif scenario == "unreachable":
        discovery = dict(MCP_DISCOVERY["unreachable"])
    else:
        discovery = dict(MCP_DISCOVERY["ok"])
    if checked:
        discovery["checked_at"] = MCP_RECHECKED_AT
    grants = list(getattr(handler, "assistant_grants", None) or []) \
        if scenario == "connected" else []
    return {
        "enabled": scenario != "off",
        "path": MCP_PATH,
        "url": MCP_URL,
        "discovery_url": MCP_DISCOVERY_URL,
        "discovery": discovery,
        "grants": grants,
        "grant_count": len(grants),
    }


def _state(handler, checked=False):
    scenario = getattr(handler, "assistant_state", "configured")
    key = {"configured": True, "hint": KEY_HINT, "source": "admin"}
    handler_key = {"configured": True, "hint": KEY_HINT, "source": "admin"}
    verify = {"ok": True, "code": "verified",
              "message": "Anthropic accepted this key.",
              "at": "2026-08-19T11:02:00+00:00"}
    handler_verify = dict(verify)
    unchecked = {"ok": None, "code": "", "message": "", "at": ""}
    if scenario == "unset":
        key = {"configured": False, "hint": "", "source": "none"}
        handler_key = {"configured": False, "hint": "", "source": "none"}
        verify = dict(unchecked)
        handler_verify = dict(unchecked)
    elif scenario == "verify_failed":
        verify = {"ok": False, "code": "invalid_key",
                  "message": "Anthropic rejected this key.",
                  "at": "2026-08-19T11:02:00+00:00"}
    stopped = scenario == "route_stopped"
    route_ready = scenario not in ("unset", "route_stopped")
    route = {
        "feature": "assistant",
        "provider": "anthropic" if scenario != "unset" else "",
        "model": "claude-sonnet-5" if scenario != "unset" else "",
        "credential": "admin" if scenario != "unset" else "",
        "credential_configured": scenario != "unset",
        "ready": route_ready,
        "error": "emergency_stopped" if stopped else
                 ("credential_missing" if scenario == "unset" else ""),
    }
    return {
        "schema_version": 2,
        "enabled": scenario != "disabled",
        "key": key,
        "handler_key": handler_key,
        "model": {
            "selected": "" if scenario == "unset" else "claude-sonnet-5",
            "effective": route["model"],
            "source": "automatic" if scenario == "unset" else "admin",
            "choices": deepcopy(CHOICES),
        },
        "verify": verify,
        "handler_verify": handler_verify,
        "emergency_stop": stopped,
        "emergency_stop_static": stopped,
        "emergency_stop_database": False,
        "route": route,
        "autonomous_triage": scenario == "configured",
        "autonomous_triage_activated_at": (
            "2026-08-19T10:00:00+00:00" if scenario == "configured" else None),
        "assistant_installed": True,
        "realtime_installed": True,
        "mcp": _mcp_state(handler, checked=checked),
        "safety": {
            "hours": 24,
            "requests": [{
                "provider": "anthropic", "feature": "assistant",
                "status": "succeeded", "count": 14,
                "input_tokens": 4200, "output_tokens": 860,
            }],
            "breakers": [],
        },
    }


def get(handler, parsed):
    if parsed.path != PATH:
        return None
    # `?check=discovery` is the owner's explicit self-check control; the
    # fixture re-stamps the verdict so the page visibly re-checks.
    checked = parse_qs(parsed.query).get("check") == ["discovery"]
    return 200, _state(handler, checked=checked)


def post(handler, path, payload):
    if path != PATH:
        return None
    action = payload.get("action")
    if action == "verify":
        state = _state(handler)
        return 200, {"schema_version": 2, "verified": True,
                     "result": state["verify"] if state["verify"]["code"]
                     else {"ok": True, "code": "verified",
                           "message": "Anthropic accepted this key."},
                     "state": state}
    if action == "save":
        if handler.assistant_state == "verify_failed" and (
                payload.get("api_key") or payload.get("handler_api_key")):
            return 400, {"error": "The API key was not accepted. "
                                  "Anthropic rejected this key."}
        handler.assistant_state = "configured" if payload.get("enabled") else "disabled"
        # Absent or null leaves the switch alone, exactly as the service does.
        remote = payload.get("mcp_enabled")
        if remote is False:
            handler.assistant_mcp_state = "off"
        elif remote is True and handler.assistant_mcp_state == "off":
            handler.assistant_mcp_state = "connected"
        return 200, {"schema_version": 2, "saved": True, "state": _state(handler)}
    # Both revocations answer a count, never a 404, and the page repaints from
    # the state they return — exactly like the service.
    if action == "revoke_grant":
        before = _mcp_state(handler)["grant_count"]
        handler.assistant_grants = [
            row for row in (getattr(handler, "assistant_grants", None) or [])
            if row["id"] != payload.get("grant_id")]
        state = _state(handler)
        return 200, {"schema_version": 2,
                     "revoked": before - state["mcp"]["grant_count"],
                     "state": state}
    if action == "revoke_all_grants":
        before = _mcp_state(handler)["grant_count"]
        handler.assistant_grants = []
        return 200, {"schema_version": 2, "revoked": before,
                     "state": _state(handler)}
    return 400, {"error": "action must be verify, save, revoke_grant, "
                          "or revoke_all_grants"}


def reset(handler, fixtures, **options):
    handler.assistant_state = options.get("assistant_state", "configured")
    handler.assistant_mcp_state = options.get("assistant_mcp_state", "connected")
    handler.assistant_grants = deepcopy(MCP_GRANTS)
