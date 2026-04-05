"""Logs domain tools — query the logit.Log audit trail."""
from mojo.helpers import dates

MAX_LIMIT = 200
DEFAULT_LIMIT = 50
MAX_MINUTES = 10080  # 7 days
DEFAULT_MINUTES = 60
LOG_TRUNCATE_LENGTH = 500


def _tool_query_logs(params, user):
    """Query the logit.Log audit trail with filters."""
    from mojo.apps.logit.models import Log

    minutes = params.get("minutes", DEFAULT_MINUTES)
    if not isinstance(minutes, int) or minutes < 1:
        return {"error": "minutes must be a positive integer"}
    minutes = min(minutes, MAX_MINUTES)

    criteria = {"created__gte": dates.subtract(minutes=minutes)}

    # Apply filters
    if params.get("level"):
        criteria["level"] = params["level"]
    if params.get("kind"):
        criteria["kind"] = params["kind"]
    if params.get("model_name"):
        criteria["model_name"] = params["model_name"]
    if params.get("model_id"):
        criteria["model_id"] = params["model_id"]
    if params.get("uid") is not None and params.get("uid") != "":
        criteria["uid"] = params["uid"]
    if params.get("ip"):
        criteria["ip"] = params["ip"]
    if params.get("path"):
        criteria["path__icontains"] = params["path"]
    if params.get("method"):
        criteria["method"] = params["method"].upper()

    queryset = Log.objects.filter(**criteria)

    # Free-text search in log content
    search = params.get("search", "").strip()
    if search:
        queryset = queryset.filter(log__icontains=search)

    queryset = queryset.order_by("-created")

    # Count only mode
    if params.get("count_only", False):
        return {"count": queryset.count(), "period_minutes": minutes}

    limit = min(params.get("limit", DEFAULT_LIMIT), MAX_LIMIT)
    verbose = params.get("verbose", False)
    logs = queryset[:limit]

    results = []
    for entry in logs:
        row = {
            "id": entry.id,
            "created": str(entry.created),
            "level": entry.level,
            "kind": entry.kind,
            "method": entry.method,
            "path": entry.path,
            "ip": entry.ip,
            "uid": entry.uid,
            "username": entry.username,
            "model_name": entry.model_name,
            "model_id": entry.model_id,
        }

        # Log content — truncate unless verbose
        log_content = entry.log or ""
        if verbose:
            row["log"] = log_content
            row["payload"] = entry.payload
            row["user_agent"] = entry.user_agent
        else:
            if len(log_content) > LOG_TRUNCATE_LENGTH:
                row["log"] = log_content[:LOG_TRUNCATE_LENGTH]
                row["log_truncated"] = True
            else:
                row["log"] = log_content

        results.append(row)

    return {
        "results": results,
        "count": len(results),
        "total": queryset.count(),
        "period_minutes": minutes,
    }


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "query_logs",
        "description": (
            "Query the audit log trail (logit.Log). Every HTTP request/response, model change, "
            "API error, and custom event is recorded here. Filter by time range, level, kind, "
            "model_name, model_id, user (uid), IP, path, method, or free-text search. "
            "Always time-bounded (default 60 min, max 7 days). "
            "Use this to investigate what happened, trace user activity, find errors, "
            "or see the history of a specific record."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "minutes": {
                    "type": "integer",
                    "description": "Look back N minutes (default 60, max 10080 = 7 days)",
                },
                "level": {
                    "type": "string",
                    "enum": ["info", "warn", "error", "debug"],
                    "description": "Filter by log level",
                },
                "kind": {
                    "type": "string",
                    "description": "Filter by log kind (e.g. 'request', 'response', 'api_error', 'model:created', 'model:changed')",
                },
                "model_name": {
                    "type": "string",
                    "description": "Filter by target model (e.g. 'account.User', 'incident.Incident')",
                },
                "model_id": {
                    "type": "integer",
                    "description": "Filter by target model instance ID",
                },
                "uid": {
                    "type": "integer",
                    "description": "Filter by user ID who triggered the event",
                },
                "ip": {
                    "type": "string",
                    "description": "Filter by client IP address",
                },
                "path": {
                    "type": "string",
                    "description": "Filter by request path (substring match)",
                },
                "method": {
                    "type": "string",
                    "description": "Filter by HTTP method (GET, POST, etc.)",
                },
                "search": {
                    "type": "string",
                    "description": "Free-text search in log content",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default 50, max 200)",
                },
                "count_only": {
                    "type": "boolean",
                    "description": "If true, return only the count",
                },
                "verbose": {
                    "type": "boolean",
                    "description": "If true, include full log content, payload, and user_agent",
                },
            },
        },
        "handler": _tool_query_logs,
        "permission": "view_logs",
    },
]
