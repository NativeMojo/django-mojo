from copy import deepcopy
from urllib.parse import parse_qs


NAME = "activity"

INCIDENTS = [
    {"id": 301, "created": "2026-08-10T16:20:00Z", "priority": 9,
     "status": "open", "scope": "global", "category": "auth:failures",
     "title": "Repeated sign-in failures", "details": "Threshold crossed",
     "source_ip": "203.0.113.25", "group_id": 9,
     "metadata": {"detector": "auth", "api_token": "fixture-must-mask"}},
    {"id": 302, "created": "2026-08-10T14:00:00Z", "priority": 4,
     "status": "resolved", "scope": "global", "category": "deploy",
     "title": "Edge convergence delayed", "details": "Recovered after retry",
     "source_ip": None, "group_id": 7, "metadata": {"attempts": 2}},
]
EVENTS = [
    {"id": 401, "created": "2026-08-10T16:19:00Z", "level": 9,
     "scope": "global", "category": "invalid_password", "source_ip": "203.0.113.25",
     "uid": 2, "title": "Invalid password", "details": "Bouncer rejected login",
     "incident_id": 301, "group_id": 9, "metadata": {"password": "fixture-must-mask"}},
    {"id": 402, "created": "2026-08-10T14:01:00Z", "level": 4,
     "scope": "webapp", "category": "deploy:retry", "hostname": "edge-2",
     "model_name": "WebApp", "model_id": 42, "title": "Deployment retried",
     "details": "Coordinator scheduled retry", "group_id": 7, "metadata": {"attempt": 2}},
]
LOGS = [
    {"id": 501, "created": "2026-08-10T16:19:01Z", "level": "warning",
     "kind": "auth", "method": "POST", "path": "/api/login", "ip": "203.0.113.25",
     "uid": 2, "gid": 9, "username": "avery@example.com", "log": "Login rejected",
     "payload": {"username": "avery@example.com", "password": "fixture-must-mask"}},
    {"id": 502, "created": "2026-08-10T14:01:01Z", "level": "info",
     "kind": "deploy", "method": "POST", "path": "/api/edge/deploy", "gid": 7,
     "model_name": "WebApp", "model_id": 42, "log": "Deployment retry queued"},
    # A Domain-subject row, so the log inspector's cross-link to the domain page
    # has something to link to.
    {"id": 503, "created": "2026-08-10T13:30:00Z", "level": "info",
     "kind": "dns", "method": "POST", "path": "/api/dnsman/dns", "gid": 7,
     "model_name": "Domain", "model_id": 11, "log": "Record set replaced"},
]
TICKETS = [
    {"id": 601, "created": "2026-08-10T16:22:00Z", "modified": "2026-08-10T16:30:00Z",
     "title": "Review auth source", "description": "Confirm whether address is hostile",
     "status": "open", "priority": 8, "category": "security", "user_id": 2,
     "group_id": 9, "assignee_id": 1, "incident_id": 301,
     "activity_group_label": "Web Operations", "activity_assignee_label": "Ian Smith",
     "metadata": {"authorization": "fixture-must-mask"}},
]

ENDPOINTS = {
    "/api/incident/incident": "activity_incidents",
    "/api/incident/event": "activity_events",
    "/api/logs": "activity_logs",
    "/api/incident/ticket": "activity_tickets",
}


def describe(capabilities):
    values = {"view_logs": capabilities["view_logs"],
              "view_security": capabilities["view_security"],
              "manage_security": capabilities["manage_security"]}
    return {"id": NAME, "enabled": any(values.values()), "capabilities": values}


def reset(handler, fixtures, *, activity_state="full", **options):
    handler.activity_state = activity_state
    handler.activity_incidents = deepcopy(INCIDENTS) if activity_state == "full" else []
    handler.activity_events = deepcopy(EVENTS) if activity_state == "full" else []
    handler.activity_logs = deepcopy(LOGS) if activity_state == "full" else []
    handler.activity_tickets = deepcopy(TICKETS) if activity_state == "full" else []


def get(handler, parsed):
    path = parsed.path.rstrip("/") or "/"
    base = next((endpoint for endpoint in ENDPOINTS if path == endpoint or path.startswith(f"{endpoint}/")), None)
    if base is None:
        return None
    if handler.activity_state == "unavailable":
        return 503, {"error": "Deterministic Activity source unavailable"}
    rows = list(getattr(handler, ENDPOINTS[base]))
    if path != base:
        try:
            pk = int(path.rsplit("/", 1)[-1])
        except ValueError:
            return 404, {"error": "Not found"}
        return 200, next((row for row in rows if row["id"] == pk), {})
    query = parse_qs(parsed.query)
    reserved = {"graph", "start", "offset", "size", "limit", "sort", "search"}
    for key, values in query.items():
        if key in reserved or not values:
            continue
        value = values[0]
        if key.endswith("__gte"):
            field = key[:-5]; rows = [row for row in rows if str(row.get(field, "")) >= value]
        elif key.endswith("__lte"):
            field = key[:-5]; rows = [row for row in rows if str(row.get(field, "")) <= value]
        else:
            alias = {"group": "group_id", "incident": "incident_id",
                     "user": "user_id", "assignee": "assignee_id"}.get(key, key)
            rows = [row for row in rows if str(row.get(alias, "")) == value]
    search = query.get("search", [""])[0].lower()
    if search:
        rows = [
            row for row in rows
            if search in " ".join(
                str(value).lower() for value in row.values()
                if not isinstance(value, (dict, list)))
        ]
    sort = query.get("sort", ["-id"])[0]
    field = sort.lstrip("-")
    rows.sort(key=lambda row: (row.get(field) is None, row.get(field)), reverse=sort.startswith("-"))
    count = len(rows)
    start = max(0, int(query.get("start", [0])[0]))
    size = max(1, min(50, int(query.get("size", [25])[0])))
    return 200, {"results": rows[start:start + size], "count": count, "start": start, "size": size}


def put(handler, path, payload):
    base = next((endpoint for endpoint in ("/api/incident/incident", "/api/incident/ticket")
                 if path.startswith(f"{endpoint}/")), None)
    if base is None:
        return None
    try:
        pk = int(path.rsplit("/", 1)[-1])
    except ValueError:
        return 404, {"error": "Not found"}
    rows = getattr(handler, ENDPOINTS[base])
    row = next((item for item in rows if item["id"] == pk), None)
    if row is None:
        return 404, {"error": "Not found"}
    if "status" in payload:
        row["status"] = payload["status"]
        if base.endswith("ticket"):
            row["modified"] = "2026-08-10T18:00:00Z"
    return 200, row
